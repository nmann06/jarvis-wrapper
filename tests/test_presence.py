import json
import os
from types import SimpleNamespace

from jarvis_code import presence


def window(**kw):
    state = dict(busy=False, keep_going=True, stalled=None, manager=None, session_id="abc", cwd="/tmp")
    state.update(kw)
    return SimpleNamespace(**state)


def thread(status="finished", dialog=None):
    return SimpleNamespace(busy=status in ("starting", "working"), dialog=dialog)


def test_idle_window():
    assert not presence.busy(window())


def test_busy_turn():
    assert presence.busy(window(busy=True))


def test_waiting_out_the_usage_limit():
    assert presence.busy(window(stalled=1730700000))
    assert not presence.busy(window(stalled=1730700000, keep_going=False))


def test_manager_threads():
    assert not presence.busy(window(manager=SimpleNamespace(threads=[thread(), thread("stopped")])))
    assert presence.busy(window(manager=SimpleNamespace(threads=[thread(), thread("working")])))
    assert presence.busy(window(manager=SimpleNamespace(threads=[thread(dialog=object())])))


def test_write_and_clear():
    presence.write(window(session_id="s1", cwd="/home"))
    with open(presence.path()) as f:
        state = json.load(f)
    assert (state["pid"], state["busy"], state["session"], state["cwd"]) == (os.getpid(), False, "s1", "/home")
    presence.clear()
    assert not os.path.exists(presence.path())
