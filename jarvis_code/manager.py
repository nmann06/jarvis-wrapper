"""Project manager mode (/manager): Jarvis as a coordinator that hands tasks to other Jarvises.

The coordinator is this window's own conversation, on the left. It gets tools to start and
steer worker Jarvises ("threads") from an MCP server the window hosts itself (Engine's
sdk_mcp_servers): each thread is a `claude -p` of its own in the same folder, drawn in the pane
on the right. The pane lists the threads by title, coloured by what each is doing (working,
finished, waiting for you); Enter or → opens one in full, with its permission prompts and
questions, and a line to message it. When a thread the coordinator set going finishes its
turn, the window tells the coordinator in a <thread-update> message.

The threads are saved with the coordinator's conversation (sessions.save_manager): reopening
it brings them back in the pane, each with what it did, and a thread's own conversation is
resumed when it's next messaged.
"""

import asyncio
import os
import re
import time
import uuid

from rich.console import Group
from rich.text import Text
from textual import events
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Input, Static

from . import persona, sessions, theme, tools
from .engine import Engine, EngineError
from .widgets import (AssistantEntry, DoneEntry, NoticeEntry, Spinner, ThinkingEntry, ToolEntry,
                      Transcript, UserEntry, elapsed, notice)

SERVER = "manager"   # the MCP server's name: its tools are mcp__manager__*
MAX_WORKING = 8      # threads working at once
REPORT_LIMIT = 6000  # characters of a thread's reply put in its <thread-update>
HISTORY_LIMIT = 300  # saved records redrawn in a reopened thread, as in the window

MANAGER_PROMPT = """# Project manager mode
This window is in project manager mode (/manager). You are the coordinator: you plan the \
work and hand it to worker Jarvises, each a full Claude Code session of its own in this \
folder with the same tools and permission mode as you. The owner watches the workers \
("threads") in a pane beside this conversation and steps in when one needs them.

Your tools for it, from the window's "manager" MCP server:
- mcp__manager__start_thread(title, prompt, model?): start a worker on a task. The title is 2 \
to 6 words for the owner's list. The worker sees nothing of this conversation, so the prompt \
must carry everything it needs: the goal, the files and facts you know of, constraints, and \
what to report back. It returns at once and the worker carries on by itself.
- mcp__manager__message_thread(thread, message): a follow-up to a worker; it waits behind the \
work in progress.
- mcp__manager__list_threads(): every thread and what it's doing.
- mcp__manager__read_thread(thread): a thread's latest reply in full.
- mcp__manager__stop_thread(thread): interrupt a worker.

How to work:
- Split the request into tasks that can run at the same time without getting in each \
other's way: give each worker its own files or area, and start dependent steps only once \
what they depend on is done. Something that takes a few tool calls you can do yourself.
- After starting threads, end your turn with a short note of what's running. Don't poll or \
wait: when a worker finishes its turn, the window sends you a <thread-update> message with \
its final reply. Those messages come from the window, not the owner.
- As updates come in, check the work where it matters (read the changes, run the tests), \
send follow-ups or start new threads as needed, and when it's all done give the owner a \
concise summary of what was done.
- A worker that needs permission or has a question waits for the owner in its own thread; \
you don't need to pass it on."""

WORKER_PROMPT = """# Worker thread
You are a worker in project manager mode: the coordinating Jarvis started this thread \
("{title}") for one task, and the owner can watch it and step in. The first message is the \
task, from the coordinator; later ones come from the coordinator, or from the owner typing \
in this thread. Do the task fully, then end with a short report for the coordinator: what \
you did, the files you changed, how you checked it, and anything left unresolved. Other \
workers may be changing other parts of this folder at the same time, so keep to your task \
and leave changes you didn't make alone."""

LOADED = {"anthropic/alwaysLoad": True}  # not deferred behind ToolSearch
THREAD = {"type": "integer", "description": "The thread's number"}
TOOLS = [
    {"name": "start_thread", "_meta": LOADED,
     "description": "Start a worker Jarvis (a Claude Code session of its own, in this folder) on a "
                    "task. Returns at once; a <thread-update> message comes when it finishes its turn.",
     "inputSchema": {"type": "object", "required": ["title", "prompt"], "properties": {
         "title": {"type": "string", "description": "2 to 6 words, for the owner's list of threads"},
         "prompt": {"type": "string", "description": "The whole task: the worker sees nothing else"},
         "model": {"type": "string", "description": "Optional model for it (opus, sonnet, haiku); "
                                                    "the coordinator's by default"}}}},
    {"name": "message_thread", "_meta": LOADED,
     "description": "Send a worker a follow-up message (it waits behind the work in progress).",
     "inputSchema": {"type": "object", "required": ["thread", "message"], "properties": {
         "thread": THREAD, "message": {"type": "string"}}}},
    {"name": "list_threads", "_meta": LOADED, "description": "Every thread: its title and what it's doing.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "read_thread", "_meta": LOADED, "description": "A thread's status and its latest reply, in full.",
     "inputSchema": {"type": "object", "required": ["thread"], "properties": {"thread": THREAD}}},
    {"name": "stop_thread", "_meta": LOADED, "description": "Interrupt a worker's turn.",
     "inputSchema": {"type": "object", "required": ["thread"], "properties": {"thread": THREAD}}},
]


def update_notice(text):
    """The line a <thread-update> message shows as in the coordinator's transcript (None for
    any other message): ⎿ Thread 2 · Fix the tests · finished."""
    match = re.match(r'\s*<thread-update thread="(\d+)" title="([^"]*)" status="(\w+)"', text)
    if not match:
        return None
    number, title, status = match.groups()
    glyph, colour, word = theme.THREAD_LOOK.get(status, theme.THREAD_LOOK["done"])
    return tools.block([Text.assemble((f"{glyph} ", colour), (f"Thread {number}", f"bold {colour}"),
                                      (f" · {title} · {word}", theme.DIM))])


def attribute(value):
    return " ".join(str(value).split()).replace('"', "'")


class Thread:
    """One worker Jarvis: its Claude Code, what it has done, and its view in the pane."""

    def __init__(self, manager, number, title, model=None):
        self.manager, self.app = manager, manager.app
        self.number, self.title, self.model = number, title, model
        self.status = "starting"  # starting, working, done, error, stopped (theme.THREAD_LOOK)
        self.activity = ""        # what it's doing now: "Thinking", "Bash(npm test)"
        self.turn_started = time.monotonic()
        self.ended = None
        self.engine = None
        self.closing = False
        self.reply = ""           # the final reply of its last turn
        self.waiting = {}         # uuid -> prompt sent but not yet taken in
        self.for_coordinator = False  # the coordinator asked for the turn in progress
        self.interrupting = False
        self.tools = {}           # tool_use_id -> ToolEntry, as in the window (app.py)
        self.agent_of = {}
        self.permissions = asyncio.Queue()
        self.cancelled = set()
        self.dialog = self.dialog_request = None
        self.mode = None
        self.session = None       # its Claude Code conversation, once it has one
        self.saved = None         # reopened: its saved conversation, not yet drawn
        self.batch = None         # entries being drawn from history, mounted together
        self.launching = asyncio.Lock()
        self.drawing = asyncio.Lock()
        self.view = ThreadView(self)

    @property
    def look(self):
        """Its status as the list shows it: waiting on a dialog is "attention"."""
        return "attention" if self.dialog is not None else self.status

    @property
    def busy(self):
        return self.status in ("starting", "working")

    @property
    def cwd(self):
        return self.app.cwd

    @property
    def verbose(self):
        return self.app.verbose

    def changed(self):
        self.manager.pane.redraw()
        self.view.redraw()

    async def start(self, prompt):
        if await self.launch():
            await self.send(prompt, from_coordinator=True)

    async def launch(self):
        """Start its Claude Code: a new conversation, or (reopened) its own one again."""
        app = self.app
        mode = app.mode if app.mode != "plan" else "default"  # a worker in plan mode can't do its task
        log = f"{app.options.log_path}.thread-{self.number}" if app.options.log_path else None
        prompt_text = (persona.PERSONA + "\n\n" if app.options.persona else "") + WORKER_PROMPT.format(title=self.title)
        self.engine = Engine(self.cwd, self.on_message, lambda rid, req: self.permissions.put_nowait((rid, req)),
                             model=self.model or app.model_override, effort=app.effort_override,
                             permission_mode=mode if mode != "default" else None, resume=self.session,
                             append_system_prompt=prompt_text, thinking_display=app.options.thinking_display,
                             command=app.options.engine_command, log_path=log)
        self.mode = mode
        try:
            await self.engine.start()
        except (EngineError, OSError) as e:
            await self.fail(f"Couldn't start Claude Code: {e}")
            return False
        return True

    def restore(self, saved):
        """Back as it was saved: finished, or stopped if the window closed on its turn."""
        self.session = saved.get("session") or None
        self.reply = str(saved.get("reply") or "")
        status = saved.get("status")
        self.status = status if status in ("done", "error", "stopped") else "stopped"
        took = saved.get("took") if isinstance(saved.get("took"), (int, float)) else 0
        self.ended = time.monotonic()
        self.turn_started = self.ended - took
        self.saved = sessions.find_session(self.cwd, self.session) if self.session else None

    async def show_history(self):
        """Draw what it did before the window closed, once: when it's opened or messaged."""
        async with self.drawing:
            path, self.saved = self.saved, None
            if path:
                await self.draw_history(path)

    async def draw_history(self, path):
        messages = await asyncio.to_thread(sessions.load_conversation, path)
        self.batch = []
        try:
            if len(messages) > HISTORY_LIMIT:
                skipped = len(messages) - HISTORY_LIMIT
                messages = messages[-HISTORY_LIMIT:]
                await self.add(NoticeEntry(notice(f"… {skipped} earlier records not shown"), attached=False))
            for msg in messages:
                text = sessions.prompt_text(msg.get("message")) if msg["type"] == "user" else None
                if text:
                    await self.add(UserEntry(text))
                elif msg["type"] == "user":
                    await self.on_user(msg)
                elif msg["type"] == "assistant":
                    await self.on_assistant(msg)
            for entry in self.tools.values():
                if entry.state == "running":
                    entry.mark("done")
        finally:
            batch, self.batch = self.batch, None
        await self.view.transcript.mount_all(batch)
        self.view.transcript.scroll_end(animate=False)

    async def send(self, text, from_coordinator=False):
        await self.show_history()
        async with self.launching:  # a reopened thread: its Claude Code starts on the first message
            if (self.engine is None or not self.engine.running) and self.session and not self.closing:
                if not await self.launch():
                    raise EngineError(f"thread {self.number} couldn't start again")
        if self.engine is None or not self.engine.running:
            raise EngineError(f"thread {self.number} isn't running")
        if not self.busy or self.status == "starting":
            self.status, self.turn_started, self.interrupting = "working", time.monotonic(), False
            self.for_coordinator = from_coordinator
            self.view.spinner.start()
        else:
            self.for_coordinator = self.for_coordinator or from_coordinator
        message_uuid = str(uuid.uuid4())
        self.waiting[message_uuid] = text
        self.changed()
        await self.engine.send(text, message_uuid)

    async def stop(self):
        """Interrupt the turn in progress."""
        if self.status != "working" or self.engine is None:
            return False
        self.interrupting = True
        try:
            await self.engine.interrupt()
        except EngineError:
            pass
        return True

    async def close(self):
        self.closing = True
        if self.dialog is not None:
            self.dialog.finish(None)
        if self.engine is not None:
            await self.engine.stop()

    async def fail(self, text):
        self.status, self.ended = "error", time.monotonic()
        self.view.spinner.stop()
        await self.add(NoticeEntry(notice(text, theme.ERROR), attached=False))
        self.changed()
        self.manager.save()
        if self.for_coordinator or not self.reply:
            self.for_coordinator = False
            self.manager.report(self, text)

    async def add(self, widget):
        if self.batch is not None:
            self.batch.append(widget)
        else:
            await self.view.transcript.mount(widget)
        return widget

    # Messages from its Claude Code --------------------------------------------------------------

    async def on_message(self, msg):
        kind = msg.get("type")
        try:
            if kind == "stream_event":
                self.on_stream(msg)
            elif kind == "assistant":
                await self.on_assistant(msg)
            elif kind == "user":
                await self.on_user(msg)
            elif kind == "system":
                self.on_system(msg)
            elif kind == "result":
                await self.on_result(msg)
            elif kind == "control_cancel_request":
                self.cancelled.add(msg.get("request_id"))
                if self.dialog is not None and self.dialog_request == msg.get("request_id"):
                    self.dialog.finish(None)
            elif kind == "_exit" and not self.closing:
                tail = "\n".join((msg.get("stderr") or "").splitlines()[-4:])
                await self.fail(f"Claude Code stopped (exit code {msg.get('code')})." + (f"\n{tail}" if tail else ""))
        except Exception as e:  # a drawing problem must never stop the thread
            self.app.log.error(f"thread {self.number}, handling {kind}: {e!r}")
            if os.environ.get("JARVIS_DEBUG"):
                raise

    def on_stream(self, msg):
        """Only what it's doing now, for the list: replies are drawn whole, block by block."""
        event = msg.get("event") or {}
        if msg.get("parent_tool_use_id") or event.get("type") != "content_block_start":
            return
        kind = (event.get("content_block") or {}).get("type")
        activity = {"thinking": "Thinking", "redacted_thinking": "Thinking", "text": "Responding"}.get(kind)
        if activity:
            self.activity = activity
            self.view.spinner.mode = "thinking" if activity == "Thinking" else "responding"
            self.manager.pane.redraw()

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
        for block in content:
            kind = block.get("type")
            if kind == "text" and block.get("text", "").strip():
                await self.add(AssistantEntry(block["text"]))
            elif kind in ("thinking", "redacted_thinking") and (block.get("thinking") or "").strip():
                await self.add(ThinkingEntry(block["thinking"], self.verbose, done=True))
            elif kind in ("tool_use", "server_tool_use", "mcp_tool_use") and block.get("id") not in self.tools:
                entry = ToolEntry(block.get("id"), block.get("name", "?"), block.get("input"), self.cwd, self.verbose)
                if entry.tool_name in tools.QUIET_TOOLS:
                    entry.add_class("quiet")
                    entry.display = self.verbose
                else:
                    label, arg = tools.title(entry.tool_name, entry.input, self.cwd)
                    self.activity = f"{label}({tools.one_line(arg, 60)})" if arg else label
                self.tools[entry.tool_id] = entry
                await self.add(entry)
        self.changed()

    async def on_user(self, msg):
        message = msg.get("message") or {}
        content = message.get("content")
        parent = msg.get("parent_tool_use_id")
        results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"] \
            if isinstance(content, list) else []
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
        if not results and not parent and msg.get("isReplay") and self.waiting:
            # Taken in: the task, or a follow-up that waited behind the work in progress.
            text = self.waiting.pop(msg.get("uuid"), None)
            if text is None:
                text = self.waiting.pop(next(iter(self.waiting)))
            await self.add(UserEntry(text))

    def on_system(self, msg):
        sub = msg.get("subtype")
        if sub == "init" and msg.get("session_id") and msg["session_id"] != self.session:
            self.session = msg["session_id"]
            self.manager.save()
        elif sub in ("task_started", "task_progress"):
            entry = self.tools.get(msg.get("tool_use_id"))
            if entry is not None and entry.tool_name in ("Agent", "Task"):
                entry.set_activity(msg.get("description") or "")
        elif sub == "status" and msg.get("permissionMode"):
            self.mode = msg["permissionMode"]

    async def on_result(self, msg):
        took = time.monotonic() - self.turn_started
        for entry in self.tools.values():
            if entry.state == "running":
                entry.mark("interrupted" if self.interrupting else "done")
        if self.waiting:  # a follow-up comes next, as a turn of its own
            self.turn_started = time.monotonic()
            return
        self.view.spinner.stop()
        self.ended, self.activity = time.monotonic(), ""
        self.reply = msg.get("result") if isinstance(msg.get("result"), str) else ""
        if self.interrupting:
            self.status = "stopped"
            await self.add(NoticeEntry(tools.interrupted()))
        elif msg.get("is_error"):
            self.status = "error"
            errors = [e for e in (msg.get("errors") or []) if isinstance(e, str)]
            text = "; ".join(errors) or self.reply or msg.get("subtype", "error").replace("_", " ")
            self.reply = self.reply or text
            await self.add(NoticeEntry(notice(str(text), theme.ERROR)))
        else:
            self.status = "done"
            if took >= 8 and msg.get("num_turns"):
                await self.add(DoneEntry(took))
        self.interrupting = False
        self.session = msg.get("session_id") or self.session
        self.changed()
        self.manager.save()
        if self.for_coordinator:
            self.for_coordinator = False
            self.manager.report(self, self.reply or "(no reply)")

    # Permission, questions and plans: the window's dialogs (app.decide), drawn in this thread ---

    async def permission_loop(self):
        while True:
            request_id, request = await self.permissions.get()
            engine = self.engine
            try:
                response = await self.app.decide(request_id, request, view=self)
            except Exception as e:
                self.app.log.error(f"thread {self.number}, permission dialog: {e!r}")
                response = {"behavior": "deny", "message": f"Jarvis couldn't show the prompt: {e}"}
            if request_id in self.cancelled or engine is None or not engine.running:
                continue
            try:
                await engine.respond(request_id, response)
            except EngineError:
                pass

    async def show_dialog(self, dialog, request_id=None):
        """Like the window's show_dialog, in this thread's view. It takes the keys only if you're
        in the thread; until you open it, the list shows the thread as waiting for you."""
        self.dialog, self.dialog_request = dialog, request_id
        dialog.takes_focus = self.view.has_focus_within
        self.changed()
        await self.view.dock.mount(dialog)
        self.view.transcript.scroll_end(animate=False)
        try:
            return await dialog.result
        finally:
            answered_here = dialog.has_focus_within
            self.dialog = self.dialog_request = None
            await dialog.remove()
            self.changed()
            if answered_here and self.manager.pane.opened is self:
                self.view.entry.focus()

    def set_mode(self, mode):
        """A dialog's choice switched modes (accept edits from now on): this thread's alone."""
        if mode:
            self.mode = mode

    # For the coordinator ------------------------------------------------------------------------

    def describe(self):
        word = theme.THREAD_LOOK[self.look][2]
        since = elapsed((self.ended or time.monotonic()) - self.turn_started)
        about = self.activity if self.status == "working" else ""
        return f"{self.number}. {self.title}: {word} ({since})" + (f", {about}" if about else "")

    def saved_state(self):
        took = (self.ended or time.monotonic()) - self.turn_started
        return {"number": self.number, "title": self.title, "model": self.model, "session": self.session,
                "status": self.status, "reply": self.reply, "took": round(took, 1)}


class Manager:
    """The threads of /manager, and the tools the coordinator runs them with."""

    def __init__(self, app, pane):
        self.app = app
        self.pane = pane
        self.threads = []
        self.closed = False

    def save(self):
        """Keep the threads with the coordinator's conversation, for when it's reopened."""
        if not self.closed and self.app.session_id and self.threads:
            sessions.save_manager(self.app.session_id, [t.saved_state() for t in self.threads])

    async def restore(self, saved):
        """The threads of a reopened manager conversation, as they were saved."""
        for state in sorted(saved, key=lambda t: t.get("number") or 0):
            thread = Thread(self, len(self.threads) + 1, tools.one_line(str(state.get("title") or "Thread"), 60),
                            state.get("model") or None)
            thread.restore(state)
            self.threads.append(thread)
            await self.pane.add_thread(thread)
            self.app.run_worker(thread.permission_loop(), group=f"thread-{thread.number}")

    def thread(self, number):
        try:
            number = int(number)
        except (TypeError, ValueError):
            return None
        return self.threads[number - 1] if 1 <= number <= len(self.threads) else None

    async def handle_mcp(self, server_name, message):
        """A JSON-RPC message from the coordinator's Claude Code to the manager server."""
        method, message_id = message.get("method"), message.get("id")
        if message_id is None:  # a notification: nothing to answer
            return {"jsonrpc": "2.0", "id": None, "result": {}}
        if server_name != SERVER:
            return {"jsonrpc": "2.0", "id": message_id, "error": {"code": -32601, "message": f"No server {server_name}"}}
        if method == "initialize":
            version = (message.get("params") or {}).get("protocolVersion") or "2025-06-18"
            result = {"protocolVersion": version, "capabilities": {"tools": {}},
                      "serverInfo": {"name": SERVER, "version": "1.0"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params") or {}
            text, failed = await self.call(params.get("name"), params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": text}], "isError": failed}
        elif method == "ping":
            result = {}
        else:
            return {"jsonrpc": "2.0", "id": message_id, "error": {"code": -32601, "message": f"No method {method}"}}
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    async def call(self, name, args):
        """Run one of the tools: (what it says back, whether it failed)."""
        if name == "start_thread":
            return await self.start_thread(str(args.get("title") or "").strip(), str(args.get("prompt") or ""),
                                           args.get("model") or None)
        if name == "list_threads":
            return "\n".join(t.describe() for t in self.threads) or "No threads yet.", False
        thread = self.thread(args.get("thread"))
        if thread is None:
            return f"There's no thread {args.get('thread')}; list_threads shows them.", True
        if name == "read_thread":
            reply = thread.reply or "(it hasn't replied yet)"
            return f"{thread.describe()}\n\nIts latest reply:\n{reply}", False
        if name == "message_thread":
            if not str(args.get("message") or "").strip():
                return "The message is empty.", True
            try:
                await thread.send(str(args["message"]), from_coordinator=True)
            except EngineError as e:
                return f"Couldn't message thread {thread.number}: {e}", True
            return f"Sent to thread {thread.number}. A <thread-update> comes when it finishes.", False
        if name == "stop_thread":
            if await thread.stop():
                return f"Stopping thread {thread.number}.", False
            return f"Thread {thread.number} isn't working on anything.", False
        return f"No tool {name}", True

    async def start_thread(self, title, prompt, model=None):
        if not title or not prompt.strip():
            return "A thread needs a title and a prompt.", True
        working = [t for t in self.threads if t.busy]
        if len(working) >= MAX_WORKING:
            return (f"{len(working)} threads are working already, the most at once; wait for a "
                    "<thread-update> before starting another."), True
        thread = Thread(self, len(self.threads) + 1, tools.one_line(title, 60), model)
        self.threads.append(thread)
        self.save()
        await self.pane.add_thread(thread)
        self.app.run_worker(thread.permission_loop(), group=f"thread-{thread.number}")
        self.app.run_worker(thread.start(prompt), group=f"thread-{thread.number}")
        return (f"Started thread {thread.number} ({thread.title}). A <thread-update> comes when it "
                "finishes its turn."), False

    def report(self, thread, body):
        """Tell the coordinator how a thread's turn ended."""
        if len(body) > REPORT_LIMIT:
            body = body[:REPORT_LIMIT] + f"\n… (cut short: read_thread({thread.number}) has all of it)"
        text = (f'<thread-update thread="{thread.number}" title="{attribute(thread.title)}" status="{thread.status}">\n'
                f"Thread {thread.number} ({thread.title}) {theme.THREAD_LOOK[thread.status][2]}. Its reply:\n\n"
                f"{body.strip()}\n</thread-update>")
        self.app.run_worker(self.app.send_update(text), group="thread-updates")

    async def close(self):
        """Stop every thread (leaving manager mode, another conversation, or the window
        closing). What was saved of them stays, for reopening this conversation."""
        self.save()
        self.closed = True
        threads, self.threads = self.threads, []
        for thread in threads:
            self.app.workers.cancel_group(self.app, f"thread-{thread.number}")
        await asyncio.gather(*(t.close() for t in threads), return_exceptions=True)
        await self.pane.clear()


# The pane -----------------------------------------------------------------------------------

class ThreadList(Widget):
    """The threads by title, two lines each, coloured by status. ↑↓ choose, Enter or → opens
    one, ← or Esc goes back to the coordinator's prompt, x stops the chosen thread."""

    can_focus = True
    DEFAULT_CSS = "ThreadList { height: auto; }"

    def __init__(self, pane, **kw):
        super().__init__(**kw)
        self.pane = pane
        self.index = 0

    def render(self):
        threads = self.pane.threads
        if not threads:
            return Text("No threads yet: the coordinator starts them as it hands out the work.",
                        style=f"italic {theme.DIM}")
        rows = []
        blink = int(time.monotonic() * 2.5) % 2 == 0
        width = max(10, self.size.width - 4)
        for n, thread in enumerate(threads):
            glyph, colour, word = theme.THREAD_LOOK[thread.look]
            chosen = n == self.index and self.has_focus
            since = elapsed((thread.ended or time.monotonic()) - thread.turn_started)
            right = f"  {word} · {since}"
            row = Text.assemble(("❯ " if chosen else "  ", theme.ACCENT),
                                (glyph + " ", colour if thread.look != "working" or blink else theme.DIM),
                                (tools.one_line(f"{thread.number}. {thread.title}", max(4, width - len(right))),
                                 f"bold {colour}" if chosen else colour),
                                (right, theme.DIM))
            row.no_wrap = True
            detail = thread.activity if thread.look == "working" else \
                "waiting for you: open it to answer" if thread.look == "attention" else \
                " ".join(thread.reply.split()) or "…"
            rows += [row, Text("    " + tools.one_line(detail, width), style=theme.DIM, no_wrap=True)]
        return Group(*rows)

    def move(self, step):
        if self.pane.threads:
            self.index = (self.index + step) % len(self.pane.threads)
            self.refresh()

    def chosen(self):
        threads = self.pane.threads
        return threads[min(self.index, len(threads) - 1)] if threads else None

    def on_key(self, event: events.Key):
        key = event.key
        if key in ("up", "k"):
            self.move(-1)
        elif key in ("down", "j"):
            self.move(1)
        elif key in ("enter", "right", "l") and self.chosen():
            self.pane.open(self.chosen())
        elif key in ("left", "escape", "h"):
            self.pane.back_to_prompt()
        elif key == "x" and self.chosen():
            self.app.run_worker(self.chosen().stop(), group="thread-stop")
        else:
            return
        event.stop()
        event.prevent_default()

    def on_click(self, event: events.Click):
        n = event.y // 2
        if 0 <= n < len(self.pane.threads):
            self.index = n
            self.pane.open(self.pane.threads[n])

    def on_focus(self):
        self.app.footer.hint = "↑↓ to choose · Enter or → to open · x to stop · ← or Esc for the coordinator"
        self.app.footer.redraw()
        self.refresh()

    def on_blur(self):
        if self.app.dialog is None:
            self.app.footer.hint = None
            self.app.footer.redraw()
        self.refresh()


class ThreadInput(Input):
    """The line to message an open thread; ← on it empty goes back to the list."""

    DEFAULT_CSS = f"""
    ThreadInput.-textual-compact {{ margin: 1 1 0 1; padding: 0 1; background: {theme.USER_BG}; }}
    ThreadInput.-textual-compact:focus {{ background-tint: transparent; }}
    """

    def action_cursor_left(self, select=False):
        if not select and not self.value:
            self.parent.thread.manager.pane.back_to_list()
            return
        super().action_cursor_left(select)


class ThreadView(Vertical):
    """One thread in full: what it did, its prompts for you, and a line to message it."""

    DEFAULT_CSS = """
    ThreadView { height: 1fr; display: none; }
    ThreadView > .thread-title { height: auto; padding: 0 1; }
    ThreadView > Transcript { height: 1fr; }
    ThreadView > .thread-dock { height: auto; padding: 0 1; }
    """

    def __init__(self, thread):
        super().__init__()
        self.thread = thread
        self.title_view = Static(classes="thread-title")
        self.transcript = Transcript()
        self.spinner = Spinner()
        self.dock = Vertical(classes="thread-dock")
        self.entry = ThreadInput(placeholder=f"Message thread {thread.number}… (← for the list, Esc to stop it)",
                                 compact=True)

    def compose(self):
        yield self.title_view
        yield self.transcript
        yield self.spinner
        yield self.dock
        yield self.entry

    def on_mount(self):
        self.redraw()

    def redraw(self):
        thread = self.thread
        glyph, colour, word = theme.THREAD_LOOK[thread.look]
        self.title_view.update(Text.assemble(("← ", theme.LABEL), (f"{glyph} ", colour),
                                             (f"Thread {thread.number} · {thread.title}", f"bold {colour}"),
                                             (f"  {word}", theme.DIM)))
        self.entry.display = thread.dialog is None

    def on_key(self, event: events.Key):
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            if self.thread.status == "working":
                self.app.run_worker(self.thread.stop(), group="thread-stop")
            else:
                self.thread.manager.pane.back_to_list()
        elif event.key == "left" and not self.entry.has_focus:  # in the dialog or the transcript
            event.stop()
            event.prevent_default()
            self.thread.manager.pane.back_to_list()

    def on_click(self, event: events.Click):
        if event.widget is self.title_view:
            self.thread.manager.pane.back_to_list()

    def on_input_submitted(self, event: Input.Submitted):
        event.stop()
        text = event.value.strip()
        if not text:
            return
        self.entry.value = ""
        self.app.run_worker(self.message(text), group="thread-message")

    async def message(self, text):
        try:
            await self.thread.send(text)
        except EngineError as e:
            self.app.footer.flash(str(e), 3)


class ManagerPane(Vertical):
    """The right side of the window in manager mode: the list of threads, or one opened."""

    DEFAULT_CSS = f"""
    ManagerPane {{ width: 50%; height: 1fr; display: none; border-left: solid {theme.LABEL};
                   padding: 1 0 0 1; }}
    ManagerPane > .pane-title {{ height: auto; padding: 0 1; margin-bottom: 1; }}
    ManagerPane > ThreadList {{ padding: 0 1; }}
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.threads = []
        self.opened = None
        self.title_view = Static(classes="pane-title")
        self.list = ThreadList(self)

    def compose(self):
        yield self.title_view
        yield self.list

    def on_mount(self):
        self.redraw()

    def redraw(self):
        counts = {}
        for thread in self.threads:
            counts[thread.look] = counts.get(thread.look, 0) + 1
        title = Text("Threads", style=f"bold {theme.ACCENT}")
        for status in ("attention", "working", "done", "error", "stopped"):
            if counts.get(status):
                glyph, colour, word = theme.THREAD_LOOK[status]
                title.append(f"  {glyph} {counts[status]} {word}", style=colour)
        self.title_view.update(title)
        self.list.refresh(layout=True)  # its height follows the number of threads

    def tick(self):
        if not self.display:
            return
        self.list.refresh()  # the blink and the times
        if self.opened is not None:
            view = self.opened.view
            view.spinner.tick()
            if not view.transcript.is_anchored and view.transcript.max_scroll_y > 0:
                view.transcript.anchor()
            on = int(time.monotonic() * 2.5) % 2 == 0
            for entry in self.opened.tools.values():
                if entry.state == "running":
                    entry.blink(on)

    async def add_thread(self, thread):
        self.threads.append(thread)
        await self.mount(thread.view)
        self.redraw()

    async def clear(self):
        self.back_to_list(focus=False)
        for thread in self.threads:
            await thread.view.remove()
        self.threads = []
        self.list.index = 0
        self.redraw()

    def open(self, thread):
        """Show one thread in full in place of the list."""
        if self.opened is not None:
            self.opened.view.display = False
        self.opened = thread
        self.list.index = self.threads.index(thread)
        self.title_view.display = self.list.display = False
        thread.view.display = True
        if thread.saved:
            self.app.run_worker(thread.show_history(), group=f"thread-{thread.number}")
        thread.view.transcript.scroll_end(animate=False)
        thread.view.spinner.display = thread.status == "working"
        if thread.dialog is not None:
            thread.dialog.take_focus()
        else:
            thread.view.entry.focus()

    def back_to_list(self, focus=True):
        if self.opened is not None:
            self.opened.view.display = False
            self.opened = None
        self.title_view.display = self.list.display = True
        self.redraw()
        if focus and self.display:
            self.list.focus()

    def back_to_prompt(self):
        self.app.prompt.focus()

    def ctrl_c(self):
        """ctrl+c in the pane: no to an open thread's prompt, else stop it, else go back."""
        thread = self.opened
        if thread is not None and thread.dialog is not None:
            thread.dialog.finish(None)
        elif thread is not None and thread.status == "working":
            self.app.run_worker(thread.stop(), group="thread-stop")
        else:
            self.back_to_prompt()

    def focus_list(self):
        if self.opened is None:
            self.list.focus()
        else:
            self.open(self.opened)
