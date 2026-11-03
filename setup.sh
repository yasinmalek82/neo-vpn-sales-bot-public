#!/bin/sh
# Public entry point. Keep this release pin until a replacement bundle is verified.
set -eu
# Imports must not add unlisted files to the checksum-protected bundle.
export PYTHONDONTWRITEBYTECODE=1
exec python3 -c '
import sys
import urllib.request
release = "test/installer-cca9b65702ea"
url = "https://raw.githubusercontent.com/yasinmalek82/neo-vpn-sales-bot-public/" + release + "/scripts/github_install.py"
with urllib.request.urlopen(url, timeout=60) as response:
    source = response.read()
sys.argv = ["neo-install", "--release", release, *sys.argv[1:]]
exec(compile(source, "neo-install", "exec"), {"__name__": "__main__"})
' "$@" </dev/tty
