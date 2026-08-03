"""
Double-click entry point for URTO on Windows — starts the Flask server
silently in the background (no console window, since .pyw files run under
pythonw.exe) and opens it in your default browser.

If URTO is already running (e.g. you double-clicked the icon twice, or a
previous session is still up), this just opens the browser tab instead of
starting a second server — UNLESS that running server is a stale process
left over from before an update (see check_stale_version() below), in which
case it kills it and starts fresh so double-clicking this icon always ends
up on the version actually installed on disk, not whatever happened to
still be running.
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
HOST, PORT = "127.0.0.1", 5000
URL = f"http://{HOST}:{PORT}"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def is_up():
    try:
        with socket.create_connection((HOST, PORT), timeout=0.5):
            return True
    except OSError:
        return False


def _installed_version():
    """Reads APP_VERSION straight out of app.py's own source text — avoids
    importing app.py itself just to check a version string (that would drag
    in Flask/Playwright for no reason here)."""
    try:
        text = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()
        m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', text)
        return m.group(1) if m else None
    except OSError:
        return None


def _running_version():
    try:
        with urllib.request.urlopen(f"{URL}/api/version", timeout=2) as r:
            return json.loads(r.read()).get("version")
    except Exception:
        return None  # a pre-#version-endpoint build, or genuinely not up


def _kill_stale_server():
    """Same command-line-matched kill urto_updater.pyw uses — never by bare
    process name, so an unrelated Python program (or Wardrobe's own app.py)
    is never touched."""
    target = os.path.join(HERE, "app.py").replace("'", "''")
    ps_cmd = (
        f"Get-CimInstance Win32_Process | "
        f"Where-Object {{ $_.CommandLine -like '*{target}*' }} | "
        f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, timeout=15, creationflags=NO_WINDOW,
        )
    except Exception:
        pass
    for _ in range(10):
        if not is_up():
            return
        time.sleep(0.5)


def check_stale_version():
    """If something's already listening on the port but it's reporting a
    different version than what's actually on disk right now, an update
    installed new files while the OLD process kept running (the updater's
    own kill step can fail silently — see urto_updater.pyw). Rather than
    leaving Mario stuck looking at old code with no idea why, detect the
    mismatch here too and self-heal."""
    if not is_up():
        return
    installed = _installed_version()
    running = _running_version()
    if installed and running and installed != running:
        _kill_stale_server()


def start_server():
    # sys.executable here is pythonw.exe (since this .pyw was launched that
    # way), so the spawned app.py inherits the same "no console window"
    # behavior. DETACHED_PROCESS + CREATE_NO_WINDOW keep it running after
    # this launcher script exits, with no window ever appearing.
    creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [sys.executable, os.path.join(HERE, "app.py")],
        cwd=HERE,
        creationflags=creationflags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )


def main():
    check_stale_version()
    if not is_up():
        start_server()
        # Give the server a few seconds to come up before opening the browser.
        for _ in range(30):
            if is_up():
                break
            time.sleep(0.5)
    webbrowser.open(URL)


if __name__ == "__main__":
    main()
