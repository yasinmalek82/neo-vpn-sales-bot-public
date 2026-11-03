"""A small OpenAPI 3.1 checker for the vendored PasarGuard spec.

There is no ``jsonschema`` in the venv, and a full validator is not the point:
the fake panel needs to refuse the requests the real panel refuses, and to check
its *own* answers, so a request or a reply that drifts from
``docs/vendor/pasarguard-openapi.json`` fails loudly instead of being believed.

Checked: the path and method exist, path and query parameters are declared and of
the declared type, the body is JSON that matches its schema (required fields,
types, enums, ranges, string lengths, ``date-time``), and a field the schema does
not declare is reported. The panel ignores an unknown field, so it would be
dropped without a word. That is the "wrong field name" bug.

Not checked: ``pattern`` on anything but strings with a plain regex, ``format``
other than ``date-time``, and ``discriminator``. The spec does not need them here.
"""

# ruff: noqa: PLR0911  -- handlers share one signature; early returns read better here

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

SPEC_PATH = Path(__file__).resolve().parents[3] / "docs" / "vendor" / "pasarguard-openapi.json"


@lru_cache(maxsize=1)
def load_spec() -> dict[str, Any]:
    with SPEC_PATH.open(encoding="utf-8") as fh:
        spec: dict[str, Any] = json.load(fh)
    return spec


@dataclass(frozen=True)
class RouteMatch:
    template: str
    method: str
    operation: dict[str, Any]
    path_params: dict[str, str]


class OpenApi:
    def __init__(self, spec: dict[str, Any] | None = None) -> None:
        self.spec = spec if spec is not None else load_spec()
        self._routes: list[tuple[str, re.Pattern[str], int]] = []
        for template in self.spec["paths"]:
            regex = (
                "^"
                + re.sub(
                    r"\{(\w+)\}",
                    r"(?P<\1>[^/]+)",
                    re.escape(template).replace(r"\{", "{").replace(r"\}", "}"),
                )
                + "$"
            )
            literals = sum(1 for seg in template.split("/") if seg and not seg.startswith("{"))
            self._routes.append((template, re.compile(regex), literals))
        # Most literal segments first, so /user/by-id/{id} beats /user/{name}/{x}.
        self._routes.sort(key=lambda r: -r[2])

    # ------------------------------------------------------------------ routes

    def match(self, method: str, path: str) -> RouteMatch | None:
        method = method.lower()
        for template, regex, _ in self._routes:
            found = regex.match(path)
            if found and method in self.spec["paths"][template]:
                return RouteMatch(
                    template,
                    method.upper(),
                    self.spec["paths"][template][method],
                    found.groupdict(),
                )
        return None

    def path_exists(self, path: str) -> bool:
        return any(regex.match(path) for _, regex, _ in self._routes)

    # --------------------------------------------------------------- resolving

    def resolve(self, schema: Any) -> Any:
        seen = 0
        while isinstance(schema, dict) and "$ref" in schema:
            ref = schema["$ref"]
            if not ref.startswith("#/"):
                raise ValueError(f"unsupported $ref {ref}")
            node: Any = self.spec
            for part in ref[2:].split("/"):
                node = node[part]
            schema = node
            seen += 1
            if seen > 20:
                raise ValueError("$ref loop")
        return schema

    # -------------------------------------------------------------- validation

    def validate(self, schema: Any, value: Any, where: str = "$") -> list[str]:
        schema = self.resolve(schema)
        if not isinstance(schema, dict) or not schema:
            return []
        errors: list[str] = []
        for sub in schema.get("allOf", []):
            errors += self.validate(sub, value, where)
        for key in ("anyOf", "oneOf"):
            if key in schema:
                attempts = [self.validate(sub, value, where) for sub in schema[key]]
                if not any(not a for a in attempts):
                    detail = "; ".join(a[0] for a in attempts if a)[:300]
                    errors.append(f"{where}: matches none of the allowed shapes ({detail})")
        if "const" in schema and value != schema["const"]:
            errors.append(f"{where}: must be {schema['const']!r}")
        if "enum" in schema and value not in schema["enum"]:
            errors.append(f"{where}: {value!r} is not one of {schema['enum']}")
        types = schema.get("type")
        if types is not None:
            allowed = types if isinstance(types, list) else [types]
            if not any(_is_type(value, t) for t in allowed):
                return [*errors, f"{where}: {value!r} is not of type {'/'.join(allowed)}"]
        if isinstance(value, str):
            errors += self._string(schema, value, where)
        elif isinstance(value, bool):
            pass
        elif isinstance(value, int | float):
            if "minimum" in schema and value < schema["minimum"]:
                errors.append(f"{where}: {value} is below the minimum {schema['minimum']}")
            if "maximum" in schema and value > schema["maximum"]:
                errors.append(f"{where}: {value} is above the maximum {schema['maximum']}")
        elif isinstance(value, list):
            if "minItems" in schema and len(value) < schema["minItems"]:
                errors.append(f"{where}: needs at least {schema['minItems']} items")
            if "maxItems" in schema and len(value) > schema["maxItems"]:
                errors.append(f"{where}: at most {schema['maxItems']} items")
            if "items" in schema:
                for idx, item in enumerate(value):
                    errors += self.validate(schema["items"], item, f"{where}[{idx}]")
        elif isinstance(value, dict):
            errors += self._object(schema, value, where)
        return errors

    @staticmethod
    def _string(schema: dict[str, Any], value: str, where: str) -> list[str]:
        errors = []
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{where}: shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{where}: longer than {schema['maxLength']} characters ({len(value)})")
        if schema.get("format") == "date-time":
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                errors.append(f"{where}: {value!r} is not an ISO date-time")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{where}: does not match {schema['pattern']}")
        return errors

    def _object(self, schema: dict[str, Any], value: dict[str, Any], where: str) -> list[str]:
        errors = []
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{where}.{name}: required field is missing")
        extra = schema.get("additionalProperties")
        for key, item in value.items():
            if key in props:
                errors += self.validate(props[key], item, f"{where}.{key}")
            elif isinstance(extra, dict):
                errors += self.validate(extra, item, f"{where}.{key}")
            elif extra is False or (props and extra is None):
                errors.append(
                    f"{where}.{key}: the spec has no such field (the panel would drop it silently)"
                )
        return errors

    # ----------------------------------------------------------- whole requests

    def check_query(
        self, route: RouteMatch, items: list[tuple[str, str]]
    ) -> tuple[dict[str, Any], list[str]]:
        """Coerce the query string to typed values and report what does not fit."""
        declared = {
            self.resolve(p)["name"]: self.resolve(p)
            for p in route.operation.get("parameters", [])
            if self.resolve(p)["in"] == "query"
        }
        grouped: dict[str, list[str]] = {}
        for key, raw in items:
            grouped.setdefault(key, []).append(raw)
        errors: list[str] = []
        values: dict[str, Any] = {}
        for key, raws in grouped.items():
            if key not in declared:
                errors.append(f"query {key}: not a parameter of {route.method} {route.template}")
                continue
            schema = declared[key].get("schema", {})
            coerced = self._coerce(schema, raws)
            if coerced is _BAD:
                errors.append(f"query {key}: {raws!r} does not fit {_describe(self, schema)}")
                continue
            errors += self.validate(schema, coerced, f"query {key}")
            values[key] = coerced
        for name, param in declared.items():
            if param.get("required") and name not in grouped:
                errors.append(f"query {name}: required parameter is missing")
        return values, errors

    def check_path_params(self, route: RouteMatch) -> list[str]:
        errors = []
        for raw in route.operation.get("parameters", []):
            param = self.resolve(raw)
            if param["in"] != "path":
                continue
            value = route.path_params.get(param["name"], "")
            coerced = self._coerce(param.get("schema", {}), [value])
            if coerced is _BAD:
                errors.append(
                    f"path {param['name']}: {value!r} is not {_describe(self, param.get('schema', {}))}"
                )
        return errors

    def check_body(self, route: RouteMatch, body: Any) -> list[str]:
        request_body = route.operation.get("requestBody")
        if request_body is None:
            return [] if body is None else ["body: this operation takes no request body"]
        request_body = self.resolve(request_body)
        schema = request_body.get("content", {}).get("application/json", {}).get("schema")
        if body is None:
            return ["body: required"] if request_body.get("required") else []
        if schema is None:
            return []
        return self.validate(schema, body, "body")

    def check_response(self, route: RouteMatch, status: int, body: Any) -> list[str]:
        """Check the fake's own answer against what the spec says that status looks like."""
        responses = route.operation.get("responses", {})
        spec = responses.get(str(status))
        if spec is None:
            if status in (401, 403, 404, 409, 422, 400):
                return []  # shared error shapes, not worth pinning
            return [f"status {status} is not documented for {route.method} {route.template}"]
        spec = self.resolve(spec)
        schema = spec.get("content", {}).get("application/json", {}).get("schema")
        if schema is None:
            return []
        return self.validate(schema, body, "response")

    # ---------------------------------------------------------------- coercion

    def _types_of(self, schema: Any) -> set[str]:
        schema = self.resolve(schema)
        found: set[str] = set()
        if not isinstance(schema, dict):
            return found
        types = schema.get("type")
        if types:
            found |= set(types if isinstance(types, list) else [types])
        for key in ("anyOf", "oneOf", "allOf"):
            for sub in schema.get(key, []):
                found |= self._types_of(sub)
        return found

    def _coerce(self, schema: Any, raws: list[str]) -> Any:
        types = self._types_of(schema) - {"null"}
        if "array" in types:
            resolved = self.resolve(schema)
            items = resolved.get("items") or next(
                (
                    self.resolve(s).get("items")
                    for s in resolved.get("anyOf", [])
                    if self.resolve(s).get("type") == "array"
                ),
                {},
            )
            out = [self._coerce_one(items or {"type": "string"}, r) for r in raws]
            return _BAD if any(o is _BAD for o in out) else out
        if len(raws) != 1:
            return _BAD
        return self._coerce_one(schema, raws[0])

    def _coerce_one(self, schema: Any, raw: str) -> Any:
        types = self._types_of(schema) - {"null"}
        if "integer" in types:
            try:
                return int(raw)
            except ValueError:
                if types == {"integer"}:
                    return _BAD
        if "number" in types:
            try:
                return float(raw)
            except ValueError:
                if types == {"number"}:
                    return _BAD
        if "boolean" in types:
            if raw in ("true", "false"):
                return raw == "true"
            if types == {"boolean"}:
                return _BAD
        return raw


_BAD = object()


def _is_type(value: Any, name: str) -> bool:
    if name == "null":
        return value is None
    if name == "boolean":
        return isinstance(value, bool)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if name == "string":
        return isinstance(value, str)
    if name == "array":
        return isinstance(value, list)
    if name == "object":
        return isinstance(value, dict)
    return True


def _describe(api: OpenApi, schema: Any) -> str:
    return "/".join(sorted(api._types_of(schema))) or "any"
