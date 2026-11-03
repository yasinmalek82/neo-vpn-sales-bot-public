"""The app's backup tools must follow the deployed PostgreSQL major version.

An unversioned Debian client produced a PG17 dump which failed to restore on
our PG16 target (transaction_timeout). Actual image restore is a release gate;
this configuration guard catches version drift before an expensive build.
"""

import re
from pathlib import Path


def test_runtime_backup_client_major_matches_both_compose_postgres_services():
    root = Path(__file__).resolve().parents[2]
    compose = (root / "docker/docker-compose.yml").read_text()
    server_majors = re.findall(r"image:\s*postgres:(\d+)", compose)
    assert len(server_majors) == 2
    dockerfile = (root / "docker/Dockerfile").read_text()
    client_major = re.search(r"^ARG POSTGRES_MAJOR=(\d+)$", dockerfile, re.MULTILINE)
    assert client_major, "Declare the backup client major; never use distro's latest client"
    assert set(server_majors) == {client_major.group(1)}
    assert "postgresql-client-${POSTGRES_MAJOR}" in dockerfile
