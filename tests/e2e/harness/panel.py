"""An in-memory PasarGuard behind ``httpx.MockTransport``.

The bot's real ``PasarGuardClient`` talks to it, so the client's own request
building, error mapping and response parsing run for real. Only the server is
fake, and it is held to the vendored OpenAPI file:

* every request is checked against ``docs/vendor/pasarguard-openapi.json`` (path,
  method, parameter types, body fields). A request the real panel would refuse
  gets the same 422 here and is listed in ``panel.violations``, which fails the
  scenario unless it called ``panel.allow_violations()``;
* every answer the fake builds is checked against the spec as well, so the fake
  cannot drift into a shape the real panel never sends.

It implements exactly the endpoints ``api_client/client.py`` calls. A call to
anything else answers 501 and is a violation.

Faults, for the paths the checklist breaks on purpose::

    panel.fail_next("POST /api/user", status=500)
    panel.timeout_next("PUT /api/user/{username}")                # request never answered
    panel.timeout_next("PUT /api/user/{username}", applied=True)  # applied, reply lost
    panel.forbid("GET /api/nodes")                                # 403 until panel.allow(...)
    panel.go_down()                                               # connection refused
    panel.reject_key()                                            # a wrong API key: 401
"""

# ruff: noqa: ARG002, PLR0911  -- handlers share one signature; early returns read better here

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from tests.e2e.harness.openapi import OpenApi, RouteMatch

GB = 1024**3


@dataclass
class PanelUser:
    id: int
    username: str
    token: str
    created_at: datetime
    status: str = "active"
    used_traffic: int = 0
    expire: datetime | None = None
    data_limit: int | None = None
    reset_strategy: str = "no_reset"
    note: str | None = None
    group_ids: list[int] = field(default_factory=list)
    hwid_limit: int | None = None
    on_hold_expire_duration: int | None = None
    on_hold_timeout: datetime | None = None
    auto_delete_in_days: int | None = None
    edit_at: datetime | None = None
    online_at: datetime | None = None

    @property
    def gb(self) -> float | None:
        """Volume in GB, ``None`` when unlimited (the panel stores unlimited as 0 or null)."""
        return None if not self.data_limit else self.data_limit / GB

    @property
    def unlimited(self) -> bool:
        return not self.data_limit

    def days_left(self, now: datetime | None = None) -> float | None:
        if self.expire is None:
            return None
        return (self.expire - (now or datetime.now(UTC))).total_seconds() / 86400


@dataclass
class PanelRequest:
    method: str
    path: str
    template: str | None
    query: dict[str, Any]
    body: Any
    status: int = 0
    violations: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.method} {self.template or self.path}"


@dataclass
class _Fault:
    pattern: str
    kind: str  # status | timeout | forbid
    status: int = 500
    times: int | None = 1  # None = until cleared
    applied: bool = False
    body: Any = None


class PanelError(Exception):
    """Raised inside a handler to answer with an error status."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class FakePanel:
    def __init__(
        self,
        *,
        base_url: str = "https://panel.test",
        api_key: str = "pg_key_e2e_test_key",
        version: str = "1.0.0-fake",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.version = version
        self.api = OpenApi()
        self.users: dict[str, PanelUser] = {}
        self.groups: dict[int, str] = {1: "direct", 2: "tunnel", 3: "premium"}
        self.nodes: dict[int, dict[str, Any]] = {
            1: {"name": "node-1", "status": "connected", "message": None}
        }
        self.hwids: dict[int, list[dict[str, Any]]] = {}
        self.sub_updates: dict[str, list[dict[str, Any]]] = {}
        self.requests: list[PanelRequest] = []
        self.violations: list[str] = []
        self._faults: list[_Fault] = []
        self._down = False
        self._wrong_key = False
        self._allow_violations = False
        self._next_user_id = 1000
        # Called before every panel request (see ``App.commit_at_every_network_call``).
        self.on_network: Callable[[], None] | None = None
        self.transport = httpx.MockTransport(self._handle)
        self._routes: dict[tuple[str, str], Callable[[RouteMatch, dict[str, Any], Any], Any]] = {
            ("GET", "/api/admin"): self._get_admin,
            ("POST", "/api/user"): self._create_user,
            ("GET", "/api/user/{username}"): self._get_user,
            ("PUT", "/api/user/{username}"): self._modify_user,
            ("DELETE", "/api/user/{username}"): self._delete_user,
            ("GET", "/api/user/by-id/{user_id}"): self._get_user_by_id,
            ("PUT", "/api/user/{username}/disabled"): self._disable_user,
            ("POST", "/api/user/{username}/revoke_sub"): self._revoke_sub,
            ("POST", "/api/user/{username}/reset"): self._reset_usage,
            ("GET", "/api/user/{user_id}/hwids"): self._get_hwids,
            ("POST", "/api/user/{user_id}/hwids/reset"): self._reset_hwids,
            ("GET", "/api/user/{username}/sub_update"): self._get_sub_updates,
            ("GET", "/api/user/{username}/usage"): self._get_usage,
            ("GET", "/api/groups"): self._list_groups,
            ("GET", "/api/groups/simple"): self._list_groups_simple,
            ("GET", "/api/system"): self._system,
            ("GET", "/api/nodes"): self._list_nodes,
            ("GET", "/api/nodes/simple"): self._list_nodes_simple,
            ("POST", "/api/node/{node_id}/reconnect"): self._reconnect_node,
            ("GET", "/sub/{token}/info"): self._sub_info,
        }

    # ------------------------------------------------------------ test surface

    def user(self, username: str) -> PanelUser:
        """The account as the panel holds it. Fails with the names it does hold."""
        if username not in self.users:
            raise AssertionError(
                f"the panel has no user {username!r}; it holds {sorted(self.users) or 'nobody'}"
            )
        return self.users[username]

    def only_user(self) -> PanelUser:
        """The one account on the panel (use when a scenario creates exactly one)."""
        if len(self.users) != 1:
            raise AssertionError(f"expected one panel user, found {sorted(self.users)}")
        return next(iter(self.users.values()))

    def add_group(self, group_id: int, name: str) -> None:
        self.groups[group_id] = name

    def add_node(
        self, node_id: int, name: str, status: str = "connected", message: str | None = None
    ) -> None:
        self.nodes[node_id] = {"name": name, "status": status, "message": message}

    def set_node(self, node_id: int, status: str, message: str | None = None) -> None:
        self.nodes[node_id]["status"] = status
        self.nodes[node_id]["message"] = message

    def add_hwid(self, username: str, hwid: str, *, device_os: str = "Android") -> None:
        user = self.user(username)
        now = datetime.now(UTC).isoformat()
        self.hwids.setdefault(user.id, []).append(
            {
                "id": len(self.hwids.get(user.id, [])) + 1,
                "hwid": hwid,
                "device_os": device_os,
                "created_at": now,
                "last_used_at": now,
            }
        )

    def add_sub_update(self, username: str, user_agent: str = "v2rayNG/1.8") -> None:
        self.sub_updates.setdefault(username, []).insert(
            0, {"created_at": datetime.now(UTC).isoformat(), "user_agent": user_agent}
        )

    def requests_to(self, key: str) -> list[PanelRequest]:
        """Requests like ``"POST /api/user"`` (OpenAPI template, or the concrete path)."""
        return [r for r in self.requests if key in (r.key, f"{r.method} {r.path}")]

    def allow_violations(self) -> None:
        """This scenario expects the bot to send something the real panel would refuse."""
        self._allow_violations = True

    def unexpected_violations(self) -> list[str]:
        return [] if self._allow_violations else list(self.violations)

    # ----------------------------------------------------------------- faults

    def fail_next(
        self, pattern: str, *, status: int = 500, times: int | None = 1, body: Any = None
    ) -> None:
        self._faults.append(_Fault(pattern, "status", status, times, body=body))

    def timeout_next(self, pattern: str, *, times: int | None = 1, applied: bool = False) -> None:
        """The reply never arrives. ``applied=True``: the panel did the work first."""
        self._faults.append(_Fault(pattern, "timeout", times=times, applied=applied))

    def forbid(self, pattern: str) -> None:
        """Answer 403 until ``allow(pattern)``: a key without that permission."""
        self._faults.append(_Fault(pattern, "forbid", 403, times=None))

    def allow(self, pattern: str) -> None:
        self._faults = [
            f for f in self._faults if not (f.kind == "forbid" and f.pattern == pattern)
        ]

    def go_down(self) -> None:
        self._down = True

    def go_up(self) -> None:
        self._down = False

    def reject_key(self, rejected: bool = True) -> None:
        """Every call answers 401, as if the API key in the bot were wrong."""
        self._wrong_key = rejected

    def clear_faults(self) -> None:
        self._faults.clear()
        self._down = False
        self._wrong_key = False

    def _take_fault(self, method: str, path: str, template: str | None) -> _Fault | None:
        for fault in self._faults:
            verb, _, where = fault.pattern.partition(" ")
            if verb.upper() != method:
                continue
            if where not in {path, template} and not re.fullmatch(
                re.sub(r"\\\{\w+\\\}", "[^/]+", re.escape(where)), path
            ):
                continue
            if fault.times is not None:
                fault.times -= 1
                if fault.times <= 0:
                    self._faults.remove(fault)
            return fault
        return None

    # ------------------------------------------------------------- the server

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.on_network is not None:
            self.on_network()
        method = request.method.upper()
        path = request.url.path
        if self._down:
            raise httpx.ConnectError("connection refused (panel is down)", request=request)
        route = self.api.match(method, path)
        record = PanelRequest(method, path, route.template if route else None, {}, None)
        self.requests.append(record)
        fault = self._take_fault(method, path, record.template)
        if fault is not None and fault.kind == "timeout" and not fault.applied:
            raise httpx.ReadTimeout("the panel did not answer", request=request)

        response = self._serve(request, route, record, fault)

        if fault is not None and fault.kind == "timeout" and fault.applied:
            raise httpx.ReadTimeout("the panel applied the change, reply lost", request=request)
        record.status = response.status_code
        return response

    def _serve(
        self,
        request: httpx.Request,
        route: RouteMatch | None,
        record: PanelRequest,
        fault: _Fault | None,
    ) -> httpx.Response:
        if route is None:
            return self._violation(
                record,
                404,
                f"{record.method} {record.path} is not in the OpenAPI file (invented endpoint?)",
            )
        is_public = route.template.startswith("/sub/")
        if not is_public:
            sent_key = request.headers.get("x-api-key", "")
            if self._wrong_key or sent_key != self.api_key:
                return _json(401, {"detail": "Could not validate credentials"})
        if fault is not None and fault.kind == "forbid":
            return _json(403, {"detail": "You are not allowed to access this resource"})
        if fault is not None and fault.kind == "status":
            return _json(fault.status, fault.body or {"detail": "injected fault"})

        query_items = list(request.url.params.multi_items())
        query, problems = self.api.check_query(route, query_items)
        problems += self.api.check_path_params(route)
        body: Any = None
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                problems.append("body: not valid JSON")
        problems += self.api.check_body(route, body)
        record.query, record.body = query, body
        if problems:
            return self._violation(record, 422, "; ".join(problems), detail=problems)

        handler = self._routes.get((route.method, route.template))
        if handler is None:
            return self._violation(
                record,
                501,
                f"FakePanel does not implement {route.method} {route.template} "
                "(api_client does not call it; add it here only if the client starts to)",
            )
        try:
            payload = handler(route, query, body)
            status = 201 if (route.method, route.template) == ("POST", "/api/user") else 200
            if payload is _NO_CONTENT:
                return httpx.Response(204)
        except PanelError as exc:
            return _json(exc.status, {"detail": exc.detail})
        drift = self.api.check_response(route, status, payload)
        if drift:
            raise AssertionError(
                f"FakePanel built an answer that breaks the spec for {record.key}: {drift}"
            )
        return _json(status, payload)

    def _violation(
        self, record: PanelRequest, status: int, message: str, detail: Any = None
    ) -> httpx.Response:
        line = f"{record.method} {record.path}: {message}"
        record.violations.append(line)
        self.violations.append(line)
        return _json(status, {"detail": detail or message})

    # --------------------------------------------------------- view of a user

    def _user_json(self, user: PanelUser) -> dict[str, Any]:
        return {
            "id": user.id,
            "username": user.username,
            "status": user.status,
            "used_traffic": user.used_traffic,
            "lifetime_used_traffic": user.used_traffic,
            "created_at": user.created_at.isoformat(),
            "edit_at": user.edit_at.isoformat() if user.edit_at else None,
            "online_at": user.online_at.isoformat() if user.online_at else None,
            "expire": user.expire.isoformat() if user.expire else None,
            "data_limit": user.data_limit,
            "data_limit_reset_strategy": user.reset_strategy,
            "note": user.note,
            "group_ids": list(user.group_ids),
            "hwid_limit": user.hwid_limit,
            "on_hold_expire_duration": user.on_hold_expire_duration,
            "on_hold_timeout": user.on_hold_timeout.isoformat() if user.on_hold_timeout else None,
            "auto_delete_in_days": user.auto_delete_in_days,
            "subscription_url": f"{self.base_url}/sub/{user.token}",
            "proxy_settings": {},
        }

    @staticmethod
    def _when(value: Any) -> datetime | None:
        """The panel reads an int as epoch seconds and a string as ISO; 0 and null mean never."""
        if value is None or value == 0:
            return None
        if isinstance(value, int | float):
            return datetime.fromtimestamp(value, UTC)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    def _check_groups(self, group_ids: list[int] | None) -> None:
        for gid in group_ids or []:
            if gid not in self.groups:
                raise PanelError(400, f"Group {gid} not found")

    def _find(self, username: str) -> PanelUser:
        user = self.users.get(username)
        if user is None:
            raise PanelError(404, "User not found")
        return user

    # ----------------------------------------------------------------- handlers

    def _get_admin(self, route: RouteMatch, query: dict[str, Any], body: Any) -> Any:
        return {
            "id": 1,
            "username": "e2e-admin",
            "is_disabled": False,
            "is_limited": False,
            "status": "active",
            "total_users": len(self.users),
        }

    def _create_user(self, route: RouteMatch, query: dict[str, Any], body: Any) -> Any:
        name = body["username"]
        if name in self.users:
            raise PanelError(409, "User already exists")
        self._check_groups(body.get("group_ids"))
        self._next_user_id += 1
        user = PanelUser(
            id=self._next_user_id,
            username=name,
            token=secrets.token_urlsafe(18),
            created_at=datetime.now(UTC),
            status=body.get("status") or "active",
            expire=self._when(body.get("expire")),
            data_limit=body.get("data_limit"),
            reset_strategy=body.get("data_limit_reset_strategy") or "no_reset",
            note=body.get("note"),
            group_ids=list(body.get("group_ids") or []),
            hwid_limit=body.get("hwid_limit"),
            on_hold_expire_duration=body.get("on_hold_expire_duration"),
            on_hold_timeout=self._when(body.get("on_hold_timeout")),
            auto_delete_in_days=body.get("auto_delete_in_days"),
        )
        self.users[name] = user
        return self._user_json(user)

    def _get_user(self, route: RouteMatch, query: dict[str, Any], body: Any) -> Any:
        return self._user_json(self._find(route.path_params["username"]))

    def _get_user_by_id(self, route: RouteMatch, query: dict[str, Any], body: Any) -> Any:
        wanted = int(route.path_params["user_id"])
        for user in self.users.values():
            if user.id == wanted:
                return self._user_json(user)
        raise PanelError(404, "User not found")

    def _modify_user(self, route: RouteMatch, query: dict[str, Any], body: Any) -> Any:
        user = self._find(route.path_params["username"])
        self._check_groups(body.get("group_ids"))
        # Absolute writes: a field that is present replaces the stored value.
        if "expire" in body:
            user.expire = self._when(body["expire"])
        for key, attr in (
            ("data_limit", "data_limit"),
            ("data_limit_reset_strategy", "reset_strategy"),
            ("note", "note"),
            ("hwid_limit", "hwid_limit"),
            ("on_hold_expire_duration", "on_hold_expire_duration"),
            ("auto_delete_in_days", "auto_delete_in_days"),
            ("status", "status"),
        ):
            if key in body:
                setattr(user, attr, body[key])
        if "group_ids" in body and body["group_ids"] is not None:
            user.group_ids = list(body["group_ids"])
        user.edit_at = datetime.now(UTC)
        return self._user_json(user)

    def _delete_user(self, route: RouteMatch, query: dict[str, Any], body: Any) -> Any:
        self._find(route.path_params["username"])
        del self.users[route.path_params["username"]]
        return _NO_CONTENT

    def _disable_user(self, route: RouteMatch, query: dict[str, Any], body: Any) -> Any:
        user = self._find(route.path_params["username"])
        user.status = "disabled" if body["disabled"] else "active"
        user.edit_at = datetime.now(UTC)
        return self._user_json(user)

    def _revoke_sub(self, route: RouteMatch, query: dict[str, Any], body: Any) -> Any:
        user = self._find(route.path_params["username"])
        user.token = secrets.token_urlsafe(18)
        user.edit_at = datetime.now(UTC)
        return self._user_json(user)

    def _reset_usage(self, route: RouteMatch, query: dict[str, Any], body: Any) -> Any:
        user = self._find(route.path_params["username"])
        user.used_traffic = 0
        user.edit_at = datetime.now(UTC)
        return self._user_json(user)

    def _user_by_numeric_id(self, raw: str) -> PanelUser:
        wanted = int(raw)
        for user in self.users.values():
            if user.id == wanted:
                return user
        raise PanelError(404, "User not found")

    def _get_hwids(self, route: RouteMatch, query: dict[str, Any], body: Any) -> Any:
        user = self._user_by_numeric_id(route.path_params["user_id"])
        rows = self.hwids.get(user.id, [])
        return {"hwids": rows, "count": len(rows)}

    def _reset_hwids(self, route: RouteMatch, query: dict[str, Any], body: Any) -> Any:
        user = self._user_by_numeric_id(route.path_params["user_id"])
        self.hwids[user.id] = []
        return {}

    def _get_sub_updates(self, route: RouteMatch, query: dict[str, Any], body: Any) -> Any:
        username = route.path_params["username"]
        self._find(username)
        rows = self.sub_updates.get(username, [])
        offset, limit = int(query.get("offset", 0)), int(query.get("limit", 10))
        return {"updates": rows[offset : offset + limit], "count": len(rows)}

    def _get_usage(self, route: RouteMatch, query: dict[str, Any], body: Any) -> Any:
        user = self._find(route.path_params["username"])
        now = datetime.now(UTC)
        return {
            "period": "day",
            "start": user.created_at.isoformat(),
            "end": now.isoformat(),
            "stats": {
                "-1": [{"period_start": now.isoformat(), "total_traffic": user.used_traffic}]
            },
        }

    def _list_groups(self, route: RouteMatch, query: dict[str, Any], body: Any) -> Any:
        wanted = query.get("ids")
        rows = [
            {
                "id": gid,
                "name": name,
                "inbound_tags": [],
                "is_disabled": False,
                "total_users": sum(1 for u in self.users.values() if gid in u.group_ids),
            }
            for gid, name in sorted(self.groups.items())
            if not wanted or gid in wanted
        ]
        return {"groups": rows, "total": len(rows)}

    def _list_groups_simple(self, route: RouteMatch, query: dict[str, Any], body: Any) -> Any:
        wanted = query.get("ids")
        search = (query.get("search") or "").lower()
        rows = [
            {"id": gid, "name": name}
            for gid, name in sorted(self.groups.items())
            if (not wanted or gid in wanted) and search in name.lower()
        ]
        return {"groups": rows, "total": len(rows)}

    def _system(self, route: RouteMatch, query: dict[str, Any], body: Any) -> Any:
        by_status: dict[str, int] = {}
        for user in self.users.values():
            by_status[user.status] = by_status.get(user.status, 0) + 1
        return {
            "version": self.version,
            "uptime_seconds": 86_400,
            "total_user": len(self.users),
            "online_users": 0,
            "active_users": by_status.get("active", 0),
            "on_hold_users": by_status.get("on_hold", 0),
            "disabled_users": by_status.get("disabled", 0),
            "expired_users": by_status.get("expired", 0),
            "limited_users": by_status.get("limited", 0),
            "incoming_bandwidth": 0,
            "outgoing_bandwidth": 0,
        }

    def _node_json(self, node_id: int, node: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": node_id,
            "name": node["name"],
            "address": f"node{node_id}.panel.test",
            "port": 62050,
            "api_port": 62051,
            "usage_coefficient": 1.0,
            "connection_type": "grpc",
            "server_ca": "-----BEGIN CERTIFICATE-----fake",
            "keep_alive": 60,
            "api_key": "secret-node-key",
            "status": node["status"],
            "message": node["message"],
            "core_config_id": None,
            "xray_version": "1.8.24",
            "node_version": "0.1.0",
            "core_version": "1.8.24",
        }

    def _list_nodes(self, route: RouteMatch, query: dict[str, Any], body: Any) -> Any:
        rows = [self._node_json(i, n) for i, n in sorted(self.nodes.items())]
        return {"nodes": rows, "total": len(rows)}

    def _list_nodes_simple(self, route: RouteMatch, query: dict[str, Any], body: Any) -> Any:
        rows = [
            {"id": i, "name": n["name"], "status": n["status"]}
            for i, n in sorted(self.nodes.items())
        ]
        return {"nodes": rows, "total": len(rows)}

    def _reconnect_node(self, route: RouteMatch, query: dict[str, Any], body: Any) -> Any:
        node_id = int(route.path_params["node_id"])
        if node_id not in self.nodes:
            raise PanelError(404, "Node not found")
        return {}

    def _sub_info(self, route: RouteMatch, query: dict[str, Any], body: Any) -> Any:
        token = route.path_params["token"]
        for user in self.users.values():
            if user.token == token:
                return {
                    k: v
                    for k, v in self._user_json(user).items()
                    if k
                    in {
                        "id",
                        "username",
                        "status",
                        "used_traffic",
                        "lifetime_used_traffic",
                        "expire",
                        "data_limit",
                        "data_limit_reset_strategy",
                        "group_ids",
                        "hwid_limit",
                        "created_at",
                    }
                }
        raise PanelError(404, "Not found")


_NO_CONTENT = object()


def _json(status: int, payload: Any) -> httpx.Response:
    return httpx.Response(status, json=payload)


def install(panel: FakePanel, monkeypatch: Any) -> None:
    """Make every ``PasarGuardClient`` the bot builds talk to ``panel``.

    The bot builds its clients through ``api_client.factory`` (and a few direct
    constructor calls), none of which can take a transport. Wrapping the one
    constructor reaches all of them, and the real client code still runs.
    """
    from api_client.client import PasarGuardClient

    original = PasarGuardClient.__init__

    def init(self: PasarGuardClient, **kwargs: Any) -> None:
        if kwargs.get("client") is None:
            base = str(kwargs["base_url"]).rstrip("/")
            host = urlsplit(base).netloc
            if host != urlsplit(panel.base_url).netloc:
                raise AssertionError(
                    f"the bot tried to reach a panel at {base}, but the harness panel is "
                    f"{panel.base_url}: a real address would have left the machine"
                )
            kwargs["client"] = httpx.AsyncClient(
                base_url=base + "/",
                timeout=kwargs.get("timeout", 30.0),
                transport=panel.transport,
            )
        original(self, **kwargs)
        self._owns_client = True

    monkeypatch.setattr(PasarGuardClient, "__init__", init)
