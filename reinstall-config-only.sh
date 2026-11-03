#!/bin/sh
# Recover the original, configuration-only test snapshot without deleting data.
set -eu
export PYTHONDONTWRITEBYTECODE=1

python3 - <<'PY_RECOVER'
import datetime
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

target = Path('/opt/neo-test')

def stop(message):
    print('Recovery: ' + message, file=sys.stderr)
    raise SystemExit(1)

def docker_names(*args):
    try:
        result = subprocess.run(['docker', *args], capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        stop('Could not inspect Docker. Configuration was left in place.')
    if result.returncode:
        stop('Docker inspection failed. Configuration was left in place.')
    return result.stdout.splitlines()

if sys.platform != 'linux' or os.geteuid() != 0:
    stop('Run with sudo on the Ubuntu test server.')
try:
    release = Path('/etc/os-release').read_text()
except OSError:
    stop('Could not verify the Ubuntu release.')
if not re.search(r'^ID=(?:ubuntu|"ubuntu")$', release, re.M) or not re.search(r'^VERSION_ID="?(22\.04|24\.04)"?$', release, re.M):
    stop('Ubuntu 22.04 or 24.04 is required; configuration was left in place.')
if os.uname().machine not in {'x86_64', 'amd64'}:
    stop('An amd64 server is required; configuration was left in place.')

fd = os.open('/opt/.neo-test-install.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX)
    labelled_containers = docker_names('ps', '-a', '--filter', 'label=com.docker.compose.project=neo-test', '--format', '{{.Names}}')
    labelled_volumes = docker_names('volume', 'ls', '--filter', 'label=com.docker.compose.project=neo-test', '--format', '{{.Name}}')
    named_containers = docker_names('ps', '-a', '--format', '{{.Names}}')
    named_volumes = docker_names('volume', 'ls', '--format', '{{.Name}}')
    known_names = [name for name in [*named_containers, *named_volumes] if name.startswith(('neo-test_', 'neo-test-'))]
    if labelled_containers or labelled_volumes or known_names:
        stop('Existing neo-test containers or volumes were found. This configuration-only recovery cannot continue; use a backed-up upgrade path.')
    if target.is_symlink():
        stop('Installation directory is a symlink; refusing recovery.')
    if target.exists():
        for name in ('manifest.json', 'compose.yml', '.env', 'source'):
            path = target / name
            if path.is_symlink() or not path.exists():
                stop('Installation metadata is incomplete or contains a link; refusing recovery.')
        try:
            manifest = json.loads((target / 'manifest.json').read_text())
            compose_hash = hashlib.sha256((target / 'compose.yml').read_bytes()).hexdigest()
        except (OSError, ValueError):
            stop('Could not validate the old configuration; refusing recovery.')
        if not isinstance(manifest, dict) or manifest.get('schema_version') != 1 or manifest.get('platform') != 'linux/amd64' or manifest.get('source_tag') != 'test/installer-04a358f7f7a7':
            stop('This helper only handles the original 04a358f7f7a7 test snapshot.')
        if compose_hash != '5c1a7cf2babe4de3b95560ca3a2c180305e0b42c129979878088366d73ce1a18':
            stop('Compose configuration has been modified; refusing automatic recovery.')
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup = target.with_name('neo-test-config-backup-' + stamp + '-' + uuid.uuid4().hex[:8])
        target.rename(backup)
        backup.chmod(0o700)
        print('Old configuration archived privately at: ' + str(backup))
    else:
        print('No saved installation directory; continuing with fresh setup.')
finally:
    os.close(fd)
PY_RECOVER

curl -fsSL https://raw.githubusercontent.com/yasinmalek82/neo-vpn-sales-bot-public/main/setup.sh | /bin/sh
