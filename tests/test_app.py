"""The window against a fake Claude Code (fake_claude.py): each feature, end to end."""

import asyncio
import io
import json
import os
import platform
import sys
import time

import pytest
from rich.console import Console

from textual.widget import Widget

from jarvis_code import app as app_module, sessions, theme
from jarvis_code.app import JarvisApp, Options
from jarvis_code.widgets import AssistantEntry, NoticeEntry, ThinkingEntry, ToolEntry, UnqueueButton, UserEntry

FAKE = [sys.executable, os.path.join(os.path.dirname(__file__), "fake_claude.py")]
SIZE = (110, 40)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "HISTORY", str(tmp_path / "history.jsonl"))
    monkeypatch.setattr(sessions, "PROJECTS", str(tmp_path / "projects"))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "sent.jsonl"))
    monkeypatch.setattr(app_module, "SETTINGS", str(tmp_path / "jarvis-window.json"))
    monkeypatch.setattr(sessions, "MANAGERS", str(tmp_path / "jarvis-manager"))
    monkeypatch.setattr(sessions, "FINISHED", str(tmp_path / "jarvis-finished.json"))
    (tmp_path / "notes.txt").write_text("alpha\n")
    return tmp_path


def make(home, **kw):
    return JarvisApp(Options(cwd=str(home), engine_command=FAKE, **kw))


async def until(pilot, condition, timeout=8.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting")
        await pilot.pause(0.05)


async def ready(pilot):
    await until(pilot, lambda: pilot.app.engine_state == "running" and pilot.app.model)


async def send(pilot, text):
    pilot.app.prompt.set_text(text)
    await pilot.press("enter")


def sent(home):
    path = home / "sent.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def entries(app, kind):
    return [w for w in app.transcript.children if isinstance(w, kind)]


def text_of(renderable, width=100):
    console = Console(width=width, file=io.StringIO(), color_system=None)
    console.print(renderable)
    return console.file.getvalue()


def replies(app):
    return [e.text.strip() for e in entries(app, AssistantEntry)]


async def test_banner_and_hello(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        banner = text_of(app.banner.render())
        assert "╚█████╔╝" in banner and "Haiku 5.5 · high effort · Claude Pro" in banner
        await send(pilot, "hello")
        await until(pilot, lambda: replies(app) == ["Hello, sir. How may I help?"] and not app.busy)
        assert [e.text for e in entries(app, UserEntry)] == ["hello"]
        await until(pilot, lambda: "3% context" in app.footer.right)
        assert not app.spinner.display
        assert sessions.read_history(str(home)) == ["hello"]


async def test_permission_yes_and_dont_ask_again(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "edit the notes")
        await until(pilot, lambda: app.dialog is not None)
        shown = text_of(app.dialog.choice_view.content)
        assert "Yes, allow all edits during this session" in shown
        await pilot.press("2")
        await until(pilot, lambda: not app.busy and replies(app))
        tool = entries(app, ToolEntry)[0]
        assert tool.state == "done" and app.mode == "acceptEdits"
        response = [m for m in sent(home) if m.get("type") == "control_response"][0]["response"]["response"]
        assert response["behavior"] == "allow" and response["updatedPermissions"][0]["mode"] == "acceptEdits"
        assert "Updated notes.txt with 1 addition and 1 removal" in text_of(tool.result_view.content)


async def test_permission_rejected_with_esc(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "edit the notes")
        await until(pilot, lambda: app.dialog is not None)
        await pilot.press("escape")
        await until(pilot, lambda: not app.busy)
        assert entries(app, ToolEntry)[0].state == "rejected"
        response = [m for m in sent(home) if m.get("type") == "control_response"][0]["response"]["response"]
        assert response["behavior"] == "deny" and response["interrupt"] is True
        assert app.prompt.has_focus


async def test_question_answered(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "ask me a question")
        await until(pilot, lambda: app.dialog is not None)
        await pilot.press("down", "enter")
        await until(pilot, lambda: "You chose Coffee." in replies(app))


async def test_question_typed_answer(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "ask me a question")
        await until(pilot, lambda: app.dialog is not None)
        await pilot.press("3")
        await until(pilot, lambda: app.dialog.entry.has_focus)
        await pilot.press(*"Water", "enter")
        await until(pilot, lambda: "You chose Water." in replies(app))


async def test_plan_approval_switches_mode(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "make a plan")
        await until(pilot, lambda: app.dialog is not None)
        await pilot.press("1")
        await until(pilot, lambda: "Starting on the plan." in replies(app))
        assert app.mode == "auto"


async def test_shift_tab_cycles_modes(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        seen = []
        for _ in range(4):
            await pilot.press("shift+tab")
            seen.append(app.mode)
        assert seen == ["acceptEdits", "plan", "auto", "default"]
        await until(pilot, lambda: [m["request"]["mode"] for m in sent(home)
                                    if m.get("request", {}).get("subtype") == "set_permission_mode"] == seen)


async def test_footer_shows_usage_effort_and_when_the_5_hour_session_ends(home):
    app = make(home)
    async with app.run_test(size=(160, 40)) as pilot:
        await ready(pilot)
        clock = lambda t: time.strftime("%-I:%M%p", time.localtime(t)).lower().replace(":00", "")
        await until(pilot, lambda: "weekly" in app.footer.right)  # from get_usage
        assert app.session_ends - time.time() == pytest.approx(7200, abs=60)
        right = app.footer.right
        assert f"5-hour 62% ends {clock(app.session_ends)}" in right and "weekly 23%" in right
        assert "Haiku 5.5 · high effort" in right
        later = int(time.time()) + 3 * 3600
        app.on_rate_limit({"rate_limit_info": {"status": "allowed", "resetsAt": later, "rateLimitType": "five_hour",
                                               "unifiedWindows": {"five_hour": {"utilization": 0.1, "resetsAt": later},
                                                                  "seven_day": {"utilization": 0.3, "resetsAt": later}}}})
        assert f"5-hour 10% ends {clock(later)} · weekly 30%" in app.footer.right
        app.session_ends = time.time() - 1  # past: just the usage until the next turn reports one
        app.update_footer()
        assert "5-hour 10% · weekly 30%" in app.footer.right and "ends" not in app.footer.right


async def test_slash_menu_completes_and_runs(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        app.prompt.set_text("/mo")
        await pilot.pause(0.1)
        assert app.menu.open and app.menu.current()[0] == "model"
        await pilot.press("tab")
        assert app.prompt.text == "/model "
        await pilot.press(*"sonnet", "enter")
        await until(pilot, lambda: any("Set model to" in text_of(n.renderable) for n in entries(app, NoticeEntry)))
        assert app.model_override == "sonnet"


async def test_queued_prompt_runs_after(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "slow")
        await until(pilot, lambda: app.busy and replies(app))
        await send(pilot, "hello")
        assert app.queued == ["hello"] and app.queued_panel.display
        await until(pilot, lambda: "Hello, sir. How may I help?" in replies(app) and not app.busy, timeout=15)
        assert [e.text for e in entries(app, UserEntry)] == ["slow", "hello"]
        assert not app.queued_panel.display


async def test_ctrl_x_s_sends_now(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "slow")
        await until(pilot, lambda: app.busy and replies(app))
        app.prompt.set_text("hello")
        await pilot.press("ctrl+x")
        assert app.prompt.text == "hello"  # not cut
        await pilot.press("s")
        await until(pilot, lambda: "Hello, sir. How may I help?" in replies(app) and not app.busy, timeout=15)
        assert any("Interrupted" in text_of(n.renderable) for n in entries(app, NoticeEntry))
        assert [e.text for e in entries(app, UserEntry)] == ["slow", "hello"]
        assert not app.queued and app.prompt.text == ""


async def test_ctrl_x_then_another_key_types_it(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        app.prompt.set_text("hi")
        app.prompt.move_cursor((0, 2))
        await pilot.press("ctrl+x", "a")
        assert app.prompt.text == "hia" and not entries(app, UserEntry)


def test_starts_in_auto_mode(monkeypatch):
    from jarvis_code.__main__ import parse
    monkeypatch.delenv("JARVIS_MODE", raising=False)
    assert parse([]).mode == "auto"
    assert parse(["--mode", "default"]).mode == "default"


async def test_escape_interrupts(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "slow")
        await until(pilot, lambda: replies(app))
        await pilot.press("escape")
        await until(pilot, lambda: not app.busy)
        assert any("Interrupted" in text_of(n.renderable) for n in entries(app, NoticeEntry))


async def test_fuck_stops_at_once(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "slow")
        await until(pilot, lambda: replies(app))
        await send(pilot, "/fuck")
        await until(pilot, lambda: not app.busy)
        assert any("Interrupted" in text_of(n.renderable) for n in entries(app, NoticeEntry))
        assert [e.text for e in entries(app, UserEntry)] == ["slow"]


async def test_up_arrow_walks_history(home):
    for text in ("first", "second"):
        sessions.add_history(text, str(home))
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        app.prompt.set_text("draft")
        await pilot.press("up")
        assert app.prompt.text == "second"
        await pilot.press("up")
        assert app.prompt.text == "first"
        await pilot.press("down", "down")
        assert app.prompt.text == "draft"


async def test_shell_output_goes_with_next_prompt(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "!echo from-the-shell")
        await until(pilot, lambda: any("from-the-shell" in text_of(n.renderable) for n in entries(app, NoticeEntry)))
        await send(pilot, "hello")
        await until(pilot, lambda: not app.busy and replies(app))
        prompt = [m for m in sent(home) if m.get("type") == "user"][0]["message"]["content"]
        assert prompt.startswith("<bash-input>echo from-the-shell</bash-input>") and prompt.endswith("hello")


async def test_shell_output_unfolds_with_ctrl_o(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "!seq 1 12")
        await until(pilot, lambda: any("+8 lines" in text_of(n.renderable) for n in entries(app, NoticeEntry)))
        await pilot.press("ctrl+o")
        out = text_of(entries(app, NoticeEntry)[-1].renderable)
        assert "12" in out and "ctrl+o to expand" not in out
        await pilot.press("ctrl+o")
        assert "+8 lines" in text_of(entries(app, NoticeEntry)[-1].renderable)


async def test_click_unfolds_shell_output_and_collapse_folds_it(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "!seq 1 12")
        await until(pilot, lambda: any("+8 lines" in text_of(n.renderable) for n in entries(app, NoticeEntry)))
        notice = entries(app, NoticeEntry)[-1]
        await pilot.click(notice.view)
        assert "12" in text_of(notice.renderable) and notice.collapse_button.display
        await pilot.click(notice.collapse_button)
        assert "+8 lines" in text_of(notice.renderable) and not notice.collapse_button.display


async def test_bash_command_folds_to_one_line_and_click_unfolds_it(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "run bash")
        await until(pilot, lambda: app.dialog is not None)
        tool = entries(app, ToolEntry)[0]
        assert not tool.header_view.has_class("oneline")  # the whole command while it runs
        await pilot.press("enter")
        await until(pilot, lambda: not app.busy and replies(app))
        assert tool.header_view.has_class("oneline")  # one line once it's done
        assert "+8 lines" in text_of(tool.result_view.content) and not tool.collapse_button.display
        await pilot.click(tool.header_view)
        assert "+8 lines" not in text_of(tool.result_view.content) and tool.collapse_button.display
        await pilot.click(tool.collapse_button)
        assert "+8 lines" in text_of(tool.result_view.content) and not tool.collapse_button.display
        await pilot.click(tool.header_view)
        await pilot.press("ctrl+o")  # ctrl+o takes over from the click
        assert not tool.collapse_button.display


async def test_btw_answers_aside(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "hello")
        await until(pilot, lambda: replies(app) and not app.busy)
        before = len(app.transcript.children)
        await send(pilot, "/btw what did I say?")
        await until(pilot, lambda: "ran what did I say?" in text_of(app.side_answer.drawn))
        assert len(app.transcript.children) == before  # not part of the conversation
        argv = [m["argv"] for m in sent(home) if "argv" in m][-1]
        assert argv[:2] == ["-p", "what did I say?"] and "--no-session-persistence" in argv
        assert argv[argv.index("--tools") + 1] == "" and "--fork-session" in argv
        assert argv[argv.index("--resume") + 1] == app.session_id
        await pilot.press("escape")
        assert not app.side_answer.display and not app.busy


async def test_btw_without_a_question(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "/btw")
        await pilot.pause(0.2)
        printed = [m["argv"] for m in sent(home) if "argv" in m and m["argv"][0] == "-p"
                   and not m["argv"][1].startswith("--")]
        assert not app.side_answer.display and not printed


async def test_agent_and_thinking(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "use an agent")
        await until(pilot, lambda: not app.busy and replies(app))
        agent = entries(app, ToolEntry)[0]
        assert agent.children_calls[0][1] == "Bash"
        assert "Done (1 tool use · 1.5k tokens · 2.1s)" in text_of(agent.result_view.content)
        await send(pilot, "think hard")
        await until(pilot, lambda: not app.busy and len(replies(app)) == 2)
        thought = entries(app, ThinkingEntry)[0]
        assert thought.text == "Seventeen times twenty-three is 391."
        await pilot.press("ctrl+o")
        assert thought.expanded and "391" in text_of(thought.view.content)


async def test_bash_permission_and_folded_output(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "run bash")
        await until(pilot, lambda: app.dialog is not None)
        assert "Yes, and don't ask again for ls:* commands in this project" in text_of(app.dialog.choice_view.content)
        await pilot.press("enter")
        await until(pilot, lambda: not app.busy and replies(app))
        out = text_of(entries(app, ToolEntry)[0].result_view.content)
        assert "file3.txt" in out and "… +8 lines (ctrl+o to expand)" in out


async def test_clear_starts_fresh(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "hello")
        await until(pilot, lambda: replies(app) and not app.busy)
        await send(pilot, "/clear")
        await until(pilot, lambda: not replies(app) and not app.busy)
        assert any("new conversation" in text_of(n.renderable) for n in entries(app, NoticeEntry))


async def test_computer_says_where_the_window_runs(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "/computer")
        await until(pilot, lambda: any("Running on" in text_of(n.renderable) for n in entries(app, NoticeEntry)))
        assert any(platform.node() in text_of(n.renderable) for n in entries(app, NoticeEntry))


async def test_resume_shows_saved_conversation(home):
    cwd = str(home)
    folder = home / "projects" / sessions.project_dir(cwd).split("/")[-1]
    folder.mkdir(parents=True)
    records = [
        {"type": "user", "message": {"role": "user", "content": "what's in notes?"}},
        {"type": "assistant", "message": {"id": "m1", "role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": f"{cwd}/notes.txt"}}]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "1\talpha"}]},
         "toolUseResult": {"type": "text", "file": {"numLines": 1}}},
        {"type": "assistant", "message": {"id": "m2", "role": "assistant", "content": [{"type": "text", "text": "It says alpha."}]}},
    ]
    (folder / "s1.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
    app = make(home, resume="s1")
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        assert [e.text for e in entries(app, UserEntry)] == ["what's in notes?"]
        assert replies(app) == ["It says alpha."]
        assert "Read 1 line" in text_of(entries(app, ToolEntry)[0].result_view.content)
        assert ["--resume", "s1"] == next(m["argv"] for m in sent(home) if "argv" in m)[-2:] or \
            "--resume" in next(m["argv"] for m in sent(home) if "argv" in m)


async def test_ctrl_c_twice_exits(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await pilot.press("ctrl+c")
        assert "again to exit" in (app.footer.flash_text or "")
        await pilot.press("ctrl+c")
        await until(pilot, lambda: app.engine is None)


async def test_engine_that_dies_is_reported(home):
    app = JarvisApp(Options(cwd=str(home), engine_command=[sys.executable, "-c", "import sys; sys.exit(3)"]))
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: app.engine_state == "stopped")
        assert any("Couldn't start Claude Code" in text_of(n.renderable) for n in entries(app, NoticeEntry))


async def test_model_picker_from_a_command(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "/model")
        await until(pilot, lambda: app.dialog is not None)
        await pilot.press(*"sonn", "enter")
        await until(pilot, lambda: any("Set model to" in text_of(n.renderable) for n in entries(app, NoticeEntry)))
        assert app.model_override == "sonnet"


async def test_resume_picker_switches_conversation(home):
    cwd = str(home)
    folder = home / "projects" / sessions.project_dir(cwd).split("/")[-1]
    folder.mkdir(parents=True)
    for sid, prompt in (("old1", "first chat"), ("old2", "second chat")):
        (folder / f"{sid}.jsonl").write_text(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n")
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "/resume")
        await until(pilot, lambda: app.dialog is not None)
        await pilot.press(*"first", "enter")
        await until(pilot, lambda: app.dialog is None and app.engine_state == "running"
                    and [e.text for e in entries(app, UserEntry)] == ["first chat"])
        assert app.session_id == "old1"


def save_session(home, folder, sid, prompt, extra=()):
    """A saved conversation started in folder, as Claude Code writes it."""
    directory = home / "projects" / sessions.project_dir(str(folder)).split("/")[-1]
    directory.mkdir(parents=True, exist_ok=True)
    records = [{"type": "user", "cwd": str(folder), "message": {"role": "user", "content": prompt}}, *extra]
    path = directory / f"{sid}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


async def test_left_arrow_lists_every_session_and_resumes_in_its_folder(home, monkeypatch):
    monkeypatch.chdir(home)  # resuming moves the window (and the process) to the other folder
    other = home / "other"
    other.mkdir()
    for n in range(11):
        save_session(home, home, f"here{n}", f"chat {n} here")
    away = save_session(home, other, "away1", "plan the buck converter")
    os.utime(away, (1, 1))  # the oldest: the list has to scroll to reach it
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await pilot.press("left")
        await until(pilot, lambda: app.dialog is not None)
        picker = app.dialog
        assert "12, every folder" in picker.title_text and len(picker.shown) == 9
        assert "↓ 3 more" in text_of(picker.choice_view.content)
        await pilot.press("pagedown", "pagedown")  # nine at a time, stopping at the end
        assert picker.shown[picker.index][0] == "away1" and "↑ 3 more" in text_of(picker.choice_view.content)
        assert "other" in text_of(picker.choice_view.content)
        await pilot.press("enter")
        await until(pilot, lambda: app.dialog is None and app.engine_state == "running"
                    and [e.text for e in entries(app, UserEntry)] == ["plan the buck converter"])
        assert app.session_id == "away1" and app.cwd == str(other)
        assert os.path.realpath(os.getcwd()) == os.path.realpath(other)


async def test_a_manager_conversation_reopens_with_its_threads(home):
    app = make(home)
    async with app.run_test(size=(140, 40)) as pilot:
        await ready(pilot)
        await send(pilot, "/manager delegate the work")
        await until(pilot, lambda: app.manager is not None and len(app.manager.threads) == 2
                    and app.manager.threads[0].look == "done" and app.manager.threads[1].look == "attention")
        coordinator = app.session_id
        workers = [t.session for t in app.manager.threads]
        reply = app.manager.threads[0].reply
        assert all(workers) and reply and coordinator not in workers
    # Claude Code keeps each conversation (the fake doesn't): the coordinator's and the threads'.
    save_session(home, home, coordinator, "delegate the work")
    save_session(home, home, workers[0], "hello", [{"type": "assistant", "message": {
        "id": "m1", "role": "assistant", "content": [{"type": "text", "text": "Hello, sir. How may I help?"}]}}])
    save_session(home, home, workers[1], "edit the notes")
    save_session(home, home, "plain1", "what's the weather")
    app = make(home)
    async with app.run_test(size=(140, 40)) as pilot:
        await ready(pilot)
        await pilot.press("left")
        await until(pilot, lambda: app.dialog is not None)
        shown = {item[0]: item[1] for item in app.dialog.shown}
        assert set(shown) == {coordinator, "plain1"}  # the threads come back with their coordinator
        assert shown[coordinator] == "Manager mode · delegate the work" and shown["plain1"] == "what's the weather"
        await pilot.press(*["down"] * [item[0] for item in app.dialog.shown].index(coordinator), "enter")
        await until(pilot, lambda: app.dialog is None and app.engine_state == "running"
                    and app.manager is not None and len(app.manager.threads) == 2)
        assert app.manager_pane.display and app.session_id == coordinator
        init = [m for m in sent(home) if (m.get("request") or {}).get("subtype") == "initialize"]
        assert init[-1]["request"].get("sdkMcpServers") == ["manager"]
        one, two = app.manager.threads
        assert (one.title, one.look, two.title, two.look) == ("Say hello", "done", "Edit the notes", "stopped")
        assert one.engine is None and two.engine is None  # nothing runs until a thread is messaged
        assert one.reply == reply and reply in app.manager_pane.list.render().renderables[1].plain
        # Opening one draws what it did; messaging it resumes its own conversation.
        await pilot.press("right", "enter")
        await until(pilot, lambda: thread_replies(one) == ["Hello, sir. How may I help?"])
        await pilot.press(*"think")
        await pilot.press("enter")
        await until(pilot, lambda: "It is **391**, sir." in thread_replies(one) and one.look == "done")
        argv = [m["argv"] for m in sent(home) if "argv" in m][-1]
        assert argv[argv.index("--resume") + 1] == workers[0]
        # /manager off: a plain conversation again, its threads listed on their own.
        await pilot.press("left", "left")
        await send(pilot, "/manager off")
        await until(pilot, lambda: app.manager is None and not app.busy)
        assert sessions.load_manager(coordinator) == []
        assert {s.id for s in sessions.list_sessions(str(home))} == {coordinator, "plain1", *workers}


async def test_finished_marks_the_conversation_in_the_list(home):
    save_session(home, home, "s1", "fix the buck converter")
    save_session(home, home, "s2", "plan the lab")
    app = make(home, resume="s1")
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "/finished")
        await until(pilot, lambda: sessions.finished_sessions() == {"s1"})
        await pilot.press("left")
        await until(pilot, lambda: app.dialog is not None)
        about = {item[0]: item[2] for item in app.dialog.shown}
        assert about["s1"].startswith("✓ finished · ") and "✓" not in about["s2"]
        await pilot.press("escape")
        await until(pilot, lambda: app.dialog is None)
        await send(pilot, "/finished off")
        await until(pilot, lambda: sessions.finished_sessions() == set())
        assert not any(s.finished for s in sessions.list_sessions(str(home)))


async def test_left_arrow_in_text_moves_the_cursor(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        app.prompt.set_text("ab")
        app.prompt.move_cursor((0, 2))
        await pilot.press("left", "x")
        assert app.prompt.text == "axb" and app.dialog is None


async def test_tab_title_is_what_the_conversation_is_about(home):
    path = save_session(home, home, "s1", "what's in notes?")
    app = make(home, resume="s1")
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await until(pilot, lambda: app.tab_title == "Notes About Alpha")
        assert {"type": "ai-title", "aiTitle": "Notes About Alpha", "sessionId": "s1"} in \
            [json.loads(line) for line in path.read_text().splitlines()]
        assert sessions.list_sessions(str(home))[0].title == "Notes About Alpha"


async def test_tab_title_uses_a_saved_title(home):
    save_session(home, home, "s2", "hello there", [{"type": "custom-title", "customTitle": "Greeting", "sessionId": "s2"}])
    app = make(home, resume="s2")
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await until(pilot, lambda: app.tab_title == "Greeting")
        assert not any("<message>" in " ".join(m.get("argv", [])) for m in sent(home))  # none asked for


async def test_stale_menu_does_not_run_a_command(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        app.prompt.set_text("/")
        await pilot.pause(0.1)
        app.prompt.set_text("hello")
        await pilot.press("enter")
        await until(pilot, lambda: replies(app) == ["Hello, sir. How may I help?"])


async def test_permission_prompt_runs_notification_hooks(home, monkeypatch):
    out = home / "hook.json"
    monkeypatch.setenv("FAKE_HOOK_OUT", str(out))
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "edit the notes")
        await until(pilot, lambda: app.dialog is not None)
        await until(pilot, lambda: out.exists() and out.read_text())
        event = json.loads(out.read_text())
        assert event["hook_event_name"] == "Notification" and event["notification_type"] == "permission_prompt"
        assert event["cwd"] == str(home) and "Edit" in event["message"]
        await pilot.press("1")


async def test_task_list_without_bookkeeping_entries(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "make tasks")
        await until(pilot, lambda: not app.busy and replies(app))
        assert app.task_list() == [{"content": "Write the tests", "status": "in_progress", "activeForm": "Writing the tests"}]
        quiet = [e for e in entries(app, ToolEntry) if e.tool_name in ("TaskCreate", "TaskUpdate")]
        assert len(quiet) == 2 and not any(e.display for e in quiet)
        await pilot.press("ctrl+o")
        assert all(e.display for e in quiet)


async def test_background_tasks_in_footer(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "start background server")
        await until(pilot, lambda: not app.busy and "1 background task" in app.footer.right)
        await send(pilot, "stop background server")
        await until(pilot, lambda: not app.busy and "background" not in app.footer.right)


async def test_left_arrow_typing_starts_a_new_conversation(home):
    save_session(home, home, "old1", "plan the buck converter")
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "first chat")
        await until(pilot, lambda: not app.busy and len(entries(app, UserEntry)) == 1)
        await pilot.press("left")
        await until(pilot, lambda: app.dialog is not None)
        await pilot.press(*"buck", "enter")  # typing isn't searching: it's a new conversation
        await until(pilot, lambda: app.dialog is None and app.engine_state == "running"
                    and [e.text for e in entries(app, UserEntry)] == ["buck"])
        assert app.history[0] == "buck"


async def test_left_arrow_ctrl_f_searches(home, monkeypatch):
    monkeypatch.chdir(home)
    save_session(home, home, "old1", "plan the buck converter")
    save_session(home, home, "old2", "notes on op amps")
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await pilot.press("left")
        await until(pilot, lambda: app.dialog is not None)
        picker = app.dialog
        await pilot.press("ctrl+f", *"op amp")
        await until(pilot, lambda: [v for v, _, _ in picker.matches] == ["old2"])
        await pilot.press("escape")  # stops searching, without closing
        await until(pilot, lambda: len(picker.matches) == 2 and picker.filter.value == "")
        assert app.dialog is picker
        await pilot.press("ctrl+f", *"buck", "enter")
        await until(pilot, lambda: app.dialog is None and app.engine_state == "running"
                    and [e.text for e in entries(app, UserEntry)] == ["plan the buck converter"])
        assert app.session_id == "old1"


async def test_ctrl_backspace_deletes_a_word(home, monkeypatch):
    from textual import events
    from jarvis_code import app as app_module
    monkeypatch.setattr(app_module, "ERASE_IS_DELETE", True)
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        app.prompt.set_text("charge the capacitor")
        app.post_message(events.Key("backspace", "\x08"))  # what most terminals send for ctrl+backspace
        await until(pilot, lambda: app.prompt.text == "charge the ")
        app.post_message(events.Key("backspace", "\x7f"))  # backspace itself: one letter
        await until(pilot, lambda: app.prompt.text == "charge the")
        app.post_message(events.Key("ctrl+backspace", None))  # a terminal that sends it as such
        await until(pilot, lambda: app.prompt.text == "charge ")
        app.prompt.clear()
        await pilot.press("left")
        await until(pilot, lambda: app.dialog is not None)
        await pilot.press(*"buck converter")
        app.post_message(events.Key("ctrl+backspace", None))  # Input's own would delete to the right
        await until(pilot, lambda: app.dialog.filter.value == "buck ")


async def test_usage_keeps_up_with_other_windows_and_the_5_hour_reset(home, monkeypatch):
    from jarvis_code import app as app_module
    monkeypatch.setattr(app_module, "USAGE_REFRESH", 0.3)
    asked = lambda: sum(1 for m in sent(home) if m.get("request", {}).get("subtype") == "get_usage")
    app = make(home)
    async with app.run_test(size=(160, 40)) as pilot:
        await ready(pilot)
        await until(pilot, lambda: app.usage.get("five_hour") == 62)
        app.usage["five_hour"] = 5  # stale, as if another window had used some since
        await until(pilot, lambda: asked() >= 3 and app.usage.get("five_hour") == 62)  # asked again unprompted
        app.session_ends = time.time() - 1  # the window's end came
        app.session_over()
        assert "five_hour" not in app.usage and "5-hour" not in app.footer.right
        await until(pilot, lambda: app.usage.get("five_hour") == 62 and app.session_ends > time.time())
        resets = int(time.time()) - 1
        app.on_rate_limit({"rate_limit_info": {"status": "rejected", "resetsAt": resets}})
        await until(pilot, lambda: "limit reached" not in app.footer.right)  # dropped once its reset passed


async def test_keepitgoin_carries_on_when_the_usage_limit_resets(home):
    prompts = lambda: [m["message"]["content"] for m in sent(home) if m.get("type") == "user"]
    app = make(home)
    async with app.run_test(size=(160, 40)) as pilot:
        await ready(pilot)
        assert app.keep_going  # on unless turned off
        await send(pilot, "hit the limit")
        await until(pilot, lambda: not app.busy and "keeps going then" in app.footer.right)
        await until(pilot, lambda: app_module.KEEP_GOING_PROMPT in prompts())  # carried on at the reset
        await until(pilot, lambda: not app.busy and app.stalled is None and app.kept_going == 0)

        await send(pilot, "/keepitgoin")
        await until(pilot, lambda: not app.keep_going)
        assert json.loads((home / "jarvis-window.json").read_text()) == {"keep_going": False}
        await send(pilot, "hit the limit")
        await until(pilot, lambda: app.stalled is not None and not app.busy)
        await pilot.pause(2.5)  # the limit resets, and Jarvis waits
        assert prompts().count(app_module.KEEP_GOING_PROMPT) == 1
        await send(pilot, "/keepitgoin on")  # turned back on after the reset: carries on now
        await until(pilot, lambda: prompts().count(app_module.KEEP_GOING_PROMPT) == 2)
    assert make(home).keep_going  # saved: a new window reads it


async def test_ctrl_x_ctrl_s_sends_the_queued_prompt_now(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "slow")
        await until(pilot, lambda: app.busy and replies(app))
        await send(pilot, "hello")
        assert app.queued == ["hello"] and app.prompt.text == ""
        started = time.monotonic()
        await pilot.press("ctrl+x", "ctrl+s")
        await until(pilot, lambda: "Hello, sir. How may I help?" in replies(app) and not app.busy)
        assert time.monotonic() - started < 3  # not after the slow one finished
        assert any("Interrupted" in text_of(n.renderable) for n in entries(app, NoticeEntry))
        assert [e.text for e in entries(app, UserEntry)] == ["slow", "hello"]
        assert not app.queued and not app.queued_panel.display


async def test_ctrl_x_s_with_nothing_queued_says_so(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await pilot.press("ctrl+x", "ctrl+s")
        assert app.footer.flash_text == "Nothing queued" and not app.busy


async def test_up_arrow_walks_wrapped_rows_before_history(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        app.history = ["older prompt"]
        long = " ".join(f"word{n}" for n in range(60))  # wraps to a few rows, one line
        app.prompt.set_text(long)
        await pilot.pause()
        rows = 0
        while app.prompt.text == long:
            await pilot.press("up")
            rows += 1
            assert rows < 10
        assert app.prompt.text == "older prompt" and rows > 1  # the rows above first, then history
        app.prompt.set_text("one\ntwo\nthree")
        await pilot.pause()
        await pilot.press("up")
        assert app.prompt.text == "one\ntwo\nthree" and app.prompt.cursor_location[0] == 1


async def test_selected_text_is_copied(home):
    from textual import events
    from textual.selection import Selection
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        app.prompt.set_text("hello world")
        app.prompt.selection = Selection((0, 0), (0, 5))
        await pilot.press("ctrl+c")
        assert app.clipboard == "hello" and app.prompt.text == "hello world"  # copied, not cleared
        await send(pilot, "hello")
        await until(pilot, lambda: not app.busy and replies(app))
        reply = entries(app, AssistantEntry)[-1]
        target = next(w for w in reply.walk_children()
                      if w.allow_select and "Hello" in str((w.get_selection(Selection(None, None)) or [""])[0]))
        app.screen.selections = {target: Selection(None, None)}
        app.screen.post_message(events.TextSelected())
        await pilot.pause()
        assert app.clipboard and app.clipboard.strip() in reply.text


async def test_ctrl_x_s_works_whatever_has_focus(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "slow")
        await until(pilot, lambda: app.busy and replies(app))
        await send(pilot, "hello")
        app.transcript.focus()
        await pilot.pause()
        # ctrl let go and pressed again between the keys: kitty-protocol terminals report it
        await pilot.press("ctrl+x", "left_control", "ctrl+s")
        await until(pilot, lambda: "Hello, sir. How may I help?" in replies(app) and not app.busy)
        now = [m for m in sent(home) if m.get("request", {}).get("subtype") == "interrupt"]
        assert now and now[-1]["request"].get("send_now") is True
        assert [e.text for e in entries(app, UserEntry)] == ["slow", "hello"]


async def test_ctrl_q_unqueues_the_newest(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "very slow")
        await until(pilot, lambda: app.busy and replies(app))
        await send(pilot, "hello there")
        await send(pilot, "oops")
        await until(pilot, lambda: len(app.query(UnqueueButton)) == 2)
        await pilot.press("ctrl+q")
        await until(pilot, lambda: app.queued == ["hello there"])
        assert app.is_running  # not Textual's quit
        cancel = [m["request"] for m in sent(home) if m.get("request", {}).get("subtype") == "cancel_async_message"]
        users = [m for m in sent(home) if m.get("type") == "user"]
        assert cancel[-1]["message_uuid"] == users[-1]["uuid"]
        await pilot.press("ctrl+x", "s")
        await until(pilot, lambda: "Hello, sir. How may I help?" in replies(app) and not app.busy)
        assert [e.text for e in entries(app, UserEntry)] == ["very slow", "hello there"]


async def test_clicking_a_queued_prompt_unqueues_it(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await send(pilot, "very slow")
        await until(pilot, lambda: app.busy and replies(app))
        await send(pilot, "first")
        await send(pilot, "second")
        await until(pilot, lambda: len(app.query(UnqueueButton)) == 2)
        await pilot.click(app.query(UnqueueButton).first())
        await until(pilot, lambda: app.queued == ["second"])
        assert len(app.query(UnqueueButton)) == 1
        await pilot.press("ctrl+q")
        await until(pilot, lambda: not app.queued and not app.queued_panel.display)
        await pilot.press("escape")
        await until(pilot, lambda: not app.busy)
        assert [e.text for e in entries(app, UserEntry)] == ["very slow"]


async def test_ctrl_q_with_nothing_queued(home):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await pilot.press("ctrl+q")
        assert app.is_running and app.footer.flash_text == "Nothing queued"


async def test_ctrl_brackets_jump_between_your_messages(home):
    app = make(home)
    async with app.run_test(size=(100, 20)) as pilot:
        await ready(pilot)
        for word in ("table one", "table two", "table three"):
            await send(pilot, word)
            await until(pilot, lambda: not app.busy and len(replies(app)) == len(entries(app, UserEntry)))
        mine = entries(app, UserEntry)

        def at(entry):
            # at the top of the view (with the gap above it, for one after a reply's own gap)
            return entry.has_class("picked") and 0 <= entry.virtual_region.y - app.transcript.scroll_y <= 1

        await pilot.press("ctrl+left_square_bracket")  # back from the end: the last one
        await until(pilot, lambda: at(mine[2]))
        await pilot.press("ctrl+left_square_bracket")
        await until(pilot, lambda: at(mine[1]))
        await pilot.press("ctrl+right_square_bracket")  # and forwards again
        await until(pilot, lambda: at(mine[2]))
        await pilot.press("ctrl+right_square_bracket")  # past the last: back to the end
        await until(pilot, lambda: app.jumped is None and app.transcript.scroll_y == app.transcript.max_scroll_y)
        assert not any(e.has_class("picked") for e in mine)
        await pilot.press("ctrl+right_square_bracket")  # from the end, ctrl+] starts at the first
        await until(pilot, lambda: at(mine[0]))


def thread_replies(thread):
    return [e.text.strip() for e in thread.view.transcript.children if isinstance(e, AssistantEntry)]


async def test_manager_hands_the_work_to_threads(home):
    app = make(home)
    async with app.run_test(size=(140, 40)) as pilot:
        await ready(pilot)
        await send(pilot, '/manager "delegate the work"')
        await until(pilot, lambda: app.manager is not None and len(app.manager.threads) == 2)
        assert app.manager_pane.display
        one, two = app.manager.threads
        assert (one.title, two.title) == ("Say hello", "Edit the notes")
        # The coordinator runs with the window's MCP server, its tools always loaded.
        init = [m for m in sent(home) if (m.get("request") or {}).get("subtype") == "initialize"]
        assert any(m["request"].get("sdkMcpServers") == ["manager"] for m in init)
        replies_to_mcp = [((m.get("response") or {}).get("response") or {}).get("mcp_response") for m in sent(home)]
        listed = [r["result"]["tools"] for r in replies_to_mcp if r and isinstance(r["result"].get("tools"), list)]
        assert {t["name"] for t in listed[0]} == {"start_thread", "message_thread", "list_threads",
                                                  "read_thread", "stop_thread"}
        assert all(t["_meta"]["anthropic/alwaysLoad"] for t in listed[0])
        # Thread 1 finishes; thread 2 waits on a permission prompt, without taking the keys.
        await until(pilot, lambda: one.look == "done" and two.look == "attention")
        assert thread_replies(one) == ["Hello, sir. How may I help?"]
        assert app.prompt.has_focus and app.dialog is None
        rows = app.manager_pane.list.render().renderables
        assert theme.SUCCESS in str(rows[0].spans) and theme.ATTENTION in str(rows[2].spans)
        assert "needs you" in rows[2].plain and "finished" in rows[0].plain
        # The coordinator hears how thread 1 went, shown as a line rather than a message of yours.
        await until(pilot, lambda: "Noted." in replies(app) and not app.busy)
        notices = [text_of(e.renderable) for e in entries(app, NoticeEntry) if not isinstance(e.renderable, Widget)]
        assert any("Thread 1 · Say hello · finished" in n for n in notices)
        assert [e.text for e in entries(app, UserEntry)] == ["delegate the work"]
        # → on the empty prompt goes to the list; ↓ and Enter open thread 2 at its prompt.
        await pilot.press("right")
        assert app.manager_pane.list.has_focus
        await pilot.press("down", "enter")
        assert app.manager_pane.opened is two and two.dialog.has_focus
        await pilot.press("1")
        await until(pilot, lambda: two.look == "done")
        assert thread_replies(two) == ["Changed alpha to beta."]
        assert [e.state for e in two.tools.values()] == ["done"]
        await until(pilot, lambda: replies(app).count("Noted.") == 2)
        # ← on the empty message line is back to the list, and ← again to the coordinator.
        assert two.view.entry.has_focus
        await pilot.press("left")
        assert app.manager_pane.opened is None and app.manager_pane.list.has_focus
        await pilot.press("left")
        assert app.prompt.has_focus


async def test_messaging_a_thread_and_manager_off(home):
    app = make(home)
    async with app.run_test(size=(140, 40)) as pilot:
        await ready(pilot)
        await send(pilot, "/manager delegate the work")
        await until(pilot, lambda: app.manager is not None and len(app.manager.threads) == 2
                    and app.manager.threads[0].look == "done")
        one = app.manager.threads[0]
        await until(pilot, lambda: not app.busy)
        noted = replies(app).count("Noted.")
        await pilot.press("right", "enter")
        assert app.manager_pane.opened is one and one.view.entry.has_focus
        await pilot.press(*"think")
        await pilot.press("enter")
        await until(pilot, lambda: "It is **391**, sir." in thread_replies(one) and one.look == "done")
        assert [e.text for e in one.view.transcript.children if isinstance(e, UserEntry)] == ["hello", "think"]
        await pilot.pause(0.5)
        assert replies(app).count("Noted.") == noted  # what you ask a thread yourself isn't reported
        engines = [one.engine, app.manager.threads[1].engine]
        await pilot.press("escape")  # a thread that's done: Esc is back to the list
        assert app.manager_pane.list.has_focus
        await pilot.press("escape")
        await send(pilot, "/manager off")
        await until(pilot, lambda: any("manager mode is off" in text_of(e.renderable)
                                       for e in entries(app, NoticeEntry) if not isinstance(e.renderable, Widget)))
        assert not app.manager_pane.display and not any(e.running for e in engines)
        init = [m for m in sent(home) if (m.get("request") or {}).get("subtype") == "initialize"]
        assert "sdkMcpServers" not in init[-1]["request"]
