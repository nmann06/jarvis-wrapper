"""/jarvis settings: the settings of Jarvis itself (the voice assistant in ~/jarvis on hyde), on
a page of their own like the conversations: JARVIS on top, the categories in a row, a search
box, and the settings of the category, or every match while searching.

The settings come from Jarvis (`python -m jarvis.settings list`, which knows each one's
category, kind, default and what it does) and are changed through it (`set KEY VALUE`), so this
only draws and takes keys. On hyde it runs ~/jarvis's own Python; elsewhere (the Windows
laptop) it runs there over ssh, to the host in JARVIS_HYDE. Secrets are never sent here, only
whether they're set. Most changes apply once Jarvis restarts (ctrl+r, or asked on leaving);
do not disturb applies at once.
"""

import asyncio
import json
import os
import shlex
import sys
from pathlib import Path

from rich.console import Group
from rich.text import Text
from textual.screen import Screen

from . import theme
from .panels import Panel, dim
from .widgets import Splash

JARVIS = Path.home() / "jarvis"
ALL = "All"


def command(*args):
    """argv for `python -m jarvis.settings args` and the folder to run it in, or None if
    there's no Jarvis here and no JARVIS_HYDE to reach it on."""
    python = JARVIS / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if python.exists():
        return [str(python), "-m", "jarvis.settings", *args], str(JARVIS)
    host = os.environ.get("JARVIS_HYDE")
    if host:
        remote = "cd ~/jarvis && .venv/bin/python -m jarvis.settings " + " ".join(map(shlex.quote, args))
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, remote], None
    return None


async def run(*args, timeout=30):
    """What jarvis.settings printed, as a dict; raises RuntimeError with its error."""
    found = command(*args)
    if found is None:
        raise RuntimeError("Jarvis isn't on this computer (~/jarvis), and JARVIS_HYDE (its ssh host) isn't set")
    argv, cwd = found
    proc = await asyncio.create_subprocess_exec(*argv, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("Jarvis took too long to answer") from None
    try:
        data = json.loads(out.decode(errors="replace").strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError(err.decode(errors="replace").strip()[-300:] or f"exit code {proc.returncode}") from None
    if "error" in data:
        raise RuntimeError(data["error"])
    return data


class JarvisSettingsPanel(Panel):
    """The settings, by category. Keys: ←/→ (or tab) change category; typing searches every
    category; ↑/↓ move; Enter or Space changes one (on/off flips, a choice opens its list, a
    number or text opens a line to type in); Delete puts one back to its default; ctrl+r
    restarts Jarvis; Esc goes back a step, then leaves."""

    def __init__(self):
        super().__init__()
        self.data = None
        self.error = None
        self.category = 0
        self.search = ""
        self.selected = 0
        self.choosing = None  # the setting whose choices are open
        self.choice = 0
        self.typing = None  # the setting a value is being typed for
        self.typed = ""
        self.unapplied = False  # changed since Jarvis last started
        self.leaving = False  # asked whether to restart before leaving

    def on_mount(self):
        super().on_mount()
        self.run_worker(self.load(), group="jarvis-settings")

    async def load(self):
        try:
            self.data = await run("list")
            self.error = None
        except Exception as e:
            self.error = str(e) or type(e).__name__
        self.redraw()

    # What's shown -----------------------------------------------------------------------------

    def categories(self):
        return (self.data or {}).get("categories") or []

    def rows(self):
        """The settings shown: the category's, or while searching, every one that matches."""
        settings = (self.data or {}).get("settings") or []
        q = self.search.lower().strip()
        if q:
            return [s for s in settings
                    if q in s["label"].lower() or q in s["key"].lower() or q in (s["about"] or "").lower()
                    or q in s["category"].lower()]
        cats = self.categories()
        if not cats:
            return []
        return [s for s in settings if s["category"] == cats[self.category % len(cats)]]

    @staticmethod
    def shown(s):
        """The value as shown: secrets only as set or not; the default in grey when unset."""
        if s["kind"] == "secret":
            return "set" if s.get("set") else "not set"
        value = s["value"] if s["value"] is not None else s["default"]
        if s["kind"] == "bool":
            value = "on" if str(value).lower() in ("on", "true", "1", "yes") else "off"
        return value if value != "" else "(empty)"

    # Keys -------------------------------------------------------------------------------------

    def press(self, key, char):
        if self.leaving:
            if key == "enter":
                self.run_worker(self.restart(then_leave=True), group="jarvis-settings-save")
            elif key == "escape":
                self.finish()
            return True
        if key == "escape":
            return self.escape()
        if self.choosing:
            return self.choose_key(key)
        if self.typing:
            return self.type_key(key, char)
        rows = self.rows()
        if key in ("left", "shift+tab", "right", "tab"):
            if self.categories() and not self.search:
                self.category = (self.category + (-1 if key in ("left", "shift+tab") else 1)) % len(self.categories())
                self.selected = 0
                self.message = ""
                self.scroller.scroll_home(animate=False)
            return True
        if key == "up":
            self.selected = max(0, self.selected - 1)
        elif key == "down":
            self.selected = min(self.selected + 1, max(0, len(rows) - 1))
        elif key in ("pageup", "pagedown"):
            step = 10 if key == "pagedown" else -10
            self.selected = max(0, min(len(rows) - 1, self.selected + step))
        elif key in ("enter", "space") and rows and not (key == "space" and self.search and char):
            self.change(rows[self.selected])
        elif key == "space" and self.search:
            self.search += " "
        elif key == "delete" and rows:
            self.commit(rows[self.selected], "default")
        elif key == "ctrl+r":
            self.run_worker(self.restart(), group="jarvis-settings-save")
        elif key == "backspace":
            self.search = self.search[:-1]
            self.selected = 0
        elif char and char.isprintable() and key != "space":
            self.search += char
            self.selected = 0
        else:
            return False
        return True

    def escape(self):
        if self.choosing or self.typing:
            self.choosing = self.typing = None
        elif self.search:
            self.search = ""
            self.selected = 0
        elif self.unapplied:
            self.leaving = True
        else:
            self.finish()
        return True

    def choose_key(self, key):
        options = self.choosing["choices"]
        if key == "up":
            self.choice = (self.choice - 1) % len(options)
        elif key == "down":
            self.choice = (self.choice + 1) % len(options)
        elif key == "enter":
            item, self.choosing = self.choosing, None
            self.commit(item, options[self.choice])
        return True

    def type_key(self, key, char):
        if key == "enter":
            item, self.typing = self.typing, None
            self.commit(item, self.typed.strip() or "default")
        elif key == "backspace":
            self.typed = self.typed[:-1]
        elif key == "space":
            self.typed += " "
        elif char and char.isprintable():
            self.typed += char
        return True

    def change(self, s):
        if s["kind"] == "secret":
            self.say(f"{s['label']} can only be changed in ~/jarvis/.env")
        elif s["kind"] == "bool":
            self.commit(s, "off" if self.shown(s) == "on" else "on")
        elif s["kind"] == "choice" and s["choices"]:
            self.choosing = s
            current = self.shown(s)
            self.choice = s["choices"].index(current) if current in s["choices"] else 0
        else:
            self.typing = s
            self.typed = "" if s["value"] is None else str(s["value"])

    def commit(self, s, value):
        if s["kind"] == "secret":
            self.say(f"{s['label']} can only be changed in ~/jarvis/.env")
            return
        self.say(f"Setting {s['label']}…")
        self.run_worker(self.save(s, value), group="jarvis-settings-save")

    async def save(self, s, value):
        try:
            said = await run("set", s["key"], value)
        except Exception as e:
            self.say(f"Couldn't change {s['label']}: {e}")
            return
        self.unapplied = self.unapplied or said.get("restart", False)
        self.message = said.get("message", "")
        await self.load()

    async def restart(self, then_leave=False):
        self.leaving = False
        self.say("Restarting Jarvis…")
        try:
            said = await run("restart", timeout=90)
        except Exception as e:
            self.say(f"Couldn't restart Jarvis: {e}")
            return
        self.unapplied = False
        self.say(said.get("message", "Jarvis restarted."))
        if then_leave:
            self.finish()

    # Drawing ----------------------------------------------------------------------------------

    def redraw(self):
        head = Text.assemble(("Jarvis settings", f"bold {theme.ACCENT}"), "  ")
        for n, name in enumerate(self.categories()):
            if n == self.category and not self.search:
                head.append(f" {name} ", style=f"bold black on {theme.ACCENT}")
            else:
                head.append(f" {name} ", style=theme.DIM if self.search else "")
            head.append(" ")
        self.head.update(head)
        if self.error:
            body = Text(f"Couldn't get Jarvis's settings: {self.error}", style=theme.ERROR)
        elif self.data is None:
            body = dim("Loading…")
        elif self.leaving:
            body = Group(Text("Some changes apply once Jarvis restarts.", style=f"bold {theme.ACCENT}"),
                         Text(""), Text("Enter to restart Jarvis now · Esc to leave without restarting"))
        else:
            body = self.draw_rows()
        self.content.update(body)
        self.hint.update(Text(self.hint_text()))

    def draw_rows(self):
        if self.choosing:
            s = self.choosing
            rows = [Text(s["label"], style=f"bold {theme.ACCENT}"), dim(s["about"] or ""), Text("")]
            for n, option in enumerate(s["choices"]):
                chosen = n == self.choice
                row = Text.assemble(("❯ " if chosen else "  ", theme.ACCENT))
                row.append(option, style=f"bold {theme.ACCENT}" if chosen else "")
                if option == self.shown(s):
                    row.append("  ✔", style=theme.SUCCESS)
                if option == s["default"]:
                    row.append("  (default)", style=theme.DIM)
                rows.append(row)
            return Group(self.search_box(), Text(""), *rows)
        if self.typing:
            s = self.typing
            row = Text(f"{s['label']}: ", style=f"bold {theme.ACCENT}")
            row.append(self.typed)
            row.append("▏", style=theme.ACCENT)
            default = s["default"] if s["default"] != "" else "empty"
            return Group(self.search_box(), Text(""), row, dim(s["about"] or ""),
                         dim(f"Default: {default}. Empty puts the default back."))
        shown = self.rows()
        if not shown:
            return Group(self.search_box(), Text(""), dim("No settings match."))
        self.selected = min(self.selected, len(shown) - 1)
        width = max(len(s["label"]) for s in shown) + 3
        value_width = min(40, max(len(self.shown(s)) + (len(s["note"]) + 3 if s.get("note") else 0) + 2
                                  for s in shown))
        lines = []
        for n, s in enumerate(shown):
            chosen = n == self.selected
            line = Text.assemble(("❯ " if chosen else "  ", theme.ACCENT))
            line.append(s["label"].ljust(width), style=f"bold {theme.ACCENT}" if chosen else "")
            unset = s["value"] is None and s["kind"] not in ("secret",) and s["key"] != "DND"
            start = line.cell_len
            line.append(self.shown(s), style=theme.ACCENT if chosen else theme.DIM if unset else theme.LABEL)
            if s.get("note"):
                line.append(f" ({s['note']})", style=theme.DIM)
            if s["kind"] in ("choice", "int", "float", "text"):
                line.append(" ›", style=theme.DIM)
            if self.search:  # which category each match is in, lined up after the values
                line.append(" " * max(2, value_width + 2 - (line.cell_len - start)))
                line.append(s["category"], style=theme.DIM)
            lines.append(line)
        about = shown[self.selected]["about"] or ""
        return Group(self.search_box(), *self.window(lines, self.selected, self.list_height(18)),
                     Text(""), Text(about, style=theme.LABEL))

    def search_box(self):
        colour = theme.ACCENT if self.search else theme.LABEL
        text = Text.assemble(("⌕ ", colour))
        if self.search:
            text.append(self.search)
            text.append("▏", style=theme.ACCENT)
        else:
            text.append("Type to search every setting…", style=theme.DIM)
        width = max(30, (self.content.size.width or (self.app.size.width if self.app else 100) - 14) - 1)
        top = Text("╭" + "─" * (width - 2) + "╮", style=colour)
        middle = Text.assemble(("│ ", colour))
        middle.append_text(text)
        middle.append(" " * max(0, width - 3 - text.cell_len))
        middle.append("│", style=colour)
        bottom = Text("╰" + "─" * (width - 2) + "╯", style=colour)
        return Group(top, middle, bottom)

    def hint_text(self):
        prefix = f"{self.message} · " if self.message else ""
        if self.unapplied and not self.message:
            prefix = "Restart Jarvis to apply (ctrl+r) · "
        if self.choosing:
            return prefix + "↑/↓ to choose · Enter to set · Esc to go back"
        if self.typing:
            return prefix + "Type a value · Enter to set · Esc to go back"
        if self.search:
            return prefix + "↑/↓ to move · Enter to change · Esc to clear the search"
        return (prefix + "←/→ categories · type to search · ↑/↓ · Enter to change · Delete for the default · "
                "ctrl+r restarts Jarvis · Esc to close")


class JarvisSettingsPage(Screen):
    """The settings on a page of their own, JARVIS on top, as the conversations are."""

    DEFAULT_CSS = f"""
    JarvisSettingsPage {{ background: ansi_default; layout: vertical; }}
    JarvisSettingsPage > JarvisSettingsPanel {{ height: 1fr; max-height: 100%; margin: 0 2; }}
    JarvisSettingsPage > JarvisSettingsPanel > VerticalScroll {{ height: 1fr; max-height: 100%; }}
    JarvisSettingsPage TopRule {{ display: none; }}
    """

    def __init__(self, panel):
        super().__init__()
        self.panel = panel

    def compose(self):
        yield Splash()
        yield self.panel


__all__ = ["JarvisSettingsPanel", "JarvisSettingsPage", "command", "run"]
