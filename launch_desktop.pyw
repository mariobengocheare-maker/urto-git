"""
Double-click entry point for URTO on Windows — starts the Flask server
silently in the background (no console window, since .pyw files run under
pythonw.exe) and opens it in your default browser.

If URTO is already running (e.g. you double-clicked the icon twice, or a
previous session is still up), this just opens the browser tab instead of
starting a second server.
"""

import os
import socket
import subprocess
import sys
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
HOST, PORT = "127.0.0.1", 5000
URL = f"http://{HOST}:{PORT}"


def is_up():
    try:
        with socket.create_connection((HOST, PORT), timeout=0.5):
            return True
    except OSError:
        return False


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
        start_server()
        # Give the server a few seconds to come up before opening the browser.
        for _ in range(30):
            if is_up():
                break
            time.sleep(0.5)
    webbrowser.open(URL)


if __name__ == "__main__":
    main()
