#!/bin/sh
# Public entry point. Resolve the approved GitHub release at each invocation.
set -eu
# Imports must not add unlisted files to the checksum-protected bundle.
export PYTHONDONTWRITEBYTECODE=1
exec python3 -c '
import json
import re
import sys
import urllib.request

repo = "yasinmalek82/neo-vpn-sales-bot-public"
latest = urllib.request.Request(
    "https://api.github.com/repos/" + repo + "/releases/latest",
    headers={"Accept": "application/vnd.github+json", "Cache-Control": "no-cache"},
)
try:
    with urllib.request.urlopen(latest, timeout=60) as response:
        metadata = json.loads(response.read(1024 * 1024 + 1))
    release = metadata.get("tag_name") if isinstance(metadata, dict) else None
    if (
        not isinstance(release, str)
        or not re.fullmatch(r"(?:v[0-9]+\.[0-9]+\.[0-9]+|test/installer-[0-9a-f]{12})", release)
        or metadata.get("draft") is not False
        or metadata.get("prerelease") is not False
    ):
        raise ValueError("unexpected release metadata")
except (OSError, ValueError):
    sys.exit("Could not resolve the latest published NEO installer. Try again later.")
url = "https://raw.githubusercontent.com/yasinmalek82/neo-vpn-sales-bot-public/" + release + "/scripts/github_install.py"
with urllib.request.urlopen(url, timeout=60) as response:
    source = response.read()
sys.argv = ["neo-install", "--release", release, *sys.argv[1:]]
exec(compile(source, "neo-install", "exec"), {"__name__": "__main__"})
' "$@" </dev/tty
