"""watchdog.py: a terminal that stops reading leaves out what's drawn instead of freezing the
window, and says when it has caught up; and the times shown and logged."""

import io
import re
import threading
import time

from jarvis_code import watchdog
from jarvis_code.engine import Engine
from jarvis_code.widgets import clock


class StalledTerminal(io.StringIO):
    """A terminal that takes nothing until it's let go."""

    def __init__(self):
        super().__init__()
        self.reading = threading.Event()

    def write(self, text):
        self.reading.wait()
        return super().write(text)


def test_a_stalled_terminal_leaves_writes_out_instead_of_blocking(monkeypatch):
    caught_up = []
    monkeypatch.setattr(watchdog, "drained_listener", caught_up.append)
    terminal = StalledTerminal()
    output = watchdog.Output(terminal)
    output.start()
    started = time.monotonic()
    for n in range(200):
        output.write(f"frame {n}\n")  # Textual's own writer blocks on the 31st or so
    assert time.monotonic() - started < 1
    assert output.dropped > 0
    terminal.reading.set()
    deadline = time.monotonic() + 5
    while not caught_up and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(caught_up) == 1 and caught_up[0] >= 0  # told once, so the screen is drawn again
    assert output.dropped == 0
    assert terminal.getvalue().startswith("frame 0\n")
    output.lossy = False  # as the window closes: nothing is left out
    output.write("restore the terminal\n")
    output.stop()
    assert terminal.getvalue().endswith("restore the terminal\n")


def test_clock_shows_the_day_only_when_it_isnt_today():
    assert re.fullmatch(r"\d{1,2}:\d\d [ap]m", clock())
    assert re.fullmatch(r"\d{1,2}:\d\d [ap]m", clock(time.time()))
    assert re.fullmatch(r"Oct 28 \d{1,2}:\d\d [ap]m", clock("2026-10-28T19:20:00.000Z"))
    assert clock("not a time") == ""


def test_the_engine_log_times_each_line(tmp_path):
    path = tmp_path / "engine.log"
    engine = Engine(str(tmp_path), None, None, log_path=str(path))
    engine._trace(">", '{"type": "user"}\n')
    engine._trace("!", "a warning")
    engine.log.close()
    lines = path.read_text().splitlines()
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} > \{\"type\": \"user\"\}", lines[0])
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} ! a warning", lines[1])
