"""The Jarvis window: Claude Code's screen, rebuilt in blue, with Claude Code itself answering.

Everything Claude Code does as an engine (tools, MCP servers, skills, hooks, auto mode, saved
sessions, compaction) comes from the real `claude`, running headless (engine.py). This draws
what it does the way Claude Code would, and answers what it asks: permission to use a tool,
AskUserQuestion, approval of a plan.
"""

import asyncio
import difflib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import unquote, urlparse

from rich.console import Group
from rich.text import Text
from textual import events
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.theme import Theme
from textual.widgets import Markdown, Static

from . import __version__, config_items, files, persona, sessions, stats, theme, tools
from .engine import DETACHED, Engine, EngineError, claude_command, claude_path, clean_environment
from .jarvis_settings import JarvisSettingsPage, JarvisSettingsPanel
from .manager import MANAGER_PROMPT, SERVER, THREAD_LIMIT, Manager, ManagerPane, update_notice
from .panels import McpPanel, Panel, SettingsPanel, strftime
from .widgets import (AssistantEntry, Banner, Dialog, DoneEntry, Entry, Footer, NoticeEntry,
                      Picker, PromptBar, PromptInput, QueuedPanel, SearchBar, SideAnswer, Spinner,
                      SessionsPage, SuggestionMenu, TaskPanel, ThinkingEntry, ToolEntry, Transcript, UserEntry, notice,
                      quote_reply, shortcuts)

CHORD_TIME = 2       # seconds after ctrl+x in which s sends now
HISTORY_LIMIT = 300  # saved records redrawn on resume; older ones are left out
USAGE_REFRESH = 60   # seconds between asking for the plan's usage, so it keeps up with other windows
SETTINGS = os.path.expanduser("~/.claude/jarvis-window.json")  # the window's own settings (/keepitgoin)

# /keepitgoin: a turn cut off by the usage limit carries on by itself once the limit resets,
# at most this many times in a row (a limit that's still there when it says it reset).
KEEP_GOING_PROMPT = "The usage limit has reset. Carry on with what you were doing."
KEEP_GOING_TRIES = 3
MAX_THREAD_LIMIT = 50  # /manager-thread-limit at most: each thread is a Claude Code of its own

# Headless Claude Code doesn't title conversations, so the window asks for one (for the tab
# and /resume) in a separate, unsaved `claude -p` on Haiku, with no tools.
TITLE_PROMPT = """Write a title of 2 to 6 words for a conversation that starts with the message \
below. Reply with the title only.

<message>
{}
</message>"""
TITLE_ARGS = ["--model", "haiku", "--tools", "", "--strict-mcp-config"]

# /btw, as in Claude Code: a question about the conversation, answered in a fork of it that has
# no tools and isn't saved, so it never joins the conversation or holds up the turn in progress.
BTW_PROMPT = """The user has asked a side question with /btw while the main conversation carries \
on. Answer it directly and briefly from what is already in the conversation. You have no tools \
for this reply, and neither the question nor your answer will be kept in the conversation."""
BTW_TIMEOUT = 180

EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"]
EFFORT_ABOUT = {"low": "Quick, straightforward", "medium": "Balanced", "high": "Thorough",
                "xhigh": "Very thorough", "max": "Deepest reasoning; slow and uses more of your plan"}

LOCAL_COMMANDS = {
    "help": "Show commands and shortcuts",
    "resume": "Resume an earlier conversation",
    "model": "Choose the model",
    "effort": "Choose the effort level",
    "default-usage": "Choose the effort new windows start with (/default-usage <level>, or off)",
    "usage": "Show session cost, plan usage, and what's using it",
    "status": "Show the version, account, model and MCP servers",
    "config": "Change Claude Code's settings (or /config key=value)",
    "stats": "Show your Claude Code activity and tokens over time",
    "mcp": "Manage MCP servers: status, tools, reconnect, disable",
    "copy": "Copy Jarvis's last reply to the clipboard",
    "export": "Save this conversation to a text file",
    "claude": "Open this conversation in regular Claude Code; exit it to come back",
    "btw": "Ask a quick side question without interrupting or adding to the conversation",
    "jarvis": "Jarvis's own settings (/jarvis settings): voice, Claude, alarms, do not disturb…",
    "manager": "Project manager mode: Jarvis hands the work to worker Jarvises (/manager off to stop)",
    "manager-thread-limit": f"How many /manager threads work at once, at every level (default {THREAD_LIMIT})",
    "finished": "Mark this conversation as finished in the list of conversations (/finished off to undo)",
    "keepitgoin": "Carry on by itself when a usage limit resets (/keepitgoin on|off; no args toggles)",
    "computer": "Show which computer this window is running on",
    "fuck": "Stop Jarvis right now, like esc",
    "exit": "Exit Jarvis",
}
ALIASES = {"quit": "exit", "q": "exit", "new": "clear", "reset": "clear", "continue": "resume",
           "cost": "usage", "cc": "claude", "settings": "config", "default-effort": "default-usage"}
SETTINGS_TABS = {"usage": "usage", "status": "status", "config": "config", "stats": "stats"}
# Claude Code screens with no headless form; /claude opens them on the same conversation.
ONLY_IN_CLAUDE = {"login", "logout", "plugin", "plugins", "permissions", "allowed-tools", "hooks",
                  "memory", "theme", "terminal-setup", "ide", "vim", "statusline", "keybindings",
                  "privacy-settings", "bug", "feedback", "release-notes", "install-github-app",
                  "add-dir", "rewind", "checkpoint", "upgrade", "bashes", "tasks", "todos",
                  "sandbox", "migrate-installer", "remote-control"}

REJECTED = ("The user doesn't want to proceed with this tool use. The tool use was rejected (eg. if "
            "it was a file edit, the new_string was NOT written to the file). STOP what you are "
            "doing and wait for the user to tell you how to proceed.")

PERMISSION_TITLES = {"Bash": "Bash command", "Edit": "Edit file", "MultiEdit": "Edit file",
                     "Write": "Create file", "NotebookEdit": "Edit notebook", "Read": "Read file",
                     "WebFetch": "Fetch", "WebSearch": "Web search"}


@dataclass
class Options:
    cwd: str
    model: str | None = None
    effort: str | None = None
    permission_mode: str | None = None
    resume: str | None = None       # a session id, or "" to choose one
    continue_last: bool = False
    prompt: str | None = None
    persona: bool = True
    thinking_display: str = "summarized"
    engine_command: list | None = None  # tests swap in a fake Claude Code
    log_path: str | None = None
    extra: dict = field(default_factory=dict)


def pretty_model(model):
    """claude-opus-5-5 -> Opus 5.5, claude-fable-5-1[1m] -> Fable 5.1 (1M)."""
    if not model:
        return ""
    long_context = "[1m]" in model
    parts = model.replace("[1m]", "").removeprefix("claude-").split("-")
    family = parts[0].capitalize()
    numbers = [p for p in parts[1:] if p.isdigit() and len(p) < 3]
    label = f"{family} {'.'.join(numbers)}" if numbers else family
    return label + (" (1M)" if long_context else "")


def load_setting(key, default):
    try:
        with open(SETTINGS) as f:
            return json.load(f).get(key, default)
    except (OSError, ValueError, AttributeError):
        return default


def thread_limit():
    """/manager-thread-limit's saved value, or the default."""
    value = load_setting("manager_thread_limit", None)
    return value if isinstance(value, int) and 1 <= value <= MAX_THREAD_LIMIT else THREAD_LIMIT


def save_setting(key, value):
    try:
        with open(SETTINGS) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    saved[key] = value
    try:
        with open(SETTINGS, "w") as f:
            json.dump(saved, f, indent=2)
    except OSError:
        pass


def git_branch(cwd):
    try:
        out = subprocess.run(["git", "branch", "--show-current"], cwd=cwd, capture_output=True,
                             text=True, timeout=2)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""



def erase_is_delete():
    """Whether the terminal's backspace sends DEL (^?), leaving ^H to ctrl+backspace: true of
    GNOME Terminal, Konsole, Windows Terminal and most others (stty erase says which)."""
    if os.name == "nt":
        return True
    import termios
    try:
        return termios.tcgetattr(sys.stdin.fileno())[6][termios.VERASE] in (b"\x7f", 127)
    except (AttributeError, OSError, ValueError, termios.error):
        return True


ERASE_IS_DELETE = erase_is_delete()


def is_modifier(key):
    """A modifier key pressed on its own. Terminals with the kitty keyboard protocol report
    these, so letting go of ctrl between the x and the s of ctrl+x s sends one."""
    return key.rsplit("_", 1)[-1] in ("shift", "control", "alt", "super", "meta", "hyper") \
        or key in ("caps_lock", "num_lock")


def is_word_backspace(event):
    """ctrl+backspace (or alt+backspace). Textual reads the ^H most terminals send for it as a
    plain backspace, and its Input deletes the word to the right on ctrl+backspace."""
    if event.key in ("ctrl+backspace", "alt+backspace"):
        return True
    return event.key == "backspace" and event.character == "\x08" and ERASE_IS_DELETE


def console_title(title=None):
    """On Windows, the console's title: set to title, or read (None to leave it). PowerShell's
    window and Windows Terminal's tab show it, escape codes on or not. Elsewhere, None."""
    if os.name != "nt":
        return None
    import ctypes
    kernel32 = ctypes.windll.kernel32
    if title is not None:
        kernel32.SetConsoleTitleW(title)
        return title
    buf = ctypes.create_unicode_buffer(1024)
    return buf.value if kernel32.GetConsoleTitleW(buf, len(buf)) else None

def system_copy(text):
    """Puts text on the clipboard with the system's own command, for terminals that ignore
    OSC 52 (VTE ones: xfce4-terminal, GNOME Terminal; macOS Terminal). False if there's none."""
    if sys.platform == "darwin":
        commands = [["pbcopy"]]
    elif sys.platform.startswith("linux"):
        commands = [["wl-copy"]] if os.environ.get("WAYLAND_DISPLAY") else []
        if os.environ.get("DISPLAY"):
            commands += [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
    else:
        return False  # Windows Terminal takes OSC 52
    for command in commands:
        if shutil.which(command[0]):
            try:
                proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, start_new_session=True)
                proc.stdin.write(text.encode())
                proc.stdin.close()
                return True
            except OSError:
                continue
    return False


def over_ssh():
    return bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))


def open_here(target):
    """Opens a URL or file with the system's own handler, on the screen in front of the owner.
    False where that can't be done: over ssh (it would open on the other computer's screen, if
    anywhere) or on Linux with no display."""
    if over_ssh() or (sys.platform.startswith("linux")
                      and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))):
        return False
    try:
        if sys.platform == "win32":
            os.startfile(target)
            return True
        command = "open" if sys.platform == "darwin" else "xdg-open"
        if shutil.which(command):
            subprocess.Popen([command, target], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return True
        return webbrowser.open(target)
    except OSError:
        return False


def user_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def tag(text, name):
    match = re.search(rf"<{name}>(.*?)</{name}>", text, re.S)
    return match.group(1) if match else None


def preview_patch(path, old, new, replace_all=False):
    """The hunks an edit would make, for the permission dialog (Claude Code shows the diff)."""
    try:
        with open(path, errors="replace") as f:
            before = f.read()
    except OSError:
        before = ""
    if old and old in before:
        after = before.replace(old, new) if replace_all else before.replace(old, new, 1)
    elif not old:
        after = new
    else:
        before, after = old, new
    return hunks(before, after)


def hunks(before, after):
    out = []
    a, b = before.splitlines(), after.splitlines()
    for group in difflib.SequenceMatcher(None, a, b).get_grouped_opcodes(3):
        first, last = group[0], group[-1]
        lines = []
        for op, i1, i2, j1, j2 in group:
            if op == "equal":
                lines += [" " + line for line in a[i1:i2]]
            else:
                lines += ["-" + line for line in a[i1:i2]]
                lines += ["+" + line for line in b[j1:j2]]
        out.append({"oldStart": first[1] + 1, "oldLines": last[2] - first[1],
                    "newStart": first[3] + 1, "newLines": last[4] - first[3], "lines": lines})
    return out


# The terminal's own background and text colour (as Claude Code uses them), with Jarvis blue
# for everything Textual draws by itself: focus, selection, scrollbars, links, headings.
JARVIS_THEME = Theme(
    name="jarvis", ansi=True, dark=True,
    primary=theme.ACCENT, secondary=theme.LABEL, accent=theme.ACCENT,
    warning=theme.WARNING, error=theme.ERROR, success=theme.SUCCESS,
    foreground="ansi_default", background="ansi_default", surface="ansi_default",
    panel="ansi_default", boost="ansi_default",
    variables={
        "ansi-background": "ansi_black", "ansi-foreground": "ansi_white",
        "border": theme.ACCENT, "border-blurred": theme.LABEL,
        "block-cursor-foreground": "ansi_black", "block-cursor-background": theme.ACCENT,
        "input-cursor-background": theme.CURSOR, "input-cursor-foreground": "ansi_black",
        "input-cursor-text-style": "none",
        "input-selection-background": "#1f4f7a", "input-selection-foreground": "ansi_default",
        "screen-selection-background": "#1f4f7a", "screen-selection-foreground": "ansi_default",
        "scrollbar": "#22364f", "scrollbar-hover": theme.LABEL, "scrollbar-active": theme.ACCENT,
        "scrollbar-background": "ansi_default", "scrollbar-background-hover": "ansi_default",
        "scrollbar-background-active": "ansi_default", "scrollbar-corner-color": "ansi_default",
        "link-color": theme.CODE, "link-color-hover": theme.SHIMMER, "link-background-hover": "transparent",
        "markdown-h1-color": "ansi_default", "markdown-h2-color": "ansi_default",
        "markdown-h3-color": "ansi_default", "markdown-h4-color": "ansi_default",
        "markdown-h1-background": "transparent", "markdown-h2-background": "transparent",
        "markdown-h3-background": "transparent",
    },
)


class JarvisApp(App):
    TITLE = "Jarvis"
    ENABLE_COMMAND_PALETTE = False

    CSS = f"""
    Screen {{ background: ansi_default; layout: vertical; }}
    #panes {{ height: 1fr; }}
    #main {{ width: 1fr; height: 1fr; }}
    #dock {{ height: auto; padding: 0 1; }}
    SuggestionMenu, Footer {{ background: ansi_default; }}
    Markdown.reply, Markdown.plan {{ padding: 0; margin: 0; background: transparent; color: ansi_default; }}
    Markdown.reply > MarkdownParagraph, Markdown.plan > MarkdownParagraph {{ margin: 0 0 1 0; }}
    Markdown.reply MarkdownH1, Markdown.reply MarkdownH2, Markdown.reply MarkdownH3,
    Markdown.plan MarkdownH1, Markdown.plan MarkdownH2, Markdown.plan MarkdownH3 {{
        content-align: left top; background: transparent; color: ansi_default; margin: 0 0 1 0;
    }}
    Markdown.reply MarkdownH1, Markdown.plan MarkdownH1 {{ text-style: bold underline; }}
    Markdown.reply MarkdownH2, Markdown.plan MarkdownH2 {{ text-style: bold; }}
    Markdown.reply MarkdownH3, Markdown.plan MarkdownH3 {{ text-style: bold italic; }}
    Markdown.reply MarkdownBlock > .code_inline, Markdown.plan MarkdownBlock > .code_inline {{
        background: transparent !important; color: {theme.CODE} !important;
    }}
    Markdown.reply MarkdownBlock > .strong, Markdown.plan MarkdownBlock > .strong {{ text-style: bold; }}
    Markdown.reply MarkdownFence, Markdown.plan MarkdownFence {{ background: transparent; margin: 0 0 1 0; }}
    Markdown.reply MarkdownFence > Label, Markdown.plan MarkdownFence > Label {{ padding: 0 0 0 2; }}
    Markdown.reply MarkdownBlockQuote, Markdown.plan MarkdownBlockQuote {{
        background: transparent; border-left: outer {theme.ACCENT}; margin: 0 0 1 0;
    }}
    Markdown.reply MarkdownBullet, Markdown.plan MarkdownBullet {{ color: {theme.ACCENT}; }}
    Markdown.reply MarkdownTable, Markdown.plan MarkdownTable {{ background: transparent; margin: 0 0 1 0; }}
    Markdown.reply MarkdownTableContent, Markdown.plan MarkdownTableContent {{
        grid-gutter: 0 1; keyline: thin {theme.LABEL} 50%;
    }}
    Markdown.reply MarkdownTableContent > .header, Markdown.plan MarkdownTableContent > .header {{
        color: {theme.ACCENT}; text-style: bold;
    }}
    Markdown.reply MarkdownHorizontalRule {{ border-bottom: solid {theme.LABEL}; }}
    Markdown.command-output {{ padding: 0 0 0 3; }}
    #plan-view {{ height: auto; max-height: 24; border-left: outer {theme.ACCENT}; padding: 0 1;
                  margin: 1 0 0 0; }}
    """

    BINDINGS = [
        Binding("ctrl+c", "ctrl_c", "Copy, clear or exit", priority=True, show=False),
        Binding("shift+tab", "cycle_mode", "Switch mode", priority=True, show=False),
        Binding("ctrl+o", "toggle_verbose", "Details", priority=True, show=False),
        Binding("ctrl+t", "toggle_tasks", "Tasks", show=False),
        Binding("ctrl+g", "external_editor", "Edit in $EDITOR", show=False),
        Binding("escape", "escape", "Interrupt", show=False),
        Binding("ctrl+l", "scroll_bottom", show=False),
        Binding("pageup", "page(-1)", show=False, priority=True),
        Binding("pagedown", "page(1)", show=False, priority=True),
    ]

    async def on_event(self, event):
        if isinstance(event, events.Key) and is_word_backspace(event):
            event = events.Key("ctrl+w", None)  # delete a word, in the prompt and in every Input
        if isinstance(event, events.Key) and not event.is_forwarded and self.window_key(event.key):
            return
        await super().on_event(event)

    def window_key(self, key):
        """Keys taken before whatever has focus sees them (the transcript or a dialog may have
        it): ctrl+x s, ctrl+q (Textual's quit otherwise), alt+up and alt+down, ctrl+r, ctrl+f.
        True if taken."""
        if self.chord_until and not is_modifier(key):
            pending, self.chord_until = time.monotonic() < self.chord_until, 0.0
            if pending and key in ("s", "ctrl+s"):
                self.footer.flash_text = None
                self.footer.redraw()
                text = self.prompt.text if self.dialog is None else ""
                self.prompt.post_message(PromptInput.Submitted(text, now=True))
                return True
        if key == "ctrl+x":  # instead of TextArea's cut: the start of ctrl+x s
            self.chord_until = time.monotonic() + CHORD_TIME
            self.footer.flash("ctrl+x · s to send now", CHORD_TIME)
            return True
        if key == "ctrl+q":
            self.unqueue()
            return True
        if key in ("alt+up", "alt+down") and self.dialog is None:
            self.jump_to_prompt(-1 if key == "alt+up" else 1)
            return True
        if key == "ctrl+r" and self.dialog is None:
            self.reply_to_selection()
            return True
        if key == "ctrl+f" and self.dialog is None:  # instead of TextArea's delete-word-right
            self.search_bar.open()
            self.search(self.search_bar.input.value)
            return True
        return False

    def __init__(self, options: Options):
        super().__init__(ansi_color=True)
        self.options = options
        self.cwd = options.cwd
        self.engine = None
        self.stopping_engine = False
        self.info = {}
        self.commands = {}
        self.models = []
        self.mcp_servers = []
        self.session_id = None
        self.mode = options.permission_mode or "default"
        self.model = None
        self.effort = None
        self.model_override = options.model
        self.effort_override = options.effort or load_setting("default_effort", None)  # /default-usage
        self.busy = False
        self.interrupting = False
        self.turn_started = 0.0
        self.tokens_done = self.tokens_current = 0
        self.verbose = False
        self.show_tasks = True
        self.tasks = {}      # TaskCreate id -> {"content", "status", "activeForm"}
        self.todos = []      # TodoWrite's list
        self.tools = {}      # tool_use_id -> ToolEntry
        self.agent_of = {}   # a subagent's tool_use_id -> its agent's ToolEntry
        self.blocks = {}     # (message id, block index) -> entry being streamed
        self.waiting = {}    # message id -> streamed entries waiting for their final text
        self.message_id = None
        self.sent = []       # [text, shown, uuid] prompts not yet taken in by Claude Code
        self.updates = set() # uuids of /manager's thread updates not yet taken in
        self.manager = None  # project manager mode's threads (manager.py), when it's on
        self.thread_limit = thread_limit()  # /manager-thread-limit
        self.capabilities = set()  # what Claude Code says it can do, on system/init
        self.chord_until = 0.0     # ctrl+x was pressed; s before this sends now
        self.jumped = None   # the message of yours alt+up / alt+down last scrolled to
        self.reply = None    # the highlighted text the next prompt replies to (ctrl+r)
        self.found = []      # the entries matching the ctrl+f search, oldest first
        self.found_index = -1
        self.pending_context = []  # ! output that goes along with the next prompt
        self.history = []
        self.history_index = -1
        self.history_draft = ""
        self.dialog = None
        self.dialog_request = None
        self.cancelled = set()
        self.last_exit_press = 0.0
        self.last_escape = 0.0
        self.context_pct = None
        self.limit_note = ""
        self.keep_going = load_setting("keep_going", True)  # /keepitgoin
        self.stalled = None               # a turn hit the usage limit: when it resets (epoch seconds)
        self.kept_going = 0               # times in a row /keepitgoin has carried on
        self.session_ends = None          # when the 5-hour usage window resets (epoch seconds)
        self.usage = {}                   # % of the plan used: {"five_hour": 13, "seven_day": 28}
        self.tab_title = None             # the terminal tab's title: what the conversation is about
        self.finished = False             # a turn ended and you haven't typed since: ✓ in the tab's title
        self.title_shown = None           # the title as last written to the terminal
        self.titled = None                # the session a title was looked for (once each)
        self.file_index = files.FileIndex(self.cwd)
        self.permission_queue = None
        self.branch = ""
        self.engine_state = "connecting"  # connecting, running, stopped
        self.batch = None                 # entries collected while redrawing a saved conversation
        self.hooks = {}                   # the hooks in Claude Code's settings, by event
        self.background = {}              # task id -> what's running in the background
        self.config_options = None        # what /config takes, as Claude Code lists it
        self.engine_started = 0.0
        self.stats_cache = (0.0, None)    # (when, stats.Stats)
        self.commands_pending = False     # a /model or /effort ran: read the settings again after it
        self.show_tasks_idle = False

    # Layout ---------------------------------------------------------------------------------

    def compose(self):
        with Horizontal(id="panes"):
            with Vertical(id="main"):
                with Transcript(id="transcript"):
                    yield Banner()
                yield Spinner(id="spinner")
                yield TaskPanel(id="tasks")
                yield QueuedPanel(id="queued")
                with Vertical(id="dock"):
                    yield SideAnswer(id="btw")
                    yield SearchBar(id="search")
                    yield PromptBar(id="prompt-bar")
                yield SuggestionMenu(id="menu")
            yield ManagerPane(id="manager")  # /manager's threads, on the right
        yield Footer(id="footer")

    async def on_mount(self):
        self.register_theme(JARVIS_THEME)
        self.theme = "jarvis"
        self.transcript = self.query_one(Transcript)
        self.banner = self.query_one(Banner)
        self.spinner = self.query_one(Spinner)
        self.task_panel = self.query_one(TaskPanel)
        self.queued_panel = self.query_one(QueuedPanel)
        self.side_answer = self.query_one(SideAnswer)
        self.search_bar = self.query_one(SearchBar)
        self.manager_pane = self.query_one(ManagerPane)
        self.dock = self.query_one("#dock")
        self.prompt_bar = self.query_one(PromptBar)
        self.prompt = self.prompt_bar.input
        self.menu = self.query_one(SuggestionMenu)
        self.footer = self.query_one(Footer)
        self.branch = git_branch(self.cwd)
        self.banner.set_info(cwd=tools.short_path(self.cwd, None), branch=self.branch)
        self.history = sessions.read_history(self.cwd)
        self.permission_queue = asyncio.Queue()
        self.submissions = asyncio.Queue()
        self.set_interval(0.08, self.tick)
        self.set_interval(USAGE_REFRESH, self.refresh_usage)
        self.run_worker(self.permission_loop(), group="permissions")
        self.run_worker(self.submission_loop(), group="submissions")
        self.run_worker(self.file_index.load(), group="files")
        self.prompt_bar.set_mode(self.mode)
        self.footer.mode = self.mode
        self.update_footer()
        self.prompt.focus()
        self.run_worker(self.boot(), group="boot")

    async def boot(self):
        resume = self.options.resume
        if resume == "":
            resume = await self.pick_session()
        elif self.options.continue_last:
            recent = sessions.list_sessions(self.cwd, limit=1)
            resume = recent[0].id if recent else None
            if not recent:
                await self.note("No earlier conversation in this folder; starting a new one.")
        if resume:
            path = sessions.find_session(self.cwd, resume)
            if path:
                resume = os.path.basename(path)[:-6]
                await self.show_history(path)
                await self.reopen_manager(resume)
            else:
                await self.note(f"No conversation {resume} found; starting a new one.", theme.ERROR)
                resume = None
        if await self.start_engine(resume=resume):
            if resume or self.options.continue_last:
                self.name_conversation()
            if self.options.prompt:
                self.submissions.put_nowait((self.options.prompt, False))

    # The engine -----------------------------------------------------------------------------

    async def start_engine(self, resume=None):
        system_prompt = persona.PERSONA if self.options.persona else None
        if self.manager is not None:  # the coordinator, with the tools to run its threads
            system_prompt = (system_prompt + "\n\n" if system_prompt else "") + MANAGER_PROMPT
        engine = Engine(self.cwd, None, None, model=self.model_override, effort=self.effort_override,
                        permission_mode=self.mode if self.mode != "default" else None,
                        resume=resume, append_system_prompt=system_prompt,
                        thinking_display=self.options.thinking_display,
                        sdk_mcp_servers=[SERVER] if self.manager is not None else (),
                        on_mcp=self.manager.handle_mcp if self.manager is not None else None,
                        command=self.options.engine_command, log_path=self.options.log_path)
        engine.on_message = lambda msg: self.on_engine_message(engine, msg)
        engine.on_permission = lambda rid, req: self.permission_queue.put_nowait((engine, rid, req))
        self.engine = engine
        self.session_id = resume
        self.updates.clear()
        self.engine_state = "connecting"
        self.update_footer()
        try:
            self.info = await engine.start()
        except (EngineError, OSError) as e:
            tail = "\n".join(list(engine.stderr)[-6:])
            await self.note(f"Couldn't start Claude Code: {e}" + (f"\n{tail}" if tail else ""), theme.ERROR)
            self.engine_state = "stopped"
            self.update_footer()
            return False
        self.engine_state = "running"
        self.engine_started = time.monotonic()
        self.commands = {c["name"]: c for c in self.info.get("commands", [])}
        self.models = self.info.get("models", [])
        self.set_mode(self.info.get("current_permission_mode") or self.mode)
        account = self.info.get("account") or {}
        self.banner.set_info(plan=account.get("subscriptionType"), version=self.info.get("claude_code_version"))
        await self.refresh_settings()
        self.run_worker(self.fetch_usage(engine), group="usage", exclusive=True)
        return True

    def refresh_usage(self):
        """Ask again how much of the plan is used. It's shared with every other Claude (other
        windows, Claude Code, claude.ai), so a turn here isn't the only thing that moves it."""
        if self.engine is not None and self.engine.running:
            self.run_worker(self.fetch_usage(self.engine), group="usage", exclusive=True)

    def on_app_focus(self):
        self.refresh_usage()  # back from another window, maybe one that used some

    async def fetch_usage(self, engine):
        """How much of the plan is used (5-hour and weekly) and when the 5-hour window resets,
        for the footer: when Claude Code starts, after each turn, every minute and on coming back to the window."""
        try:
            limits = (await engine.request("get_usage", timeout=90)).get("rate_limits") or {}
        except EngineError:
            return
        listed = {l.get("kind"): l for l in limits.get("limits") or [] if isinstance(l, dict)}
        for name, kind in (("five_hour", "session"), ("seven_day", "weekly_all")):
            percent = (limits.get(name) or {}).get("utilization")
            if percent is None:
                percent = (listed.get(kind) or {}).get("percent")
            if percent is not None:
                self.usage[name] = round(percent)
        when = (limits.get("five_hour") or {}).get("resets_at") or (listed.get("session") or {}).get("resets_at")
        try:
            self.set_session_end(datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp())
        except (AttributeError, ValueError):
            pass
        self.update_footer()

    def set_session_end(self, when):
        if when and when != self.session_ends:
            self.session_ends = when
            self.set_timer(max(0.0, when - time.time()) + 1, self.session_over)
            self.update_footer()

    def session_over(self):
        """The 5-hour window's end came: what it used no longer counts. Ask for the new one."""
        if self.session_ends and self.session_ends <= time.time():
            self.usage.pop("five_hour", None)
            self.update_footer()
            self.refresh_usage()

    async def stop_engine(self):
        engine, self.engine = self.engine, None
        if engine is not None:
            self.stopping_engine = True
            await engine.stop()
            self.stopping_engine = False
        self.end_turn()

    async def refresh_settings(self):
        if not self.engine:
            return
        try:
            settings = await self.engine.request("get_settings", timeout=10)
        except EngineError:
            return
        applied = settings.get("applied") or {}
        self.hooks = (settings.get("effective") or {}).get("hooks") or {}
        self.set_model(applied.get("model"))
        self.effort = applied.get("effort") or self.effort
        self.banner.set_info(model=self.model_label(), effort=self.effort)
        self.update_footer()

    async def refresh_context(self):
        if not self.engine or not self.engine.running:
            return
        try:
            usage = await self.engine.request("get_context_usage", timeout=10)
        except EngineError:
            return
        self.context_pct = usage.get("percentage")
        self.update_footer()

    def model_label(self):
        for m in self.models:
            if m.get("value") != "default" and m.get("resolvedModel") == self.model:
                return m.get("displayName") or pretty_model(self.model)
        return pretty_model(self.model)

    def set_model(self, model):
        if model and model != "<synthetic>" and model != self.model:
            self.model = model
            self.banner.set_info(model=self.model_label())
            self.update_footer()

    def set_mode(self, mode):
        if not mode:
            return
        self.mode = mode
        self.footer.mode = mode
        self.prompt_bar.set_mode(mode)
        self.update_footer()

    def update_footer(self):
        if self.engine_state == "connecting":
            self.footer.right = "connecting to Claude Code…"
        elif self.engine_state == "stopped":
            self.footer.right = "Claude Code isn't running"
        else:
            running = len(self.background)
            session = f"5-hour {self.usage['five_hour']}%" if "five_hour" in self.usage else ""
            if self.session_ends and self.session_ends > time.time() and "resets" not in self.limit_note:
                clock = strftime(datetime.fromtimestamp(self.session_ends), "%-I:%M%p")
                session = (session or "5-hour session") + " ends " + clock.lower().replace(":00", "")
            limit = self.limit_note
            if limit and self.stalled and self.keep_going:
                limit += " · keeps going then"
            parts = (limit, session,
                     f"weekly {self.usage['seven_day']}%" if "seven_day" in self.usage else "",
                     f"{running} background task{'s' if running != 1 else ''}" if running else "",
                     self.model_label(), self.effort and f"{self.effort} effort",
                     f"{self.context_pct}% context" if self.context_pct is not None else "")
            self.footer.right = " · ".join(p for p in parts if p)
        self.footer.redraw()

    # Messages from Claude Code ----------------------------------------------------------------

    async def on_engine_message(self, engine, msg):
        if engine is not self.engine:
            return
        kind = msg.get("type")
        try:
            if kind == "stream_event":
                await self.on_stream(msg)
            elif kind == "assistant":
                await self.on_assistant(msg)
            elif kind == "user":
                await self.on_user(msg)
            elif kind == "system":
                await self.on_system(msg)
            elif kind == "result":
                await self.on_result(msg)
            elif kind == "rate_limit_event":
                self.on_rate_limit(msg)
            elif kind == "conversation_reset":
                await self.on_reset(msg)
            elif kind == "control_cancel_request":
                self.cancel_dialog(msg.get("request_id"))
            elif kind == "_exit":
                await self.on_engine_exit(msg)
        except Exception as e:  # a drawing problem must never stop the conversation
            self.log.error(f"handling {kind}: {e!r}")
            if os.environ.get("JARVIS_DEBUG"):
                raise

    async def on_stream(self, msg):
        if msg.get("parent_tool_use_id"):
            return  # subagents show as their tool calls
        event = msg.get("event") or {}
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message") or {}
            self.message_id = message.get("id")
            self.set_model(message.get("model"))
            self.tokens_current = 0
        elif kind == "content_block_start":
            block = event.get("content_block") or {}
            key = (self.message_id, event.get("index"))
            btype = block.get("type")
            if btype == "text":
                entry = AssistantEntry()
                self.spinner.mode = "responding"
            elif btype in ("thinking", "redacted_thinking"):
                entry = ThinkingEntry(verbose=self.verbose)
                self.spinner.mode = "thinking"
            elif btype in ("tool_use", "server_tool_use", "mcp_tool_use"):
                entry = self.tools.get(block.get("id"))
                if entry is None:
                    entry = self.tool_entry(block.get("id"), block.get("name", "?"), None)
                    await self.add(entry)
                self.blocks[key] = entry
                self.spinner.mode = "tool-input"
                return
            else:
                return
            self.blocks[key] = entry
            self.waiting.setdefault(self.message_id, []).append(entry)
            await self.add(entry)
        elif kind == "content_block_delta":
            entry = self.blocks.get((self.message_id, event.get("index")))
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta" and isinstance(entry, AssistantEntry):
                await entry.append(delta.get("text", ""))
                self.tokens_current += max(1, len(delta.get("text", "")) // 4)
            elif delta.get("type") == "thinking_delta" and isinstance(entry, ThinkingEntry):
                entry.append(delta.get("thinking", ""))
        elif kind == "content_block_stop":
            entry = self.blocks.pop((self.message_id, event.get("index")), None)
            if isinstance(entry, AssistantEntry):
                await entry.finish()
            elif isinstance(entry, ThinkingEntry):
                entry.finish()
        elif kind == "message_delta":
            usage = event.get("usage") or {}
            if usage.get("output_tokens"):
                self.tokens_current = usage["output_tokens"]
        elif kind == "message_stop":
            self.tokens_done += self.tokens_current
            self.tokens_current = 0
        self.spinner.tokens = self.tokens_done + self.tokens_current

    def tool_entry(self, tool_id, name, tool_input):
        entry = ToolEntry(tool_id, name, tool_input, self.cwd, self.verbose)
        if name in tools.QUIET_TOOLS:
            entry.add_class("quiet")
            entry.display = self.verbose
        self.tools[tool_id] = entry
        return entry

    def take_waiting(self, message_id, kind):
        entries = self.waiting.get(message_id) or []
        for entry in entries:
            if isinstance(entry, kind):
                entries.remove(entry)
                return entry
        return None

    async def on_assistant(self, msg):
        message = msg.get("message") or {}
        content = message.get("content") or []
        parent = msg.get("parent_tool_use_id")
        if parent:
            agent = self.tools.get(parent)
            if agent is not None:
                for block in content:
                    if block.get("type") == "tool_use":
                        agent.add_child(block.get("id"), block.get("name"), block.get("input"))
                        self.agent_of[block.get("id")] = agent
            return
        if message.get("model") == "<synthetic>":
            text = user_text(content).strip()
            if text and not msg.get("_history"):
                await self.add(NoticeEntry(self.command_output(text)))
            return
        self.set_model(message.get("model"))
        history = msg.get("_history")
        message_id = message.get("id")
        for block in content:
            btype = block.get("type")
            if btype == "text":
                entry = None if history else self.take_waiting(message_id, AssistantEntry)
                if entry is not None:
                    await entry.finish(block.get("text", ""))
                elif block.get("text", "").strip():
                    await self.add(AssistantEntry(block["text"]))
            elif btype in ("thinking", "redacted_thinking"):
                entry = None if history else self.take_waiting(message_id, ThinkingEntry)
                if entry is not None:
                    entry.finish(block.get("thinking"))
                elif history and (block.get("thinking") or "").strip():
                    await self.add(ThinkingEntry(block["thinking"], self.verbose, done=True))
            elif btype in ("tool_use", "server_tool_use", "mcp_tool_use"):
                entry = self.tools.get(block.get("id"))
                if entry is None:
                    entry = self.tool_entry(block.get("id"), block.get("name", "?"), block.get("input"))
                    await self.add(entry)
                else:
                    entry.set_input(block.get("input"))
                self.track_tasks(block.get("name"), block.get("input") or {})

    async def on_user(self, msg):
        message = msg.get("message") or {}
        content = message.get("content")
        parent = msg.get("parent_tool_use_id")
        results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"] \
            if isinstance(content, list) else []
        if results:
            for block in results:
                tool_id = block.get("tool_use_id")
                if parent or tool_id in self.agent_of:
                    agent = self.agent_of.get(tool_id) or self.tools.get(parent)
                    if agent is not None:
                        agent.child_done(tool_id, block.get("is_error"))
                    continue
                entry = self.tools.get(tool_id)
                if entry is None:
                    continue
                text = tools.content_text(block.get("content"))
                if block.get("is_error") and ("doesn't want to proceed" in text or "rejected" in text):
                    entry.mark("rejected")
                elif block.get("is_error") and "Interrupted" in text and not text.startswith("Error"):
                    entry.mark("interrupted")
                else:
                    entry.set_result(block.get("content"), msg.get("tool_use_result"), block.get("is_error"))
                self.track_result(entry, msg.get("tool_use_result"))
            return
        if parent:
            return  # the prompt Jarvis gave a subagent
        if msg.get("_history"):
            await self.show_saved_prompt(user_text(content))
            return
        if msg.get("isReplay"):
            await self.on_replay(user_text(content), msg.get("uuid"))

    async def on_replay(self, text, message_uuid=None):
        """Claude Code took in a prompt: show it if it was waiting in the queue."""
        if message_uuid in self.updates:  # a /manager thread's news, for the coordinator
            self.updates.discard(message_uuid)
            await self.add(NoticeEntry(update_notice(text) or notice("A thread update"), attached=False))
            if not self.busy:
                self.begin_turn()
            return
        if text.lstrip().startswith(("<command-", "/")) or not self.sent:
            return
        item = next((item for item in self.sent if message_uuid and item[2] == message_uuid), self.sent[0])
        self.sent = [other for other in self.sent if other is not item]
        prompt, shown, _ = item
        if not shown:
            self.show_queued()
            await self.add(UserEntry(prompt))
        if not self.busy:
            self.begin_turn()

    @property
    def queued(self):
        """Prompts typed while Jarvis was busy, still waiting in Claude Code's queue."""
        return [text for text, shown, _ in self.sent if not shown]

    def show_queued(self):
        self.queued_panel.show([(message_uuid, text) for text, shown, message_uuid in self.sent if not shown])

    async def show_saved_prompt(self, text):
        """A user record from a saved conversation: a prompt, a command, or ! output."""
        text = text.strip()
        if not text or text.startswith(("<system-reminder>", "<local-command-caveat>", "Caveat:")):
            return
        if update_notice(text) is not None:
            await self.add(NoticeEntry(update_notice(text), attached=False))
            return
        if text.startswith("[Request interrupted"):
            await self.add(NoticeEntry(tools.interrupted()))
            return
        name = tag(text, "command-name")
        if name:
            args = (tag(text, "command-args") or "").strip()
            await self.add(UserEntry(f"{name} {args}".strip()))
            return
        stdout = tag(text, "local-command-stdout")
        if stdout is not None:
            if stdout.strip():
                await self.add(NoticeEntry(self.command_output(stdout)))
            return
        command = tag(text, "bash-input")
        if command is not None:
            await self.add(UserEntry(command, "!", f"bold {theme.BASH_MODE}"))
            return
        if text.startswith("<bash-stdout>"):
            out = (tag(text, "bash-stdout") or "") + (tag(text, "bash-stderr") or "")
            await self.add(NoticeEntry(draw=lambda verbose: tools.block(
                tools.plain_lines(out, verbose) or [Text("(No output)", style=theme.DIM)]), verbose=self.verbose))
            return
        await self.add(UserEntry(text))

    def command_output(self, text):
        """What a slash command printed: Markdown when it's a table or headed report."""
        text = re.sub(r"</?local-command-stdout>", "", text).strip()
        text = re.sub(r"\x1b\[[0-9;]*m", "", text)
        if "\n" in text and ("|" in text or "#" in text or "**" in text):
            return Markdown(text, classes="reply command-output")
        return tools.block([Text(line) for line in text.split("\n")])

    async def on_system(self, msg):
        sub = msg.get("subtype")
        if sub == "init":
            self.session_id = msg.get("session_id") or self.session_id
            self.set_model(msg.get("model"))
            self.set_mode(msg.get("permissionMode"))
            self.mcp_servers = msg.get("mcp_servers") or self.mcp_servers
            self.capabilities = set(msg.get("capabilities") or [])
            if not self.busy and self.sent:
                self.begin_turn()
        elif sub == "status":
            if msg.get("permissionMode"):
                self.set_mode(msg["permissionMode"])
            status = msg.get("status")
            if status == "compacting":
                if not self.busy:
                    self.begin_turn()
                self.spinner.message = "Compacting conversation"
            elif status == "requesting" and self.spinner.message == "Compacting conversation":
                self.spinner.message = None
        elif sub == "thinking_tokens":
            self.tokens_current = max(self.tokens_current, msg.get("estimated_tokens") or 0)
            self.spinner.tokens = self.tokens_done + self.tokens_current
        elif sub == "compact_boundary":
            await self.add(NoticeEntry(Text("✻ Conversation compacted · ctrl+o for history",
                                            style=theme.DIM), attached=False))
        elif sub == "api_retry":
            attempt, most = msg.get("attempt"), msg.get("max_retries")
            self.spinner.extra = f"retrying {attempt}/{most}" if attempt else "retrying"
        elif sub in ("task_started", "task_progress"):
            entry = self.tools.get(msg.get("tool_use_id"))
            if entry is not None and entry.tool_name in ("Agent", "Task"):
                entry.set_activity(msg.get("description") or "")
            if sub == "task_started" and msg.get("is_backgrounded"):
                self.background[msg.get("task_id")] = msg.get("description") or ""
                self.update_footer()
        elif sub in ("task_notification", "task_updated"):
            status = msg.get("status") or (msg.get("patch") or {}).get("status")
            if status in ("completed", "failed", "killed", "stopped") and msg.get("task_id") in self.background:
                del self.background[msg["task_id"]]
                self.update_footer()
        elif sub in ("model_fallback", "model_refusal_fallback", "notification", "informational",
                     "memory_saved", "instruction_size_warning"):
            text = msg.get("message") or msg.get("text") or msg.get("content") or ""
            if isinstance(text, str) and text.strip():
                await self.add(NoticeEntry(notice(text.strip()), attached=False))
        elif sub == "local_command":
            text = msg.get("content") or msg.get("output") or ""
            if isinstance(text, str) and text.strip():
                await self.add(NoticeEntry(self.command_output(text)))

    async def on_result(self, msg):
        took = time.monotonic() - self.turn_started if self.turn_started else 0
        self.session_id = msg.get("session_id") or self.session_id
        if self.interrupting:
            for entry in self.tools.values():
                if entry.state == "running":
                    entry.mark("interrupted")
            await self.add(NoticeEntry(tools.interrupted()))
        elif msg.get("is_error"):
            errors = [e for e in (msg.get("errors") or []) if isinstance(e, str)]
            text = "; ".join(errors) or msg.get("result") or msg.get("subtype", "error").replace("_", " ")
            await self.add(NoticeEntry(notice(str(text), theme.ERROR)))
        elif took >= 8 and msg.get("num_turns"):
            await self.add(DoneEntry(took))
        if not self.limit_note:  # it got through: nothing left to carry on
            self.stalled, self.kept_going = None, 0
        # Prompts shown already were taken in; ones still queued come next, as a new turn.
        # Claude Code keeps them through an interrupt too (that's how ctrl+x s sends them now).
        self.sent = [item for item in self.sent if not item[1]]
        if self.sent or self.updates:
            self.interrupting = False
            self.turn_started = time.monotonic()
        else:
            self.show_queued()
            self.end_turn()
        self.run_worker(self.refresh_context(), group="context")
        self.refresh_usage()
        self.name_conversation()
        if self.commands_pending:
            self.commands_pending = False
            self.run_worker(self.refresh_settings(), group="settings")

    def on_rate_limit(self, msg):
        info = msg.get("rate_limit_info") or {}
        windows = info.get("unifiedWindows") or {}
        resets = info.get("resetsAt")
        five_hour = (windows.get("five_hour") or {}).get("resetsAt")
        if not five_hour and info.get("rateLimitType") == "five_hour":
            five_hour = resets
        if five_hour:
            self.set_session_end(five_hour)
        for name in ("five_hour", "seven_day"):
            utilization = (windows.get(name) or {}).get("utilization")
            if utilization is not None:
                self.usage[name] = round(utilization * 100)  # a fraction here, a percent in get_usage
        when = strftime(datetime.fromtimestamp(resets), "%-I:%M %p").lower() if resets else ""
        self.limit_note = f"usage limit reached · resets {when}".strip() if info.get("status") == "rejected" else ""
        if self.limit_note and self.busy:
            self.stalled = resets or None  # cut off mid-turn: /keepitgoin carries on at the reset
        if self.limit_note and resets:
            self.set_timer(max(0.0, resets - time.time()) + 1, lambda: self.limit_over(resets))
        self.update_footer()

    def limit_over(self, resets):
        if resets > time.time():
            return
        if self.limit_note:
            self.limit_note = ""
            self.update_footer()
            self.refresh_usage()
        self.keep_it_going()

    def keep_it_going(self):
        """/keepitgoin: the limit that cut a turn off has reset, so Jarvis carries on by itself."""
        if not (self.keep_going and self.stalled and self.stalled <= time.time()) or self.busy:
            return
        self.stalled = None
        if self.kept_going >= KEEP_GOING_TRIES:
            self.footer.flash(f"Usage limit still there after {KEEP_GOING_TRIES} tries; not carrying on", 5)
            return
        self.kept_going += 1
        self.submissions.put_nowait((KEEP_GOING_PROMPT, False))

    async def cmd_keep_going(self, args):
        arg = args.lower()
        if arg not in ("", "on", "off"):
            self.footer.flash("Usage: /keepitgoin [on|off]", 3)
            return
        self.keep_going = (arg == "on") if arg else not self.keep_going
        save_setting("keep_going", self.keep_going)
        self.update_footer()
        if self.keep_going:
            await self.note("Keep it goin' is on: when a usage limit cuts Jarvis off mid-task, it carries "
                            "on by itself once the limit resets. /keepitgoin again turns it off.")
            self.keep_it_going()  # it may have reset already
        else:
            await self.note("Keep it goin' is off: after a usage limit, Jarvis waits for you.")

    async def on_reset(self, msg):
        """/clear: a new conversation, same window."""
        await self.clear_transcript()
        self.session_id = msg.get("new_conversation_id") or self.session_id
        self.set_tab_title(None)
        await self.note("Started a new conversation. The last one stays saved: /resume brings it back.")

    async def on_engine_exit(self, msg):
        self.end_turn()
        if self.stopping_engine or self.engine_state == "connecting":
            return  # stopped on purpose, or start_engine reports it
        tail = "\n".join((msg.get("stderr") or "").splitlines()[-6:])
        text = f"Claude Code stopped (exit code {msg.get('code')})."
        if tail:
            text += f"\n{tail}"
        await self.add(NoticeEntry(notice(text + "\nSend a message to start it again.", theme.ERROR), attached=False))
        self.engine_state = "stopped"
        self.update_footer()

    # Turns ----------------------------------------------------------------------------------

    def begin_turn(self):
        self.set_finished(False)
        self.busy = True
        self.interrupting = False
        self.turn_started = time.monotonic()
        self.tokens_done = self.tokens_current = 0
        self.spinner.start()
        self.spinner.message = self.active_task()
        self.refresh_tasks()

    def end_turn(self):
        if self.busy:
            self.set_finished(True)
        self.busy = False
        self.interrupting = False
        self.turn_started = 0.0
        self.spinner.stop()
        self.blocks.clear()
        self.waiting.clear()
        self.refresh_tasks()

    async def interrupt(self):
        if not self.busy or not self.engine:
            return
        self.interrupting = True
        try:
            await self.engine.interrupt()
        except EngineError:
            self.end_turn()

    async def stop_turn(self, timeout=15):
        """Interrupt the turn in progress and wait for it to end, so the next prompt (ctrl+x s)
        starts a turn of its own instead of waiting for this one (prompts already queued still
        go first)."""
        if not self.busy:
            return
        await self.interrupt()
        deadline = time.monotonic() + timeout
        while self.busy and self.interrupting and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    def tick(self):
        # Follow new output once there's more than a screenful (anchoring sooner would sit
        # the banner at the bottom); scrolling up lets go until you're back at the end. Once
        # it all fits again (a dialog that squeezed it has closed), let go, banner at the top.
        if self.transcript.max_scroll_y > 0:
            if not self.transcript.is_anchored:
                self.transcript.anchor()
        else:
            if self.transcript.is_anchored:
                self.transcript.anchor(False)
            if self.transcript.scroll_y:  # anchoring leaves it below 0, where scroll_home won't go
                self.transcript.scroll_y = 0
        self.spinner.tick()
        self.manager_pane.tick()
        on = int(time.monotonic() * 2.5) % 2 == 0
        for entry in self.tools.values():
            if entry.state == "running":
                entry.blink(on)
        if self.footer.flash_text and time.monotonic() >= self.footer.flash_until:
            self.footer.flash_text = None
            self.footer.redraw()

    # Tasks ------------------------------------------------------------------------------------

    def track_tasks(self, name, tool_input):
        if name == "TodoWrite":
            self.todos = tool_input.get("todos") or []
        elif name == "TaskUpdate" and tool_input.get("taskId") in self.tasks:
            task = self.tasks[tool_input["taskId"]]
            if tool_input.get("status") == "deleted":
                del self.tasks[tool_input["taskId"]]
            else:
                for key in ("status", "activeForm"):
                    if tool_input.get(key):
                        task[key] = tool_input[key]
                if tool_input.get("subject"):
                    task["content"] = tool_input["subject"]
        self.refresh_tasks()

    def track_result(self, entry, data):
        if entry.tool_name == "TaskCreate" and isinstance(data, dict) and data.get("task"):
            task = data["task"]
            self.tasks[str(task.get("id"))] = {"content": task.get("subject") or entry.input.get("subject", ""),
                                               "status": "pending",
                                               "activeForm": entry.input.get("activeForm")}
            self.refresh_tasks()

    def task_list(self):
        return self.todos + list(self.tasks.values())

    def active_task(self):
        for task in self.task_list():
            if task.get("status") == "in_progress":
                return task.get("activeForm") or task.get("content")
        return None

    def refresh_tasks(self):
        tasks = self.task_list()
        if tasks and all(t.get("status") == "completed" for t in tasks) and not self.busy:
            self.todos, self.tasks = [], {}
            tasks = []
        self.task_panel.show(tasks, self.show_tasks and (self.busy or self.show_tasks_idle))
        if self.busy:
            self.spinner.message = self.active_task()

    # Permission, questions and plans ---------------------------------------------------------

    async def permission_loop(self):
        while True:
            engine, request_id, request = await self.permission_queue.get()
            if engine is not self.engine or not engine.running:
                continue
            try:
                response = await self.decide(request_id, request)
            except Exception as e:
                self.log.error(f"permission dialog: {e!r}")
                response = {"behavior": "deny", "message": f"Jarvis couldn't show the prompt: {e}"}
            if request_id in self.cancelled or engine is not self.engine:
                continue
            try:
                await engine.respond(request_id, response)
            except EngineError:
                pass

    async def decide(self, request_id, request, view=None):
        """Answer a can_use_tool request with a dialog. view is where it's shown and what it
        changes: the window, or a /manager thread (manager.Thread), each with show_dialog,
        tools and set_mode."""
        view = view or self
        name = request.get("tool_name") or ""
        if name.startswith(f"mcp__{SERVER}__"):  # the window's own tools for /manager
            return {"behavior": "allow", "updatedInput": request.get("input") or {}}
        if name == "AskUserQuestion":
            return await self.ask_questions(request_id, request.get("input") or {}, view)
        if name == "ExitPlanMode":
            return await self.approve_plan(request_id, request.get("input") or {}, view)
        return await self.ask_permission(request_id, request, view)

    async def show_dialog(self, dialog, request_id=None):
        self.menu.hide()
        self.prompt_bar.display = False
        self.footer.hint = " " if isinstance(dialog, Panel) else "Esc to cancel"  # panels say it themselves
        self.footer.redraw()
        self.dialog, self.dialog_request = dialog, request_id
        self.spinner.display = False
        await self.dock.mount(dialog)
        self.transcript.scroll_end(animate=False)
        try:
            return await dialog.result
        finally:
            self.dialog = self.dialog_request = None
            await dialog.remove()
            self.spinner.display = self.busy
            self.prompt_bar.display = True
            self.footer.hint = None
            self.footer.redraw()
            self.prompt.focus()

    def cancel_dialog(self, request_id):
        self.cancelled.add(request_id)
        if self.dialog is not None and self.dialog_request == request_id:
            self.dialog.finish(None)

    def suggestion_label(self, suggestion, tool_name):
        kind = suggestion.get("type")
        where = "in this project" if suggestion.get("destination") in ("localSettings", "projectSettings") \
            else "during this session" if suggestion.get("destination") == "session" else "from now on"
        if kind == "setMode":
            if suggestion.get("mode") == "acceptEdits":
                return "Yes, allow all edits during this session (shift+tab)"
            return f"Yes, and switch to {suggestion.get('mode')} mode"
        if kind in ("addRules", "replaceRules"):
            rules = suggestion.get("rules") or []
            names = []
            for rule in rules:
                content = rule.get("ruleContent")
                names.append(f"{content}" if rule.get("toolName") == "Bash" and content
                             else f"{rule.get('toolName')}({content})" if content else rule.get("toolName", tool_name))
            what = ", ".join(names) or tool_name
            if tool_name == "Bash":
                return f"Yes, and don't ask again for {what} commands {where}"
            return f"Yes, and don't ask again for {what} {where}"
        if kind == "addDirectories":
            dirs = ", ".join(tools.short_path(d, None) for d in suggestion.get("directories") or [])
            return f"Yes, and always allow access to {dirs} {where}"
        return "Yes, and don't ask again"

    async def ask_permission(self, request_id, request, view):
        name = request.get("tool_name") or "?"
        tool_input = request.get("input") or {}
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        short = tools.short_path(path, self.cwd)
        title = PERMISSION_TITLES.get(name) or ("MCP tool" if name.startswith("mcp__") else "Tool use")
        question = "Do you want to proceed?"
        rows = []
        if name == "Bash":
            rows = [Text(tool_input.get("command", ""), style="bold italic")]
            if tool_input.get("description"):
                rows.append(Text(tool_input["description"], style=theme.DIM))
        elif name in ("Edit", "MultiEdit"):
            edits = tool_input.get("edits") or [tool_input]
            patch = []
            for edit in edits:
                patch += preview_patch(path, edit.get("old_string", ""), edit.get("new_string", ""),
                                       edit.get("replace_all", False))
            rows = [Text(short, style="bold"), tools.DiffView(patch, path, verbose=True)]
            question = f"Do you want to make this edit to {os.path.basename(path)}?"
        elif name == "Write":
            exists = os.path.exists(path)
            if exists:
                title = "Overwrite file"
                try:
                    before = open(path, errors="replace").read()
                except OSError:
                    before = ""
                rows = [Text(short, style="bold"), tools.DiffView(hunks(before, tool_input.get("content", "")), path, verbose=True)]
                question = f"Do you want to overwrite {os.path.basename(path)}?"
            else:
                rows = [Text(short, style="bold")] + tools.numbered(tool_input.get("content", ""), path, False, limit=20)
                question = f"Do you want to create {os.path.basename(path)}?"
        elif name == "WebFetch":
            rows = [Text(tool_input.get("url", ""), style="bold")]
            if tool_input.get("prompt"):
                rows.append(Text(tools.one_line(tool_input["prompt"], 200), style=theme.DIM))
            question = "Do you want to allow Jarvis to fetch this content?"
        else:
            rows = [tools.header(name, tool_input, self.cwd)]
            if request.get("description") and request.get("description") != name:
                rows.append(Text(request["description"], style=theme.DIM))
        if request.get("decision_reason") and isinstance(request["decision_reason"], str):
            rows.append(Text(request["decision_reason"], style=theme.DIM))
        suggestions = request.get("permission_suggestions") or []
        choices = ["Yes"] + [self.suggestion_label(s, name) for s in suggestions]
        choices.append("No, and tell Jarvis what to do differently (esc)")
        dialog = Dialog(title, Static(Group(*rows)), question, choices)
        self.notify_hooks("permission_prompt", f"Claude needs your permission to use {name}")
        entry = view.tools.get(request.get("tool_use_id"))
        if entry is not None:
            entry.set_waiting(True)
        try:
            picked = await view.show_dialog(dialog, request_id)
        finally:
            if entry is not None:
                entry.set_waiting(False)
        if picked is None or picked == len(choices) - 1:
            if entry is not None:
                entry.mark("rejected")
            return {"behavior": "deny", "message": REJECTED, "interrupt": True}
        response = {"behavior": "allow", "updatedInput": tool_input}
        if 1 <= picked <= len(suggestions):
            suggestion = suggestions[picked - 1]
            response["updatedPermissions"] = [suggestion]
            if suggestion.get("type") == "setMode":
                view.set_mode(suggestion.get("mode"))
        return response

    def notify_hooks(self, kind, message):
        """Run the settings' Notification hooks, as Claude Code does when it shows a prompt.

        With its prompts handed to this window, headless Claude Code doesn't fire them itself,
        and they're how Jarvis says "Sir, Claude needs your permission" (deploy/claude-hook.py).
        """
        groups = self.hooks.get("Notification") or []
        payload = json.dumps({"session_id": self.session_id or "", "cwd": self.cwd,
                              "hook_event_name": "Notification", "notification_type": kind,
                              "message": message,
                              "transcript_path": sessions.find_session(self.cwd, self.session_id or "-") or ""})
        for group in groups:
            matcher = group.get("matcher") or ""
            try:
                if matcher not in ("", "*") and not re.fullmatch(matcher, kind):
                    continue
            except re.error:
                continue
            for hook in group.get("hooks") or []:
                if hook.get("type") == "command" and hook.get("command"):
                    self.run_worker(self.run_hook(hook["command"], payload, hook.get("timeout", 60)),
                                    group="hooks")

    async def run_hook(self, command, payload, timeout):
        env = clean_environment()
        env["CLAUDE_PROJECT_DIR"] = self.cwd
        try:
            proc = await asyncio.create_subprocess_shell(
                command, cwd=self.cwd, env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(proc.communicate(payload.encode()), timeout)
        except (OSError, asyncio.TimeoutError):
            pass

    async def ask_questions(self, request_id, tool_input, view):
        answers = {}
        questions = tool_input.get("questions") or []
        for n, q in enumerate(questions, 1):
            options = [(o.get("label", ""), o.get("description", "")) for o in q.get("options") or []]
            multi = bool(q.get("multiSelect"))
            choices = options + [("Type something else…", "")]
            if multi:
                choices.append(("Submit", ""))
            header = q.get("header") or "Question"
            count = f" ({n}/{len(questions)})" if len(questions) > 1 else ""
            dialog = Dialog(f"☐ {header}{count}", None, q.get("question", ""), choices,
                            text_choice=len(options), multi=multi,
                            hint="Enter to choose" + (" · Space to tick" if multi else "") + " · Esc to cancel")
            picked = await view.show_dialog(dialog, request_id)
            if picked is None:
                return {"behavior": "deny", "message": "The user declined to answer the questions.",
                        "interrupt": True}
            if isinstance(picked, tuple):
                answers[q.get("question", "")] = picked[1]
            elif multi:
                labels = [options[i][0] for i in sorted(dialog.picked) if i < len(options)]
                answers[q.get("question", "")] = ", ".join(labels)
            else:
                answers[q.get("question", "")] = options[picked][0]
        return {"behavior": "allow", "updatedInput": {**tool_input, "answers": answers}}

    async def approve_plan(self, request_id, tool_input, view):
        plan = tool_input.get("plan") or ""
        if not plan and tool_input.get("planFilePath"):
            try:
                plan = open(tool_input["planFilePath"], errors="replace").read()
            except OSError:
                pass
        plan_view = VerticalScroll(Markdown(plan or "(no plan text)", classes="plan"), id="plan-view")
        choices = ["Yes, and use auto mode", "Yes, and auto-accept edits",
                   "Yes, and manually approve edits", "No, keep planning (tell Jarvis what to change)"]
        dialog = Dialog("Ready to code? Here is Jarvis's plan:", plan_view, "Would you like to proceed?",
                        choices, text_choice=3)
        picked = await view.show_dialog(dialog, request_id)
        if picked is None:
            return {"behavior": "deny", "message": "The user rejected the plan and wants to give "
                    "other instructions. Wait for them.", "interrupt": True}
        if isinstance(picked, tuple):
            return {"behavior": "deny", "message": f"The user wants to keep planning. Their feedback: {picked[1]}"}
        mode = ["auto", "acceptEdits", "default"][picked]
        view.set_mode(mode)
        return {"behavior": "allow", "updatedInput": tool_input,
                "updatedPermissions": [{"type": "setMode", "mode": mode, "destination": "session"}]}

    # The prompt -----------------------------------------------------------------------------

    async def on_prompt_input_submitted(self, event):
        text = event.text
        if not text.strip():
            if event.now:
                self.send_queued_now()
            return
        self.prompt.clear()
        self.menu.hide()
        if text.strip().lower() == "/fuck":  # here, not in the queue, so nothing ahead of it holds it up
            if self.busy:
                self.run_worker(self.interrupt(), group="interrupt")
            else:
                self.footer.flash("Nothing to stop")
            return
        self.history_index = -1
        if self.jumped is not None:
            self.unjump()
            self.transcript.scroll_end(animate=False)
        if text in self.history:
            self.history.remove(text)
        self.history.insert(0, text)
        sessions.add_history(text, self.cwd, self.session_id)
        if self.reply and not text.strip().startswith(("/", "!")):  # a command keeps the reply for the next prompt
            text = quote_reply(self.reply, text)
            self.set_reply(None)
        self.submissions.put_nowait((text, event.now))

    def send_queued_now(self):
        """ctrl+x s on an empty prompt: the prompts queued behind the turn in progress run
        straight away (Claude Code stops that turn for them, or moves what it waits on to the
        background)."""
        if not self.queued:
            self.footer.flash("Nothing queued")
            return
        self.run_worker(self.send_waiting_now(), group="interrupt")

    async def send_waiting_now(self):
        if not self.busy or not self.engine:
            return
        if "interrupt_send_now_v1" not in self.capabilities:
            await self.interrupt()  # an older Claude Code: it keeps the queue through an interrupt
            return
        self.interrupting = True  # if the turn is stopped, its result reads as an interruption
        try:
            done = await self.engine.send_now()
        except EngineError as e:
            done = None
            self.footer.flash(str(e), 3)
        if done != "stopped" and self.busy:
            self.interrupting = False

    def unqueue(self, message_uuid=None):
        """ctrl+q, or a queued prompt's ✕: take it back out of Claude Code's queue (ctrl+q: the
        newest). It stays in the prompt history."""
        waiting = [item for item in self.sent if not item[1]]
        if not waiting:
            self.footer.flash("Nothing queued")
            return
        item = next((item for item in waiting if item[2] == message_uuid), None) if message_uuid else waiting[-1]
        if item is not None and self.engine is not None:
            self.run_worker(self.cancel_queued(item), group="unqueue")

    async def cancel_queued(self, item):
        try:
            cancelled = await self.engine.cancel_queued(item[2])
        except EngineError as e:
            self.footer.flash(f"Couldn't unqueue it: {e}", 3)
            return
        if not cancelled:
            self.footer.flash("Too late to unqueue: Jarvis has taken it in", 3)
            return
        self.sent = [other for other in self.sent if other is not item]
        self.show_queued()
        self.footer.flash("Unqueued · ↑ brings it back", 3)

    def on_queued_panel_unqueue(self, event):
        self.unqueue(event.message_uuid)

    def open_url(self, url, *, new_tab=True):
        """A link clicked in a reply (Textual calls this): a web link opens in the browser, and
        a file Claude linked to (a path from the folder, maybe with #L42) in its own app. Where
        nothing can be opened (over ssh, or no display) it's copied instead, so it can be
        pasted on the computer in front of the owner."""
        target, what = url, "link"
        if url.lower().startswith("file://"):
            target, what = unquote(urlparse(url).path), "path"
        elif not re.match(r"[a-z][a-z0-9+.-]+:", url, re.I) and not url.startswith("#"):
            path = os.path.join(self.cwd, os.path.expanduser(unquote(url.split("#")[0])))
            if os.path.exists(path):
                target, what = os.path.abspath(path), "path"
        if open_here(target):
            self.footer.flash(f"Opened {url}", 2)
            return
        self.copy_text(target)
        why = "over ssh" if over_ssh() else "with no browser here"
        self.footer.flash(f"Copied the {what} ({why}): {target}", 4)

    def copy_text(self, text):
        """Copies text by OSC 52, for terminals that take it (and over ssh), and by the system's
        own clipboard command, for those that don't."""
        self.copy_to_clipboard(text)
        system_copy(text)

    def on_text_selected(self, event):
        """Text selected with the mouse is copied, as a terminal would."""
        selected = self.screen.get_selected_text()
        if selected:
            self.copy_text(selected)
            self.footer.flash("Copied")

    async def submission_loop(self):
        """Prompts run here, one at a time and in order, never in a key handler: a command
        that opens a dialog (/resume, /model) waits for keys a key handler would hold up."""
        while True:
            text, now = await self.submissions.get()
            try:
                if callable(text):  # a picker opened by a key (left arrow)
                    await text()
                    continue
                if now:
                    await self.stop_turn()
                await self.submit(text)
            except Exception as e:
                self.log.error(f"submitting {str(text)[:40]!r}: {e!r}")
                await self.note(f"Jarvis hit a problem: {e}", theme.ERROR)

    async def submit(self, text):
        stripped = text.strip()
        if stripped.startswith("!"):
            await self.run_shell(stripped[1:].strip())
            return
        if stripped.startswith("/") and not stripped.startswith("//"):
            await self.slash(stripped)
            return
        await self.send_prompt(text)

    async def ensure_engine(self):
        if self.engine is not None and self.engine.running:
            return True
        if self.engine_state == "connecting" and self.engine is not None:
            await self.note("Still connecting to Claude Code; try again in a moment.")
            return False
        await self.note("Starting Claude Code again…")
        return await self.start_engine(resume=self.session_id)

    async def send_prompt(self, text):
        if not await self.ensure_engine():
            return
        content = text
        if self.pending_context:
            content = "\n".join(self.pending_context) + "\n\n" + text
            self.pending_context = []
        message_uuid = str(uuid.uuid4())
        if self.busy:
            self.sent.append([text, False, message_uuid])
            self.show_queued()
        else:
            await self.add(UserEntry(text))
            self.sent.append([text, True, message_uuid])
            self.begin_turn()
        try:
            await self.engine.send(content, message_uuid)
        except EngineError as e:
            await self.note(str(e), theme.ERROR)

    def on_text_area_changed(self, event):
        if event.text_area is not self.prompt:
            return
        text = self.prompt.text
        if text:
            self.set_finished(False)  # you're back at it: the tab's ✓ has been seen
        if self.history_index >= 0 and text != self.history[self.history_index]:
            self.history_index = -1  # typing after the arrows starts a new prompt
        self.prompt_bar.set_bash(text.startswith("!"))
        self.refresh_menu()

    def refresh_menu(self):
        """Show what matches the prompt as it is now (called again before using the menu,
        since a change can still be on its way when Enter arrives)."""
        text = self.prompt.text
        row, col = self.prompt.cursor_location
        before = self.prompt.document.get_line(row)[:col]
        if text == "?":
            self.menu.show("help", [])
            self.menu.display = True
            self.menu.update(shortcuts())
            return
        if re.fullmatch(r"/[\w:.-]*", text):
            self.menu.show("command", self.command_matches(text[1:]))
            return
        match = re.search(r"(?:^|\s)@([^\s]*)$", before)
        if match:
            self.menu.show("file", [(p, p, "") for p in self.file_index.search(match.group(1))])
            return
        self.menu.hide()

    def command_matches(self, query):
        q = query.lower()
        found = {}
        for name, description in LOCAL_COMMANDS.items():
            found[name] = description
        for name, command in self.commands.items():
            if name.startswith("__") or "(removed)" in (command.get("description") or ""):
                continue
            found.setdefault(name, command.get("description") or "")
        ranked = []
        for name, description in found.items():
            if name.startswith(q):
                ranked.append((0, len(name), name, description))
            elif q and q in name:
                ranked.append((1, len(name), name, description))
            elif q and q in description.lower():
                ranked.append((2, len(name), name, description))
        ranked.sort(key=(lambda r: r[2]) if not q else None)
        return [(name, f"/{name}", description) for _, _, name, description in ranked[:40]]

    def menu_accept(self, run):
        self.refresh_menu()
        if not self.menu.open or self.menu.kind not in ("command", "file"):
            return False
        value = self.menu.current()[0]
        if self.menu.kind == "command":
            self.menu.hide()
            if run:
                self.prompt.clear()
                self.prompt.post_message(self.prompt.Submitted(f"/{value}"))
            else:
                self.prompt.set_text(f"/{value} ")
            return True
        row, col = self.prompt.cursor_location
        line = self.prompt.document.get_line(row)
        start = line.rfind("@", 0, col)
        self.prompt.replace(f"@{value}" + ("" if value.endswith("/") else " "), (row, start), (row, col))
        if not value.endswith("/"):
            self.menu.hide()
        return True

    def menu_move(self, step):
        if not self.menu.open or self.menu.kind not in ("command", "file"):
            return False
        if self.menu.kind == "command" and not re.fullmatch(r"/[\w:.-]*", self.prompt.text):
            self.menu.hide()
            return False
        self.menu.move(step)
        return True

    def menu_close(self):
        if self.menu.display:
            self.menu.hide()
            return True
        return False

    def history_move(self, step):
        if not self.history:
            return False
        if self.history_index == -1:
            self.history_draft = self.prompt.text
        index = self.history_index + (1 if step < 0 else -1)
        if index < -1 or index >= len(self.history):
            return True
        self.history_index = index
        self.prompt.set_text(self.history_draft if index == -1 else self.history[index])
        self.menu.hide()
        return True

    # Commands -------------------------------------------------------------------------------

    async def slash(self, text):
        name, _, args = text[1:].partition(" ")
        name, args = name.lower(), args.strip()
        name = ALIASES.get(name, name)
        if name == "exit":
            await self.quit()
        elif name == "help":
            await self.show_help()
        elif name == "resume":
            await self.cmd_resume(args)
        elif name == "model" and not args:
            await self.cmd_model()
        elif name == "effort" and not args:
            await self.cmd_effort()
        elif name == "default-usage":
            await self.cmd_default_effort(args)
        elif name in SETTINGS_TABS and not (name == "config" and args):
            await self.show_dialog(SettingsPanel(SETTINGS_TABS[name]))
        elif name == "config":
            await self.cmd_config_set(args)
        elif name == "mcp" and not args:
            await self.show_dialog(McpPanel())
        elif name == "copy":
            await self.cmd_copy()
        elif name == "export":
            await self.cmd_export(args)
        elif name == "claude":
            await self.cmd_claude()
        elif name == "btw":
            self.cmd_btw(args)
        elif name == "jarvis":
            await self.cmd_jarvis(args)
        elif name == "manager":
            await self.cmd_manager(args)
        elif name == "manager-thread-limit":
            await self.cmd_thread_limit(args)
        elif name == "keepitgoin":
            await self.cmd_keep_going(args)
        elif name == "finished":
            await self.cmd_finished(args)
        elif name == "computer":
            await self.note(f"Running on {platform.node()} ({platform.system()} {platform.release()})")
        elif name in ONLY_IN_CLAUDE and name not in self.commands:
            await self.add(UserEntry(text))
            await self.add(NoticeEntry(notice(f"/{name} is one of regular Claude Code's own screens. Type /claude to "
                                              "open this conversation there, use it, then exit to come back.")))
        else:
            await self.run_engine_command(f"/{name}" + (f" {args}" if args else ""))

    async def run_engine_command(self, text):
        if not await self.ensure_engine():
            return
        await self.add(UserEntry(text))
        if text.split()[0] in ("/model", "/effort", "/fast", "/config", "/clear"):
            self.commands_pending = True
            if text.startswith("/model ") or text.startswith("/effort "):
                key, value = text[1:].split(" ", 1)
                if key == "model":
                    self.model_override = value.strip()
                else:
                    self.effort_override = value.strip()
        if not self.busy:
            self.begin_turn()
        try:
            await self.engine.send(text)
        except EngineError as e:
            await self.note(str(e), theme.ERROR)

    async def cmd_jarvis(self, args):
        """/jarvis settings: the voice assistant's own settings, on a page of their own."""
        if args.lower() not in ("", "settings", "setting", "config"):
            self.footer.flash("Usage: /jarvis settings", 3)
            return
        self.menu.hide()
        panel = JarvisSettingsPanel()
        self.dialog, self.dialog_request = panel, None
        page = JarvisSettingsPage(panel)
        await self.push_screen(page)
        try:
            await panel.result
        finally:
            self.dialog = None
            if self.screen_stack and self.screen is page:  # not if the window is closing
                await self.pop_screen()
                self.prompt.focus()

    def cmd_btw(self, question):
        if not question:
            self.footer.flash("Usage: /btw <your question>", 3)
            return
        self.side_answer.show(question)
        self.run_worker(self.side_question(question), group="btw", exclusive=True)

    async def side_question(self, question):
        extra = ["--tools", "", "--strict-mcp-config",
                 "--append-system-prompt", (persona.PERSONA + "\n\n" if self.options.persona else "") + BTW_PROMPT]
        if self.session_id:
            extra += ["--resume", self.session_id, "--fork-session"]
        if self.model:
            extra += ["--model", self.model]
        if self.effort:
            extra += ["--effort", self.effort]
        try:
            answer = await self.claude_print(question, timeout=BTW_TIMEOUT, extra=extra)
        except EngineError as e:
            self.side_answer.show(question, f"Couldn't answer: {e}", error=True)
            return
        self.side_answer.show(question, answer or "(No answer)")

    def dismiss_side_answer(self):
        self.workers.cancel_group(self, "btw")
        self.side_answer.hide()

    async def cmd_manager(self, args):
        """/manager [what to do]: this conversation becomes the coordinator, with tools to hand
        tasks to worker Jarvises shown on the right. /manager off stops them all."""
        if len(args) >= 2 and args[0] == args[-1] and args[0] in "\"'":
            args = args[1:-1].strip()
        if args.lower() == "off":
            await self.leave_manager()
            return
        if self.manager is None:
            if self.busy:
                await self.note("Jarvis is in the middle of something; try /manager once it's done.")
                return
            self.manager = Manager(self, self.manager_pane)
            self.manager_pane.display = True
            await self.stop_engine()  # again with the coordinator's prompt and tools
            await self.note("Project manager mode: Jarvis coordinates here and hands the work to threads on "
                            "the right. → on an empty prompt goes to them; /manager off stops them.")
            if not await self.start_engine(resume=self.session_id):
                return
        elif not args:
            self.show_threads()
            return
        if args:
            await self.send_prompt(args)

    async def cmd_thread_limit(self, args):
        """/manager-thread-limit [n]: how many /manager threads may work at once, counting
        every level (saved, for new windows too)."""
        if not args:
            await self.note(f"Up to {self.thread_limit} /manager threads work at once, counting every level. "
                            f"/manager-thread-limit <n> changes it (default {THREAD_LIMIT}).")
            return
        value = THREAD_LIMIT if args.lower() == "default" else int(args) if args.isdigit() else 0
        if not 1 <= value <= MAX_THREAD_LIMIT:
            self.footer.flash(f"Usage: /manager-thread-limit <1 to {MAX_THREAD_LIMIT}|default>", 3)
            return
        self.thread_limit = value
        save_setting("manager_thread_limit", None if value == THREAD_LIMIT else value)
        await self.note(f"Up to {value} /manager thread{'s' if value != 1 else ''} work at once now, "
                        "counting every level.")

    async def leave_manager(self):
        if self.manager is None:
            self.footer.flash("Project manager mode isn't on")
            return
        await self.stop_turn()
        await self.close_manager()
        if self.session_id:  # a plain conversation again: its threads aren't kept for it
            sessions.save_manager(self.session_id, [])
        await self.stop_engine()
        await self.start_engine(resume=self.session_id)
        await self.note("Project manager mode is off; its threads are stopped.")

    async def close_manager(self):
        """Stop the threads and close the pane; what was saved of them stays."""
        if self.manager is not None:
            manager, self.manager = self.manager, None
            await manager.close()
            self.manager_pane.display = False

    async def reopen_manager(self, session_id):
        """A conversation that was in manager mode: its threads back on the right as they were,
        each picked up again when it's next messaged. Call before the coordinator starts."""
        saved = await asyncio.to_thread(sessions.load_manager, session_id)
        if not saved:
            return
        self.manager = Manager(self, self.manager_pane)
        self.manager_pane.display = True
        await self.manager.restore(saved)
        count = len(self.manager.threads)
        await self.note(f"Project manager mode, with its {count} thread{'s' if count != 1 else ''} on the right. "
                        "→ on an empty prompt goes to them; /manager off stops them.")

    def show_threads(self):
        """Right arrow on an empty prompt in manager mode: the threads."""
        if self.manager is None or self.dialog is not None or self.menu.display:
            return False
        self.manager_pane.focus_list()
        return True

    async def send_update(self, text):
        """News from a /manager thread for the coordinator: shown as a line in the transcript
        when Claude Code takes it in, not as a message of yours."""
        if self.manager is None or self.engine is None or not self.engine.running:
            return
        message_uuid = str(uuid.uuid4())
        self.updates.add(message_uuid)
        try:
            await self.engine.send(text, message_uuid)
        except EngineError as e:
            self.updates.discard(message_uuid)
            await self.note(str(e), theme.ERROR)

    async def show_help(self):
        await self.add(UserEntry("/help"))
        local = [Text.assemble((f"/{name}", f"bold {theme.ACCENT}"), "  ", (d, theme.DIM))
                 for name, d in LOCAL_COMMANDS.items()]
        engine = [n for n, c in self.commands.items() if not n.startswith("__") and n not in LOCAL_COMMANDS]
        rows = [Text(f"Jarvis {__version__}, with Claude Code v{self.info.get('claude_code_version', '?')} "
                     "doing the work underneath.", style="bold"), Text(""),
                Text("Shortcuts", style=f"bold {theme.ACCENT}"), shortcuts(), Text(""),
                Text("Jarvis's commands", style=f"bold {theme.ACCENT}"), *local, Text(""),
                Text("Claude Code's commands and skills (type / to see them with descriptions)",
                     style=f"bold {theme.ACCENT}"),
                Text("  ".join(f"/{n}" for n in sorted(engine)), style=theme.DIM)]
        await self.add(NoticeEntry(tools.block(rows)))

    async def pick_session(self, everywhere=False, new=False):
        """A conversation to resume: from this folder, or (everywhere) from any folder. With
        new, typing starts a new one instead, returned as ("new", prompt); ctrl+f searches."""
        if everywhere:
            found = await asyncio.to_thread(sessions.list_all_sessions)
        else:
            found = sessions.list_sessions(self.cwd)
        if not found and not new:
            await self.note("No earlier conversations." if everywhere else "No earlier conversations in this folder.")
            return None

        def about(s):
            parts = [sessions.time_ago(s.modified), f"{s.prompts} prompt{'s' if s.prompts != 1 else ''}"]
            if everywhere and s.cwd:
                parts.append(tools.short_path(s.cwd, None))
            if s.branch:
                parts.append(f"⎇ {s.branch}")
            return " · ".join(parts)

        items = [(s.id, tools.one_line(f"Manager mode · {s.title}" if s.manager else s.title, 70), about(s))
                 for s in found]
        finished = {s.id for s in found if s.finished}  # a ✓ where the number goes
        title = f"Resume a conversation ({len(found)}, every folder)" if everywhere else "Resume a conversation"
        if new:
            title = title.replace("Resume a conversation", "Conversations", 1)
            picker = Picker(title, items, new_text="Type to start a new conversation…",
                            open_on_right=True, marked=finished)
        else:
            picker = Picker(title, items, open_on_right=True, marked=finished,
                            hint="Type to filter · ↑↓ to scroll · Enter or → to pick · Esc to cancel")
        return await self.show_page(picker)

    async def show_page(self, picker):
        """A list of conversations, on a page of its own in place of this one."""
        self.menu.hide()
        self.dialog, self.dialog_request = picker, None
        page = SessionsPage(picker)
        await self.push_screen(page)
        try:
            return await picker.result
        finally:
            self.dialog = None
            if self.screen_stack and self.screen is page:  # not if the window is closing
                await self.pop_screen()
                self.prompt.focus()

    def show_all_sessions(self):
        """Left arrow on an empty prompt: every saved conversation, from every folder."""
        if self.dialog is not None or self.menu.display or self.prompt.text:
            return False
        self.submissions.put_nowait((self.cmd_all_sessions, False))
        return True

    async def cmd_all_sessions(self):
        picked = await self.pick_session(everywhere=True, new=True)
        if isinstance(picked, tuple):
            await self.new_session(picked[1])
        elif picked:
            await self.switch_session(picked)

    async def new_session(self, text):
        """A new conversation, in this folder, started with text."""
        await self.stop_engine()
        await self.close_manager()
        await self.clear_transcript()
        self.set_tab_title(None)
        self.history_index = -1
        if text in self.history:
            self.history.remove(text)
        self.history.insert(0, text)
        sessions.add_history(text, self.cwd, None)
        if await self.start_engine():
            await self.submit(text)

    async def cmd_finished(self, args):
        """/finished: mark this conversation done, shown so in the list; /finished off undoes it."""
        if not self.session_id:
            await self.note("There's no conversation to mark yet.")
            return
        undo = args.strip().lower() in ("off", "undo", "no")
        await asyncio.to_thread(sessions.set_finished, self.session_id, not undo)
        await self.note("This conversation is no longer marked finished." if undo else
                        "Marked this conversation as finished: the list of conversations shows it so. "
                        "/finished off undoes it.")

    async def cmd_resume(self, args):
        session_id = args or await self.pick_session()
        if session_id:
            await self.switch_session(session_id)

    async def switch_session(self, session_id):
        path = sessions.find_session(self.cwd, session_id)
        if not path:
            await self.note(f"No conversation {session_id} found.", theme.ERROR)
            return
        await self.stop_engine()
        await self.close_manager()
        await self.clear_transcript()
        cwd = sessions.summarize(path)[4]
        if cwd and cwd != self.cwd and os.path.isdir(cwd):
            self.change_dir(cwd)  # Claude Code resumes a conversation from the folder it ran in
            await self.note(f"Now in {tools.short_path(cwd, None)}")
        await self.show_history(path)
        await self.reopen_manager(os.path.basename(path)[:-6])
        self.set_tab_title(None)
        if await self.start_engine(resume=os.path.basename(path)[:-6]):
            self.name_conversation()

    def change_dir(self, cwd):
        """Move the window to another folder: a conversation from there is being resumed."""
        self.cwd = cwd
        os.chdir(cwd)
        self.file_index = files.FileIndex(cwd)
        self.run_worker(self.file_index.load(), group="files")
        self.branch = git_branch(cwd)
        self.banner.set_info(cwd=tools.short_path(cwd, None), branch=self.branch)
        self.history = sessions.read_history(cwd)
        self.history_index = -1

    def name_conversation(self):
        """Put what this conversation is about in the terminal tab, once per conversation."""
        if self.session_id and self.titled != self.session_id:
            self.titled = self.session_id
            self.run_worker(self.find_title(self.session_id), group="title")

    async def find_title(self, session_id):
        path = sessions.find_session(self.cwd, session_id)
        if not path:
            return
        title = await asyncio.to_thread(sessions.saved_title, path)
        if not title:
            first = (await asyncio.to_thread(sessions.summarize, path))[0]
            if first == "(no prompt)":
                self.titled = None  # nothing to go on yet: try again after the next turn
                return
            try:
                answer = await self.claude_print(TITLE_PROMPT.format(first[:2000]), extra=TITLE_ARGS)
            except EngineError:
                return
            title = tools.one_line(answer.strip().strip('"\'*#').rstrip("."), 60)
            if not title or session_id != self.session_id:
                return
            try:
                await asyncio.to_thread(sessions.add_title, path, session_id, title)
            except OSError:
                pass
        if session_id == self.session_id:
            self.set_tab_title(title)

    def set_tab_title(self, title):
        """The terminal tab's title: the conversation's, or Jarvis (None) for a new one."""
        self.tab_title = title
        if title is None:
            self.titled = None
        self.show_tab_title()

    def set_finished(self, finished):
        if finished != self.finished:
            self.finished = finished
            self.show_tab_title()

    def show_tab_title(self):
        clean = re.sub(r"[\x00-\x1f\x7f]", "", ("✓ " if self.finished else "") + (self.tab_title or "Jarvis"))
        self.title_shown = clean
        driver = getattr(self, "_driver", None)
        if sys.stdout.isatty():
            if driver is not None:
                driver.write(f"\033]0;{clean}\007")
            console_title(clean)

    async def show_history(self, path):
        messages = sessions.load_conversation(path)
        if len(messages) > HISTORY_LIMIT:
            skipped = len(messages) - HISTORY_LIMIT
            messages = messages[-HISTORY_LIMIT:]
            await self.note(f"… {skipped} earlier records not shown (they're still in the conversation)")
        # Build every entry first and mount them together: one layout instead of hundreds.
        self.batch = []
        try:
            for msg in messages:
                if msg["type"] == "assistant":
                    await self.on_assistant(msg)
                elif msg["type"] == "user":
                    await self.on_user(msg)
                elif msg["type"] == "system":
                    await self.on_system(msg)
            for entry in self.tools.values():
                if entry.state == "running":
                    entry.mark("done")
        finally:
            batch, self.batch = self.batch, None
        await self.transcript.mount_all(batch)
        self.transcript.scroll_end(animate=False)

    async def cmd_model(self):
        items = [(m["value"], m.get("displayName") or m["value"], m.get("description", ""))
                 for m in self.models]
        value = await self.show_dialog(Picker("Choose a model", items))
        if value:
            await self.run_engine_command(f"/model {value}")

    def effort_levels(self):
        for m in self.models:
            if m.get("resolvedModel") == self.model and m.get("supportedEffortLevels"):
                return m["supportedEffortLevels"]
        return EFFORT_LEVELS

    async def cmd_effort(self):
        items = [(level, level + ("  (current)" if level == self.effort else ""), EFFORT_ABOUT.get(level, ""))
                 for level in self.effort_levels()]
        value = await self.show_dialog(Picker("Choose an effort level", items))
        if value:
            await self.run_engine_command(f"/effort {value}")

    async def cmd_default_effort(self, args):
        """/default-usage: the effort new windows start with (saved; --effort still overrides
        it), and this window's too."""
        saved = load_setting("default_effort", None)
        value = args.lower()
        if not value:
            items = [(level, level + ("  (default)" if level == saved else ""), EFFORT_ABOUT.get(level, ""))
                     for level in EFFORT_LEVELS]  # every level: a new window may be on another model
            items.append(("off", "off" + ("" if saved else "  (default)"), "Whatever Claude Code itself starts on"))
            value = await self.show_dialog(Picker("Choose the effort new windows start with", items))
            if not value:
                return
        if value not in EFFORT_LEVELS + ["off"]:
            self.footer.flash(f"Usage: /default-usage <{'|'.join(EFFORT_LEVELS)}|off>", 3)
            return
        save_setting("default_effort", None if value == "off" else value)
        if value == "off":
            await self.note("New windows start on Claude Code's own effort level.")
            return
        await self.note(f"New windows start on {value} effort.")
        if value != self.effort and value in self.effort_levels():
            await self.run_engine_command(f"/effort {value}")

    # Data for the menus (panels.py) -------------------------------------------------------------

    async def claude_print(self, prompt, timeout=60, extra=()):
        """Run one command in a separate, unsaved Claude Code (`claude -p ... --no-session-
        persistence`), so it never lands in the conversation; returns what it printed."""
        argv = claude_command(self.options.engine_command) + ["-p", prompt, "--no-session-persistence", *extra]
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=self.cwd, env=clean_environment(), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **DETACHED)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise EngineError("Claude Code took too long") from None
        except asyncio.CancelledError:  # a /btw dismissed before its answer came
            proc.kill()
            raise
        text = out.decode(errors="replace").strip()
        if proc.returncode and not text:
            raise EngineError(err.decode(errors="replace").strip()[-300:] or f"exit code {proc.returncode}")
        return text

    def live_engine(self):
        if self.engine is None or not self.engine.running:
            raise EngineError("Claude Code isn't running")
        return self.engine

    def pretty_model(self, model):
        for m in self.models:
            if m.get("value") != "default" and m.get("resolvedModel") == model:
                return m.get("displayName") or pretty_model(model)
        return pretty_model(model)

    def mcp_summary(self, servers):
        counts = {}
        for server in servers:
            counts[server.get("status")] = counts.get(server.get("status"), 0) + 1
        parts = []
        for status, words in (("connected", "connected"), ("needs-auth", "need auth"), ("failed", "failed"),
                              ("pending", "connecting"), ("disabled", "disabled")):
            if counts.get(status):
                parts.append(f"{counts[status]} {words}")
        if parts:
            return ", ".join(parts) + " · /mcp"
        return "connecting… · /mcp" if time.monotonic() - self.engine_started < 30 else "none"

    async def panel_status(self):
        status = await self.live_engine().request("get_status", timeout=15)
        rows = []
        for group in status.get("sections") or []:
            for row in group.get("rows") or []:
                label, value = row.get("label"), row.get("value", "")
                if not label or label == "Peer address":
                    continue
                if label == "Model":
                    value = " · ".join(x for x in (self.model_label() or value,
                                                   self.effort and f"{self.effort} effort") if x)
                rows.append((label, value))
                if label == "Version":
                    rows.append(("Jarvis", f"{__version__} (this window)"))
                if label == "Session ID" and self.session_id:
                    path = sessions.find_session(self.cwd, self.session_id)
                    if path:
                        title = sessions.summarize(path)[0]
                        rows.insert(len(rows) - 1, ("Session name", tools.one_line(title, 70)))
        try:
            servers = await self.panel_mcp()
        except EngineError:
            servers = self.mcp_servers
        rows.append(("MCP servers", self.mcp_summary(servers)))
        rows.append(("Permission mode", theme.MODE_LOOK.get(self.mode, ("", self.mode, ""))[1] or "default"))
        return rows

    async def panel_usage(self):
        engine = self.live_engine()
        data = await engine.request("get_usage", timeout=90)
        try:
            data["cost_text"] = (await engine.request("get_session_cost", timeout=15)).get("text")
        except EngineError:
            pass
        return data

    async def panel_config(self):
        if self.config_options is None:
            self.config_options = config_items.parse_options(await self.claude_print("/config"))
        settings = {}
        if self.engine is not None and self.engine.running:
            settings = (await self.engine.request("get_settings", timeout=15)).get("effective") or {}
        try:
            with open(os.path.expanduser("~/.claude.json")) as f:
                global_config = json.load(f)
        except (OSError, ValueError):
            global_config = {}
        return config_items.build(self.config_options, settings, global_config)

    async def panel_set_config(self, item_id, value):
        said = await self.claude_print(f"/config {item_id}={value}")
        if item_id == "model" and self.engine is not None and self.engine.running:
            self.model_override = None if value == "default" else value
            try:
                await self.engine.request("set_model", model=self.model_override)
            except EngineError:
                pass
        await asyncio.sleep(0.6)  # Claude Code notices the settings file change by then
        await self.refresh_settings()
        return said.strip().splitlines()[-1] if said.strip() else ""

    async def panel_stats(self):
        when, cached = self.stats_cache
        if cached is None or time.monotonic() - when > 60:
            cached = await asyncio.to_thread(stats.load)
            self.stats_cache = (time.monotonic(), cached)
        return cached

    async def panel_mcp(self):
        servers = []
        if self.engine is not None and self.engine.running:
            servers = (await self.engine.request("mcp_status", timeout=20)).get("mcpServers") or []
        if not servers:  # they connect with the first message; until then, what init said
            servers = [dict(s) for s in self.mcp_servers]
        return servers

    async def panel_mcp_action(self, name, action):
        engine = self.live_engine()
        if action == "reconnect":
            await engine.request("mcp_reconnect", serverName=name, timeout=90)
            return f"Reconnected {name}"
        if action in ("enable", "disable"):
            await engine.request("mcp_toggle", serverName=name, enabled=action == "enable", timeout=30)
            return f"{action.capitalize()}d {name}"
        if action == "clear-auth":
            await engine.request("mcp_clear_auth", serverName=name, timeout=30)
            return f"Cleared {name}'s sign-in"
        return ""

    def fill_prompt(self, text):
        self.prompt.set_text(text)
        self.footer.flash("The fix is in the prompt: Enter sends it", 4)

    async def cmd_config_set(self, args):
        await self.add(UserEntry(f"/config {args}"))
        try:
            said = await self.claude_print(f"/config {args}")
        except EngineError as e:
            await self.add(NoticeEntry(notice(str(e), theme.ERROR)))
            return
        await self.add(NoticeEntry(self.command_output(said or "Done")))
        self.run_worker(self.refresh_settings(), group="settings")

    async def cmd_copy(self):
        replies = [e for e in self.transcript.children if isinstance(e, AssistantEntry)]
        if not replies:
            self.footer.flash("Nothing to copy yet")
            return
        self.copy_text(replies[-1].text)
        self.footer.flash("Copied Jarvis's last reply")

    async def cmd_export(self, args):
        name = args or time.strftime("jarvis-%Y-%m-%d-%H%M.txt")
        path = os.path.join(self.cwd, os.path.expanduser(name))
        text = "\n\n".join(p for p in (e.plain() for e in self.transcript.children if isinstance(e, Entry)) if p)
        try:
            with open(path, "w") as f:
                f.write(text + "\n")
        except OSError as e:
            await self.note(f"Couldn't save: {e}", theme.ERROR)
            return
        await self.note(f"Saved this conversation to {tools.short_path(path, self.cwd)}")

    async def cmd_claude(self):
        session_id = self.session_id
        await self.stop_engine()
        argv = [claude_path()] + (["--resume", session_id] if session_id else [])
        env = clean_environment()
        started = time.time()
        with self.suspend():
            print("\nOpening this conversation in regular Claude Code. Exit it (/exit) to come back to Jarvis.\n",
                  flush=True)
            subprocess.run(argv, cwd=self.cwd, env=env)
        self.set_tab_title(self.tab_title)  # Claude Code titled the tab its own way
        if not session_id:
            recent = [s for s in sessions.list_sessions(self.cwd, limit=1) if s.modified >= started]
            session_id = recent[0].id if recent else None
        await self.clear_transcript()
        if session_id:
            path = sessions.find_session(self.cwd, session_id)
            if path:
                await self.show_history(path)
        await self.start_engine(resume=session_id)

    async def run_shell(self, command):
        if not command:
            return
        await self.add(UserEntry(command, "!", f"bold {theme.BASH_MODE}"))
        env = clean_environment()
        try:
            proc = await asyncio.create_subprocess_shell(command, cwd=self.cwd, env=env,
                                                         stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(), 600)
        except asyncio.TimeoutError:
            proc.kill()
            out, err = b"", b"Timed out after 10 minutes"
        out, err = out.decode(errors="replace"), err.decode(errors="replace")
        data = {"stdout": out, "stderr": err}
        await self.add(NoticeEntry(draw=lambda verbose: tools.result("Bash", {"command": command}, out, data, False,
                                                                     self.cwd, verbose), verbose=self.verbose))
        self.pending_context.append(f"<bash-input>{command}</bash-input>\n<bash-stdout>{out[-30000:]}</bash-stdout>"
                                    f"<bash-stderr>{err[-5000:]}</bash-stderr>")

    # Actions --------------------------------------------------------------------------------

    def action_ctrl_c(self):
        selected = self.screen.get_selected_text()
        if not selected and self.prompt.has_focus and self.prompt.selected_text:
            self.copy_text(self.prompt.selected_text)
            self.footer.flash("Copied")
            return
        if selected:
            self.copy_text(selected)
            self.screen.clear_selection()
            self.footer.flash("Copied")
            return
        if self.dialog is not None:
            self.dialog.finish(None)
            return
        if self.manager_pane.has_focus_within:
            self.manager_pane.ctrl_c()
            return
        if self.prompt.text:
            self.prompt.clear()
            return
        if self.busy:
            self.run_worker(self.interrupt(), group="interrupt")
            return
        self.press_to_exit("Ctrl-C")

    def action_ctrl_d(self):
        self.press_to_exit("Ctrl-D")

    def press_to_exit(self, key):
        now = time.monotonic()
        if now - self.last_exit_press < 1.5:
            self.run_worker(self.quit(), group="quit")
        else:
            self.last_exit_press = now
            self.footer.flash(f"Press {key} again to exit")

    def action_escape(self):
        if self.dialog is not None:
            self.dialog.finish(None)
            return
        if self.menu.display:
            self.menu.hide()
            return
        if self.side_answer.display:
            self.dismiss_side_answer()
            return
        if self.busy:
            self.run_worker(self.interrupt(), group="interrupt")
            return
        if self.reply and not self.prompt.text:
            self.set_reply(None)
            return
        if self.prompt.text:
            now = time.monotonic()
            if now - self.last_escape < 0.8:
                self.prompt.clear()
            else:
                self.footer.flash("Esc again to clear", 0.8)
            self.last_escape = now

    def action_cycle_mode(self):
        if isinstance(self.dialog, (SettingsPanel, JarvisSettingsPanel)):
            self.dialog.press("shift+tab", None)
            self.dialog.redraw()
            return
        if self.dialog is not None or self.manager_pane.has_focus_within:
            return
        modes = theme.MODES
        mode = modes[(modes.index(self.mode) + 1) % len(modes)] if self.mode in modes else "default"
        self.set_mode(mode)
        if self.engine and self.engine.running:
            self.run_worker(self.send_mode(mode), group="mode")

    async def send_mode(self, mode):
        try:
            await self.engine.request("set_permission_mode", mode=mode)
        except EngineError as e:
            await self.note(f"Couldn't switch mode: {e}", theme.ERROR)

    def action_toggle_verbose(self):
        self.verbose = not self.verbose
        transcripts = [self.transcript] + [t.view.transcript for t in (self.manager.threads if self.manager else [])]
        with self.batch_update():
            for entry in (e for transcript in transcripts for e in transcript.children):
                if isinstance(entry, (ToolEntry, ThinkingEntry, NoticeEntry)):
                    entry.set_verbose(self.verbose)
                if entry.has_class("quiet"):
                    entry.display = self.verbose
        self.footer.flash("Showing details (ctrl+o to hide)" if self.verbose else "Details hidden")

    def action_toggle_tasks(self):
        if self.busy:
            self.show_tasks = not self.show_tasks
        else:
            self.show_tasks_idle = not self.show_tasks_idle
            self.show_tasks = True
            if not self.task_list():
                self.footer.flash("No tasks")
        self.refresh_tasks()

    def action_scroll_bottom(self):
        self.unjump()
        self.transcript.scroll_end(animate=False)

    def jump_to_prompt(self, step):
        """alt+up (step -1) and alt+down (step 1): scroll to the previous or next of your messages.
        alt+up starts from the last one and alt+down from the first; going past the last is back
        to the end of the conversation."""
        prompts = [e for e in self.transcript.children if isinstance(e, UserEntry) and e.display]
        if not prompts:
            self.footer.flash("No messages of yours yet")
            return
        if self.jumped in prompts:
            index = prompts.index(self.jumped) + step
        else:
            index = len(prompts) - 1 if step < 0 else 0
        self.unjump()
        if index >= len(prompts):
            self.transcript.scroll_end(animate=False)
            return
        if index < 0:
            index = 0
            self.footer.flash("That's your first message")
        self.jumped = prompts[index]
        self.jumped.add_class("picked")
        self.transcript.scroll_to_widget(self.jumped, top=True, animate=False)

    def reply_to_selection(self):
        """ctrl+r: the next prompt replies to the text highlighted in the conversation."""
        selected = (self.screen.get_selected_text() or "").strip()
        if not selected:
            self.footer.flash("Highlight some text to reply to it")
            return
        self.screen.clear_selection()
        self.set_reply(selected)
        self.prompt.focus()

    def search(self, text):
        """The entries with text in them (any case), newest on show."""
        query = text.strip().lower()
        self.show_found(None)
        self.found = [e for e in self.transcript.children
                      if query and isinstance(e, Entry) and e.display and query in e.plain().lower()]
        if self.found:
            self.show_found(len(self.found) - 1)
        self.search_bar.set_count("" if not query else "No matches" if not self.found else self.found_count())

    def found_count(self):
        return f"{self.found_index + 1} of {len(self.found)} · ↑↓ · esc"

    def show_found(self, index):
        if 0 <= self.found_index < len(self.found):
            self.found[self.found_index].remove_class("found")
        self.found_index = -1 if index is None else index
        if index is not None:
            entry = self.found[index]
            entry.add_class("found")
            self.transcript.scroll_to_widget(entry, top=True, animate=False)

    def on_search_bar_changed(self, event):
        self.search(event.text)

    def on_search_bar_step(self, event):
        if self.found:  # around from the oldest to the newest, and back
            self.show_found((self.found_index + event.step) % len(self.found))
            self.search_bar.set_count(self.found_count())

    def on_search_bar_closed(self, event):
        self.show_found(None)
        self.found = []
        self.search_bar.close()
        self.prompt.focus()

    def set_reply(self, quoted):
        self.reply = quoted
        self.prompt_bar.set_reply(quoted)

    def unjump(self):
        if self.jumped is not None:
            self.jumped.remove_class("picked")
            self.jumped = None

    def action_page(self, direction):
        if isinstance(self.dialog, Picker):
            self.dialog.dialog_key("pageup" if direction < 0 else "pagedown")
            self.dialog.redraw()
            return
        if isinstance(self.dialog, JarvisSettingsPanel):
            self.dialog.press("pageup" if direction < 0 else "pagedown", None)
            self.dialog.redraw()
            return
        opened = self.manager_pane.opened
        transcript = opened.view.transcript if opened and self.manager_pane.has_focus_within else self.transcript
        if direction < 0:
            transcript.scroll_page_up()
        else:
            transcript.scroll_page_down()

    def action_external_editor(self):
        editor = (os.environ.get("VISUAL") or os.environ.get("EDITOR") or shutil.which("nano")
                  or ("notepad" if os.name == "nt" else "vi"))
        with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as f:
            f.write(self.prompt.text)
            path = f.name
        try:
            with self.suspend():
                subprocess.run([*editor.split(), path])
            with open(path) as f:
                self.prompt.set_text(f.read().rstrip("\n"))
        finally:
            os.unlink(path)
        self.prompt.focus()

    async def quit(self):
        if self.manager is not None:
            await self.manager.close()
        await self.stop_engine()
        self.exit(self.session_id)

    async def on_unmount(self):
        if self.manager is not None:  # the window closed some other way
            await asyncio.gather(*(t.close() for t in self.manager.threads), return_exceptions=True)
        if self.engine is not None:
            engine, self.engine = self.engine, None
            await engine.stop()

    # Transcript -----------------------------------------------------------------------------

    async def add(self, widget):
        # A reply's Markdown ends in a blank line of its own, so what follows it needs no gap.
        # (Trimming that line with :last-child instead makes Textual restyle every block of
        # every reply on each mount.)
        shown = [w for w in (self.batch if self.batch else self.transcript.children) if not w.has_class("quiet")]
        previous = shown[-1] if shown else None
        if isinstance(previous, AssistantEntry):
            widget.add_class("after-reply")
        if self.batch is not None:
            self.batch.append(widget)
        else:
            await self.transcript.mount(widget)
        return widget

    async def note(self, text, style=theme.DIM):
        await self.add(NoticeEntry(notice(text, style), attached=False))

    async def clear_transcript(self):
        for child in list(self.transcript.children):
            if not isinstance(child, Banner):
                await child.remove()
        self.tools.clear()
        self.agent_of.clear()
        self.blocks.clear()
        self.waiting.clear()
        self.todos, self.tasks = [], {}
        self.sent.clear()
        self.updates.clear()
        self.show_queued()
        self.jumped = None
        self.context_pct = None
        self.transcript.anchor(False)
        self.transcript.scroll_home(animate=False)
        self.refresh_tasks()
        self.update_footer()
