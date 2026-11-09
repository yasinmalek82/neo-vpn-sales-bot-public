"""The piped public launcher must retain an interactive terminal."""

import errno
import os
import pty
import select
import shlex
import signal
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("tag", ["test/installer-123456abcdef", "v1.0.0"])
def test_piped_launcher_resolves_latest_and_keeps_prompts(tmp_path, tag):
    """Without /dev/tty, input() reads EOF from the consumed shell script pipe."""
    (tmp_path / "sitecustomize.py").write_text(
        "import io, json, urllib.request\n"
        f"tag = {tag!r}\n"
        "def urlopen(url, timeout):\n"
        "    assert timeout == 60\n"
        "    if isinstance(url, urllib.request.Request):\n"
        "        assert url.full_url.endswith('/neo-vpn-sales-bot-public/releases/latest')\n"
        "        assert url.get_header('Cache-control') == 'no-cache'\n"
        "        return io.BytesIO(json.dumps(dict(tag_name=tag, draft=False, "
        "prerelease=False)).encode())\n"
        "    assert url == 'https://raw.githubusercontent.com/yasinmalek82/"
        "neo-vpn-sales-bot-public/' + tag + '/scripts/github_install.py'\n"
        '    return io.BytesIO(b"import sys; assert sys.argv == '
        f"['neo-install', '--release', '{tag}', '--no-swap']; "
        "assert input('INSTALLER_READY: ') == 'continue'; "
        "print('DOWNLOADED_INSTALLER_EXECUTED', flush=True); raise SystemExit(7)\")\n"
        "urllib.request.urlopen = urlopen\n"
    )
    pid, terminal = pty.fork()
    if pid == 0:
        os.environ["PYTHONPATH"] = str(tmp_path)
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        os.environ.pop("PYTHONPYCACHEPREFIX", None)
        command = f"cat {shlex.quote(str(ROOT / 'setup.sh'))} | /bin/sh -s -- --no-swap"
        os.execl("/bin/sh", "sh", "-c", command)
    output = b""
    status = None
    try:
        deadline = time.monotonic() + 15
        answered = False
        while time.monotonic() < deadline:
            if select.select([terminal], [], [], 0.1)[0]:
                try:
                    chunk = os.read(terminal, 8192)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    chunk = b""
                output += chunk
                if b"INSTALLER_READY:" in output and not answered:
                    os.write(terminal, b"continue\n")
                    answered = True
                if not chunk:
                    # PTY EOF may precede the child's waitable exit status.
                    time.sleep(0.01)
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                pid = None
                break
        # A child may exit between writing output and waitpid. Drain the PTY
        # after reaping it so scheduler timing cannot hide its last message.
        if pid is None:
            while select.select([terminal], [], [], 0)[0]:
                try:
                    chunk = os.read(terminal, 8192)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
        assert b"DOWNLOADED_INSTALLER_EXECUTED" in output, output.decode(errors="replace")
        assert pid is None, "Launcher did not exit"
        assert os.waitstatus_to_exitcode(status) == 7
        assert not list(tmp_path.rglob("__pycache__"))
    finally:
        if pid is not None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(terminal)
