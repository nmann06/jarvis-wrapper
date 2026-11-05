"""Keeping the window going when its terminal stops reading, and finding out why if it stops anyway.

Textual hands what it draws to a thread that writes it to the terminal, and lets 30 writes wait
for it; the 31st blocks the whole window until the terminal reads again: no keys, clicks,
scrolling or repainting, and Claude Code's messages wait too. Terminals stop reading more often
than you'd think: an ssh link that stalls (wifi, Tailscale finding a new route, the laptop
asleep) once its buffers fill, and a Windows console window (PowerShell's own, not Windows
Terminal) for as long as a click has text selected in it. Output takes the wait out: past the
backlog, what's drawn is left out, and the whole screen is drawn again once the terminal reads.
On Windows, install() also turns the console's QuickEdit off, so a click goes to Jarvis instead
of starting a selection, and gives Textual's timers a sleep that doesn't hold a thread (see
steady_timers).

Watchdog writes down why, should the window stop all the same: if it hasn't drawn for STALL
seconds, what every thread and task was doing goes to LOG (and `kill -USR1 <pid>` puts it there
on demand). So do stretches when the terminal wasn't reading.
"""
import asyncio
import faulthandler
import os
import queue
import signal
import sys
import threading
import time

from textual.drivers._writer_thread import WriterThread

LOG = os.path.expanduser("~/.claude/jarvis-window-hangs.log")
LOG_LIMIT = 2 * 1024 * 1024  # past this it starts again (the last one is kept as .old)
STALL = 8.0                  # seconds without drawing before the watchdog writes it down
TERMINAL_STALL = 3.0         # seconds the terminal can go without reading before that's written down

drained_listener = None  # called (from the writer thread) when the terminal catches up: Watchdog.drained


class Output(WriterThread):
    """Textual's writer thread, except that a full backlog leaves writes out instead of waiting.
    When the terminal has caught up after some were left out, drained_listener(seconds) is called
    (from this thread) with how long it was behind; the screen then needs drawing again in full.
    (Textual makes a new one each time the window comes back from suspend(): /claude, ctrl+g.)"""

    def __init__(self, file):
        super().__init__(file)
        self.lossy = True    # off as the window closes, so the terminal gets put back as it was
        self.dropped = 0
        self.behind_since = None

    def write(self, text):
        if not self.lossy:
            self._queue.put(text)
            return
        try:
            self._queue.put_nowait(text)
        except queue.Full:
            if not self.dropped:
                self.behind_since = time.monotonic()
            self.dropped += 1

    def run(self):
        write, flush = self._file.write, self._file.flush
        get, qsize = self._queue.get, self._queue.qsize
        while True:
            text = get()
            if text is None:
                break
            write(text)
            if qsize() == 0:
                flush()
                if self.dropped:
                    behind = time.monotonic() - (self.behind_since or time.monotonic())
                    self.dropped, self.behind_since = 0, None
                    listener = drained_listener
                    if listener is not None:
                        listener(behind)
        flush()


def steady_timers():
    """On Windows, Textual sleeps each timer's wait out on a thread of asyncio's default pool
    (_win_sleep.py, for finer timing than asyncio.sleep's 15ms), for as long as the wait is. The
    pool has cpu_count + 4 threads (20 on the laptop); once that many timers wait at once, every
    other timer waits for a free thread, including the ones that redraw the screen, so the
    window froze with nothing busy: all 20 threads in wait_inner, for the hours until a usage
    limit's reset. asyncio.sleep holds no thread, and 15ms is plenty for an 80ms tick."""
    from textual import timer
    timer.sleep = asyncio.sleep


def install():
    """Have Textual's drivers write through Output (before the app runs), and on Windows keep
    QuickEdit off while the window is open and Textual's timers off the thread pool."""
    if os.name != "nt":
        from textual.drivers import linux_driver  # imports termios, which Windows hasn't got
        linux_driver.WriterThread = Output
    else:
        steady_timers()
        from textual.drivers import win32, windows_driver
        windows_driver.WriterThread = Output
        enable = win32.enable_application_mode

        def enable_application_mode():
            restore = enable()  # puts QuickEdit back as it was, with the rest of the console's modes
            # Without ENABLE_EXTENDED_FLAGS, Textual's mode leaves QuickEdit as it was.
            win32.set_console_mode(sys.__stdin__, win32.ENABLE_VIRTUAL_TERMINAL_INPUT | win32.ENABLE_EXTENDED_FLAGS)
            return restore

        win32.enable_application_mode = enable_application_mode


def open_log():
    try:
        if os.path.getsize(LOG) > LOG_LIMIT:
            os.replace(LOG, LOG + ".old")
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        return open(LOG, "a", buffering=1)
    except OSError:
        return None


class Watchdog:
    """Watches the window's own heartbeat (beat(), from its 80ms tick) from a thread of its own."""

    def __init__(self, app, stall=STALL):
        self.app = app
        self.stall = stall
        self.loop = None
        self.last = time.monotonic()
        self.stuck_since = None
        self.paused = False  # the window handed the terminal over (suspend): it isn't meant to draw
        self.log = None
        self.stopped = threading.Event()

    def start(self):
        self.loop = asyncio.get_running_loop()
        self.log = open_log()
        if self.log is None:
            return
        if hasattr(signal, "SIGUSR1"):
            try:
                faulthandler.register(signal.SIGUSR1, file=self.log, all_threads=True)
            except (RuntimeError, ValueError, OSError):
                pass
        global drained_listener
        drained_listener = self.drained
        threading.Thread(target=self.watch, name="jarvis-watchdog", daemon=True).start()

    def stop(self):
        global drained_listener
        self.stopped.set()
        drained_listener = None
        output = self.output()
        if output is not None:
            output.lossy = False
        if hasattr(signal, "SIGUSR1") and self.log is not None:
            try:
                faulthandler.unregister(signal.SIGUSR1)
            except (RuntimeError, ValueError):
                pass

    def beat(self):
        self.last = time.monotonic()

    def output(self):
        output = getattr(getattr(self.app, "_driver", None), "_writer_thread", None)
        return output if isinstance(output, Output) else None

    def write(self, text):
        if self.log is not None:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            self.log.write(f"{stamp} [pid {os.getpid()}, {os.path.basename(self.app.cwd)}] {text}\n")

    def watch(self):
        woke = time.monotonic()
        while not self.stopped.wait(1.0):
            now = time.monotonic()
            if now - woke > 3 or self.paused:  # the whole process was stopped (ctrl+z), or handed over
                self.last = now
            woke = now
            late = now - self.last
            if self.stuck_since is None and late >= self.stall:
                self.stuck_since = self.last
                self.report(late)
            elif self.stuck_since is not None and late < self.stall:
                took = self.last - self.stuck_since
                self.stuck_since = None
                self.write(f"the window is going again after about {took:.0f}s")
                self.loop.call_soon_threadsafe(self.app.recovered, took)

    def report(self, late):
        output = self.output()
        backlog = f"{output._queue.qsize()} writes waiting, {output.dropped} left out" if output else "?"
        self.write(f"the window hasn't drawn for {late:.0f}s (to the terminal: {backlog}; "
                   f"batched updates open: {getattr(self.app, '_batch_count', '?')}). Threads:")
        faulthandler.dump_traceback(file=self.log, all_threads=True)
        # If the loop itself is free (the window's queue is held up), this shows what by;
        # if it's blocked, it runs once it's free again.
        self.loop.call_soon_threadsafe(self.dump_tasks)

    def dump_tasks(self):
        if self.log is None:
            return
        self.write("asyncio tasks:")
        for task in asyncio.all_tasks(self.loop):
            try:
                task.print_stack(limit=8, file=self.log)
            except Exception as e:  # a task finishing as it's printed
                self.log.write(f"  ({e!r})\n")
        self.log.write("\n")

    def drained(self, behind):
        """The terminal read again after Jarvis got ahead of it (from the writer thread)."""
        if behind >= TERMINAL_STALL:
            self.write(f"the terminal didn't read for {behind:.0f}s; the window carried on and redrew")
        self.loop.call_soon_threadsafe(self.app.redraw_all)
