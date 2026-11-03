"""PostgreSQL backup checks use the packaged CLI, with no optional Python driver."""

from pathlib import Path

import pytest

from services.db_backup import BackupError, count_core_rows, target_has_tables


@pytest.fixture
async def psql(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    executable = tmp_path / "psql"
    executable.write_text(
        "#!/bin/sh\n"
        '[ "$PGPASSWORD" = "test-password" ] || exit 4\n'
        'printf "%s\\n" "$*" > "$PSQL_ARGS"\n'
        '[ "${PSQL_FAIL:-0}" = 0 ] || exit 2\n'
        'printf "%s\\n" "$PSQL_RESULT"\n'
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PSQL_ARGS", str(tmp_path / "args"))
    return tmp_path / "args"


URL = "postgresql+asyncpg://test:test-password@127.0.0.1:5432/synthetic"


def test_postgres_core_counts_do_not_require_an_optional_driver(psql, monkeypatch):
    monkeypatch.setenv("PSQL_RESULT", "2|1|3")
    assert count_core_rows(URL) == (2, 1, 3)
    args = psql.read_text()
    assert "test-password" not in args
    assert "ON_ERROR_STOP=1" in args


@pytest.mark.parametrize("result,want", [("0", False), ("3", True)])
def test_target_check_refuses_nonempty_postgres(psql, monkeypatch, result, want):
    monkeypatch.setenv("PSQL_RESULT", result)
    assert target_has_tables(URL) is want


@pytest.mark.parametrize("failure,result", [("2", ""), ("0", "not-counts")])
def test_failed_or_malformed_count_is_never_treated_as_an_empty_target(
    psql, monkeypatch, failure, result
):
    monkeypatch.setenv("PSQL_FAIL", failure)
    monkeypatch.setenv("PSQL_RESULT", result)
    with pytest.raises(BackupError):
        target_has_tables(URL)
