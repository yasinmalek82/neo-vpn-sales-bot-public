"""`ADMIN_TELEGRAM_IDS=1,2` (the format `.env.example` documents) must boot.

Live: two admin ids killed `python -m bot.main` with a `SettingsError`,
because pydantic-settings JSON-decodes a `list[int]` before our validator.
"""

from pathlib import Path

import pytest

from bot.config import Settings

ID_FIELDS = ("ADMIN_TELEGRAM_IDS", "STORE_OWNER_TELEGRAM_IDS")
CASES = [
    ("123456789,987654321", [123456789, 987654321]),
    ("123456789, 987654321", [123456789, 987654321]),
    ("123456789", [123456789]),
    ("", []),
    ("[1,2]", [1, 2]),
]


def _read(settings: Settings, name: str) -> list[int]:
    return list(getattr(settings, name.lower()))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*ID_FIELDS, "RENEWAL_REMINDER_DAYS", "DEFAULT_PANEL_GROUP_IDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "test-token")


@pytest.mark.parametrize("name", ID_FIELDS)
@pytest.mark.parametrize(("raw", "expected"), CASES)
def test_id_list_from_process_env(
    monkeypatch: pytest.MonkeyPatch, name: str, raw: str, expected: list[int]
) -> None:
    monkeypatch.setenv(name, raw)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert _read(settings, name) == expected


@pytest.mark.parametrize("name", ID_FIELDS)
@pytest.mark.parametrize(("raw", "expected"), CASES)
def test_id_list_from_dotenv_file(tmp_path: Path, name: str, raw: str, expected: list[int]) -> None:
    env_file = tmp_path / "test.env"
    env_file.write_text(f"{name}={raw}\n", encoding="utf-8")
    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]
    assert _read(settings, name) == expected


def test_env_example_boots_as_written(tmp_path: Path) -> None:
    """The documented example file is the contract: it must parse as shipped."""
    example = Path(__file__).resolve().parents[2] / ".env.example"
    env_file = tmp_path / "example.env"
    env_file.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]
    assert settings.admin_telegram_ids == [123456789, 987654321]
    assert settings.renewal_reminder_days == [3, 1]


def test_garbage_id_is_still_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "12,abc")
    with pytest.raises(ValueError):
        Settings(_env_file=None)  # type: ignore[call-arg]
