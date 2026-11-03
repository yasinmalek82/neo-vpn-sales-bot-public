"""Offline import input is data, never executable SQL or inferred money."""

from decimal import Decimal, localcontext

import pytest

from services.mirza_source import DumpError, read_mirza_dump


def export(values: str) -> str:
    return (
        "CREATE TABLE `user` (\n  `id` bigint NOT NULL,\n  `Balance` text,\n"
        "  `username` text\n);\n"
        f"INSERT INTO `user` VALUES {values};\n"
    )


def test_read_preserves_quoted_separators_null_and_mysql_escapes() -> None:
    parsed = read_mirza_dump(export("(1,'1200','علی; (A), \\'B\\'\\n'),(2,NULL,'NULL')").encode())
    assert parsed.tables["user"] == [
        {"id": "1", "Balance": "1200", "username": "علی; (A), 'B'\n"},
        {"id": "2", "Balance": None, "username": "NULL"},
    ]
    assert parsed.money("1200", source_unit="toman") == 1200
    assert parsed.money("12000", source_unit="rial") == 1200


@pytest.mark.parametrize("value", ["1.1", "-1", "NaN", "Infinity", "1e99999"])
def test_invalid_or_fractional_money_is_rejected_without_rounding(value: str) -> None:
    parsed = read_mirza_dump(export("(1,'0','test')").encode())
    with pytest.raises(DumpError):
        parsed.money(value, source_unit="toman")


def test_money_requires_explicit_unit_and_exact_conversion() -> None:
    parsed = read_mirza_dump(export("(1,'0','test')").encode())
    with pytest.raises(DumpError, match="money_unit"):
        parsed.money("10", source_unit=None)
    with pytest.raises(DumpError):
        parsed.money("11", source_unit="rial")
    assert parsed.money(str(Decimal("17524792")), source_unit="toman") == 17524792


@pytest.mark.parametrize(
    "value", ["9.999999999999999999999999999999", "10000000000000000000.00000000001"]
)
def test_rial_conversion_cannot_round_away_a_fraction(value: str) -> None:
    parsed = read_mirza_dump(export("(1,'0','test')").encode())
    with pytest.raises(DumpError, match="invalid_money"):
        parsed.money(value, source_unit="rial")


def test_currency_conversion_is_independent_of_decimal_context() -> None:
    parsed = read_mirza_dump(export("(1,'0','test')").encode())
    with localcontext() as context:
        context.prec = 2
        assert parsed.money("125000", source_unit="rial") == 12500


@pytest.mark.parametrize(
    "values", ["(1,'3')", "(1,'3','unterminated)", "(1,evil(),'test')", "(1,'3','test'),"]
)
def test_malformed_rows_and_sql_expressions_are_rejected(values: str) -> None:
    with pytest.raises(DumpError):
        read_mirza_dump(export(values).encode())


def test_panel_credentials_and_unrelated_runtime_tables_are_not_retained() -> None:
    source = export("(1,'0','test')") + (
        "CREATE TABLE `marzban_panel` (\n `name_panel` text,\n `url_panel` text,\n"
        " `password_panel` text\n);\n"
        "INSERT INTO `marzban_panel` VALUES ('old','https://panel.invalid','secret-value');\n"
        "CREATE TABLE `settings` (\n `token` text\n);\n"
        "INSERT INTO `settings` VALUES ('bot-secret');\n"
    )
    parsed = read_mirza_dump(source.encode())
    assert parsed.tables["marzban_panel"] == [{"name_panel": "old"}]
    assert "settings" not in parsed.tables
    assert "secret-value" not in repr(parsed)
    assert "bot-secret" not in repr(parsed)
