"""Read a Mirza mysqldump as inert data, retaining only migration fields."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

_FIELDS = {
    "user": {"id", "Balance", "username", "User_Status", "agent", "register", "limit_usertest"},
    "invoice": {
        "id_invoice",
        "id_user",
        "username",
        "Service_location",
        "time_sell",
        "name_product",
        "price_product",
        "Volume",
        "Service_time",
        "note",
        "Status",
        "id_payment",
        "payable_amount",
    },
    "Payment_report": {
        "id",
        "id_user",
        "id_order",
        "time",
        "price",
        "Payment_Method",
        "payment_Status",
        "id_invoice",
    },
    "service_other": {
        "id",
        "id_user",
        "username",
        "value",
        "time",
        "price",
        "type",
        "status",
        "payable_amount",
        "id_payment",
    },
    "marzban_panel": {"id", "code_panel", "name_panel", "type"},
}
_INTEGER = re.compile(r"[+-]?\d+(?:\.\d+)?\Z")


class DumpError(ValueError):
    """A safe error code; never includes source data or credentials."""


@dataclass(frozen=True)
class MirzaDump:
    sha256: str
    tables: dict[str, list[dict[str, str | None]]] = field(repr=False)

    @staticmethod
    def money(value: str, *, source_unit: str | None) -> int:
        if source_unit not in {"toman", "rial"}:
            raise DumpError("money_unit_required")
        try:
            if len(value) > 100:
                raise DumpError("invalid_money")
            amount = Decimal(value)
            divisor = 10 if source_unit == "rial" else 1
            if (
                not amount.is_finite()
                or amount < 0
                or amount > 9223372036854775807 * divisor
                or amount != amount.to_integral_value()
            ):
                raise DumpError("invalid_money")
            # Decimal division obeys ambient precision and can round away a
            # fraction. Bound the source integer first, then convert exactly.
            integer = int(amount)
            if integer % divisor:
                raise DumpError("invalid_money")
            return integer // divisor
        except (InvalidOperation, ValueError, OverflowError) as exc:
            raise DumpError("invalid_money") from exc


def _statements(source: str) -> Iterator[str]:
    buffer: list[str] = []
    quote = ""
    escaped = False
    index = 0
    while index < len(source):
        char = source[index]
        if quote:
            buffer.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                if index + 1 < len(source) and source[index + 1] == quote:
                    buffer.append(quote)
                    index += 1
                else:
                    quote = ""
        elif source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                raise DumpError("unterminated_comment")
            buffer.append(" ")
            index = end + 1
        elif char == "#" or (
            source.startswith("--", index) and source[index + 2 : index + 3].isspace()
        ):
            end = source.find("\n", index)
            index = len(source) if end < 0 else end
            buffer.append("\n")
        elif char in "'\"`":
            quote = char
            buffer.append(char)
        elif char == ";":
            statement = "".join(buffer).strip()
            if statement:
                yield statement
            buffer = []
        else:
            buffer.append(char)
        index += 1
    if quote or "".join(buffer).strip():
        raise DumpError("incomplete_statement")


def _rows(source: str) -> Iterator[list[str | None]]:
    index = 0
    size = len(source)

    def whitespace() -> None:
        nonlocal index
        while index < size and source[index].isspace():
            index += 1

    while True:
        whitespace()
        if index >= size or source[index] != "(":
            raise DumpError("invalid_row")
        index += 1
        row: list[str | None] = []
        while True:
            whitespace()
            if index < size and source[index] == "'":
                index += 1
                chars: list[str] = []
                while index < size:
                    char = source[index]
                    index += 1
                    if char == "\\":
                        if index >= size:
                            raise DumpError("invalid_string")
                        char = source[index]
                        chars.append(
                            {
                                "n": "\n",
                                "r": "\r",
                                "t": "\t",
                                "0": "\0",
                                "b": "\b",
                                "Z": "\x1a",
                            }.get(char, char)
                        )
                        index += 1
                    elif char == "'":
                        if index < size and source[index] == "'":
                            chars.append("'")
                            index += 1
                        else:
                            break
                    else:
                        chars.append(char)
                else:
                    raise DumpError("invalid_string")
                value: str | None = "".join(chars)
            else:
                start = index
                while index < size and source[index] not in ",)":
                    index += 1
                literal = source[start:index].strip()
                if literal.upper() == "NULL":
                    value = None
                elif _INTEGER.fullmatch(literal):
                    value = literal
                else:
                    raise DumpError("unsupported_literal")
            row.append(value)
            whitespace()
            if index >= size or source[index] not in ",)":
                raise DumpError("invalid_row")
            delimiter = source[index]
            index += 1
            if delimiter == ")":
                break
        yield row
        whitespace()
        if index == size:
            return
        if source[index] != ",":
            raise DumpError("invalid_rows")
        index += 1


def read_mirza_dump(source: bytes) -> MirzaDump:
    if len(source) > 64 * 1024 * 1024:
        raise DumpError("dump_too_large")
    try:
        decoded = source.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DumpError("dump_encoding") from exc
    schemas: dict[str, list[str]] = {}
    tables: dict[str, list[dict[str, str | None]]] = {}
    for statement in _statements(decoded):
        create = re.match(
            r"CREATE TABLE(?: IF NOT EXISTS)? `([^`]+)`\s*\((.*)\)", statement, re.S | re.I
        )
        if create and create[1] in _FIELDS:
            name = create[1]
            if name in schemas:
                raise DumpError("duplicate_schema")
            schemas[name] = re.findall(r"^\s*`([^`]+)`\s+", create[2], re.M)
            if len(schemas[name]) != len(set(schemas[name])):
                raise DumpError("duplicate_column")
            tables[name] = []
            continue
        insert = re.match(r"INSERT INTO `([^`]+)`\s+VALUES\s+(.+)\Z", statement, re.S | re.I)
        if insert and insert[1] in _FIELDS:
            name = insert[1]
            if name not in schemas:
                raise DumpError("missing_schema")
            for values in _rows(insert[2]):
                if len(values) != len(schemas[name]):
                    raise DumpError("column_count")
                tables[name].append(
                    {
                        key: value
                        for key, value in zip(schemas[name], values, strict=True)
                        if key in _FIELDS[name]
                    }
                )
        elif re.match(r"INSERT INTO `(?:" + "|".join(_FIELDS) + r")`", statement, re.I):
            raise DumpError("unsupported_insert")
    if "user" not in tables:
        raise DumpError("missing_user_table")
    return MirzaDump(hashlib.sha256(source).hexdigest(), tables)
