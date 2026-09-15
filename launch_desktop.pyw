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

Also silently checks GitHub for a newer version before starting a fresh
server (see check_for_auto_update() below, build order #122) — Mario no
longer has to double-click the separate "URTO Updater" icon at all; this
same "URTO" icon he already uses every day now updates itself first,
automatically, whenever nothing's currently running.
"""

import importlib.machinery
import importlib.util
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
# Kept in sync with urto_updater.pyw's own REPO_ZIP_URL / CLAUDE.md's
# "Branch note" — update both if the branch ever changes.
REPO_RAW_APP_URL = "https://raw.githubusercontent.com/mariobengocheare-maker/urto-git/claude/context-window-dgen3k/app.py"


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


def _remote_version():
    """Reads APP_VERSION straight out of the latest app.py on GitHub — the
    same lightweight regex-on-raw-source-text trick _installed_version()
    already uses locally, just fetched from the branch instead of read off
    disk. No import, no zip download yet, just enough to answer "is there
    something newer" before deciding a real update is even worth running.
    Returns None on ANY failure (no internet, GitHub hiccup, a changed file
    shape) — an update check must never be able to block Mario from simply
    opening the app."""
    try:
        with urllib.request.urlopen(REPO_RAW_APP_URL, timeout=6) as r:
            text = r.read().decode("utf-8", errors="replace")
        m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', text)
        return m.group(1) if m else None
    except Exception:
        return None


def check_for_auto_update():
    """See build order #122 — Mario no longer touches the separate "URTO
    Updater" icon at all; this, the ordinary "URTO" icon he already
    double-clicks every day, now checks GitHub first and silently installs
    a newer version if one exists, before ever starting the server. Only
    ever called when nothing's already up (see main() below) — an update
    must never be applied out from under an active session. The natural
    moment for this is already the common "reopen after auto-close shut
    the last session down" case, so this rarely makes a normal open take
    noticeably longer; when it does have real work to do, `run_update()`'s
    own safety nets (see urto_updater.pyw) mean a failed/interrupted
    attempt always leaves Mario on a fully working previous version, never
    a broken half-installed one."""
    installed = _installed_version()
    remote = _remote_version()
    if not installed or not remote or installed == remote:
        return
    try:
        # importlib.util.spec_from_file_location() can't infer a loader
        # from a ".pyw" extension on its own (it only recognizes the
        # standard suffixes) and silently returns None instead of raising
        # -- caught here as a real bug during testing, not assumed away.
        # An explicit SourceFileLoader sidesteps that entirely.
        updater_path = os.path.join(HERE, "urto_updater.pyw")
        loader = importlib.machinery.SourceFileLoader("urto_updater", updater_path)
        spec = importlib.util.spec_from_file_location("urto_updater", updater_path, loader=loader)
        updater = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(updater)  # module __name__ != "__main__", so its Tkinter UI never opens
        updater.run_update(log=lambda msg: None)
    except Exception:
        pass  # never let a failed auto-update block a normal launch


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
    if not is_up():
        check_for_auto_update()
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
