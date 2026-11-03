"""The piped public launcher must retain an interactive terminal."""

import errno
import os
import pty
import select
import shlex
import signal
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_piped_launcher_downloads_pinned_code_and_keeps_prompts(tmp_path):
    """Without /dev/tty, input() reads EOF from the consumed shell script pipe."""
    urllib = tmp_path / "urllib"
    urllib.mkdir()
    (urllib / "__init__.py").write_text("")
    (urllib / "request.py").write_text(
        "import io\n"
        "def urlopen(url, timeout):\n"
        "    assert url == 'https://raw.githubusercontent.com/yasinmalek82/"
        "neo-vpn-sales-bot-public/test/installer-cca9b65702ea/scripts/github_install.py'\n"
        "    assert timeout == 60\n"
        '    return io.BytesIO(b"import sys; assert sys.argv == '
        "['neo-install', '--release', 'test/installer-cca9b65702ea', '--no-swap']; "
        "assert input('INSTALLER_READY: ') == 'continue'; "
        "print('DOWNLOADED_INSTALLER_EXECUTED', flush=True); raise SystemExit(7)\")\n"
    )
    pid, terminal = pty.fork()
    if pid == 0:
        os.environ["PYTHONPATH"] = str(tmp_path)
        os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
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
