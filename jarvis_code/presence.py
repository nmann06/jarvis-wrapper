"""What each open window is doing, for whatever on the machine needs to know: a file per window
in DIR, rewritten every few seconds and removed as it closes.

hyde's nightly restart (~/jarvis/deploy/nightly_reboot.py) reads them: it restarts only when
every window says it's idle, and reopens each one on its conversation after the restart. A
window with no file, or one that hasn't been rewritten lately, counts as busy.

    {"pid": 1234, "busy": false, "session": "<session id>", "cwd": "/home/nate",
     "tmux": "%0", "updated": 1730700000.0}
"""
import json
import os
import tempfile
import time

DIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), "jarvis-windows")
EVERY = 5.0  # seconds between rewrites


def path(pid=None):
    return os.path.join(DIR, f"{pid or os.getpid()}.json")


def busy(app):
    """Mid-turn, or about to carry on by itself: the window or any of its /manager threads."""
    if app.busy or (app.keep_going and app.stalled):
        return True
    manager = app.manager
    return bool(manager and any(t.busy or t.dialog is not None for t in manager.threads))


def write(app):
    state = {"pid": os.getpid(), "busy": busy(app), "session": app.session_id, "cwd": app.cwd,
             "tmux": os.environ.get("TMUX_PANE"), "updated": time.time()}
    try:
        os.makedirs(DIR, exist_ok=True)
        tmp = path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, path())
    except OSError:
        pass


def clear():
    try:
        os.remove(path())
    except OSError:
        pass
