"""The Docker build context must not carry secrets, data or history into the image.

``docker/Dockerfile`` does ``COPY . .`` with the repo root as context, so without a
``.dockerignore`` the image held ``.env``, ``data/bot.db``, ``backups/`` and ``.git``.
Runtime env comes from compose's ``env_file``, so the image does not need the file.
"""

from __future__ import annotations

from pathlib import Path

DOCKERIGNORE = Path(__file__).resolve().parents[2] / ".dockerignore"

MUST_BE_IGNORED = {".env", ".env.*", ".git", ".venv", "data", "backups", "logs", "*.log"}
# What the image runs from (Dockerfile chmods the entrypoint, backup and healthcheck).
MUST_STAY_IN_IMAGE = {
    "bot",
    "services",
    "api_client",
    "database",
    "scheduler",
    "alembic",
    "alembic.ini",
    "config",
    "assets",
    "scripts",
    "docker",
    "requirements.txt",
    "pyproject.toml",
}


def _entries() -> set[str]:
    assert DOCKERIGNORE.is_file(), ".dockerignore is missing from the repo root"
    lines = (line.strip() for line in DOCKERIGNORE.read_text().splitlines())
    return {line.rstrip("/") for line in lines if line and not line.startswith("#")}


def test_secrets_data_and_history_are_kept_out_of_the_image() -> None:
    assert _entries() >= MUST_BE_IGNORED


def test_nothing_the_image_runs_from_is_ignored() -> None:
    assert not (MUST_STAY_IN_IMAGE & _entries())


def test_serverless_experiment_credentials_are_outside_the_image() -> None:
    assert "experiments" in _entries()
