"""/jarvis settings, against a fake jarvis.settings (the real one is in ~/jarvis)."""

import pytest

from jarvis_code import jarvis_settings
from jarvis_code.jarvis_settings import JarvisSettingsPage, JarvisSettingsPanel

from test_app import SIZE, home, make, ready, send, until  # noqa: F401 (home is a fixture)


def S(key, category, label, kind, default, value=None, choices=None, **more):
    return {"key": key, "category": category, "label": label, "kind": kind, "default": default,
            "about": f"What {label} does", "choices": choices, "secret": kind == "secret",
            "value": value, **more}


class FakeJarvis:
    def __init__(self):
        self.settings = [
            S("CLAUDE_MODEL", "Claude", "Model", "choice", "claude-haiku-5-5",
              choices=["claude-haiku-5-5", "claude-sonnet-5-5"]),
            S("CLAUDE_THINKING", "Claude", "Thinking", "bool", "off"),
            S("WAKE_THRESHOLD", "Listening", "Wake word sensitivity", "float", "0.5"),
            S("DND", "Do not disturb", "Do not disturb", "bool", "off", value="off"),
            S("DISCORD_TOKEN", "Accounts & keys", "Discord bot token", "secret", "", set=True),
        ]
        self.calls = []

    async def run(self, *args, timeout=30):
        self.calls.append(args)
        if args[0] == "list":
            return {"categories": ["Claude", "Listening", "Do not disturb", "Accounts & keys"],
                    "settings": [dict(s) for s in self.settings]}
        if args[0] == "set":
            s = next(s for s in self.settings if s["key"] == args[1])
            s["value"] = None if args[2] == "default" else args[2]
            return {"message": f"{s['label']}: {args[2]}.", "restart": s["key"] != "DND"}
        if args[0] == "restart":
            return {"message": "Jarvis restarted with the new settings.", "restart": False}
        raise AssertionError(args)


@pytest.fixture
def fake(monkeypatch):
    jarvis = FakeJarvis()
    monkeypatch.setattr(jarvis_settings, "run", jarvis.run)
    return jarvis


def value(fake, key):
    return next(s for s in fake.settings if s["key"] == key)["value"]


async def open_page(pilot):
    await send(pilot, "/jarvis settings")
    await until(pilot, lambda: isinstance(pilot.app.screen, JarvisSettingsPage))
    panel = pilot.app.screen.panel
    await until(pilot, lambda: panel.data is not None)
    return panel


async def test_categories_toggle_and_restart(home, fake):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        panel = await open_page(pilot)
        assert [s["key"] for s in panel.rows()] == ["CLAUDE_MODEL", "CLAUDE_THINKING"]
        await pilot.press("down", "enter")  # Thinking: off -> on
        await until(pilot, lambda: value(fake, "CLAUDE_THINKING") == "on")
        await until(pilot, lambda: panel.unapplied)
        await pilot.press("right")  # Listening
        assert [s["key"] for s in panel.rows()] == ["WAKE_THRESHOLD"]
        await pilot.press("enter", "backspace", "backspace", "backspace", "0", ".", "7", "enter")
        await until(pilot, lambda: value(fake, "WAKE_THRESHOLD") == "0.7")
        await pilot.press("escape")  # changes not applied yet: asks to restart
        assert panel.leaving and isinstance(app.screen, JarvisSettingsPage)
        await pilot.press("enter")
        await until(pilot, lambda: not isinstance(app.screen, JarvisSettingsPage))
        assert ("restart",) in fake.calls


async def test_search_choice_and_secret(home, fake):
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        panel = await open_page(pilot)
        for ch in "model":
            await pilot.press(ch)
        assert [s["key"] for s in panel.rows()] == ["CLAUDE_MODEL"]
        await pilot.press("enter")  # opens the choices
        assert panel.choosing and panel.choosing["key"] == "CLAUDE_MODEL"
        await pilot.press("down", "enter")
        await until(pilot, lambda: value(fake, "CLAUDE_MODEL") == "claude-sonnet-5-5")
        await pilot.press("escape")  # clears the search
        assert panel.search == "" and isinstance(app.screen, JarvisSettingsPage)
        for ch in "token":
            await pilot.press(ch)
        await pilot.press("enter")
        assert "only be changed in" in panel.message and value(fake, "DISCORD_TOKEN") is None
        assert "set" == panel.shown(panel.rows()[0])
        await pilot.press("escape")
        await pilot.press("left", "left")  # Do not disturb: applies at once, no restart asked
        assert panel.rows()[0]["key"] == "DND"


async def test_do_not_disturb_needs_no_restart(home, fake):
    fake.settings = [s for s in fake.settings if s["key"] == "DND"]
    app = make(home)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        panel = await open_page(pilot)
        panel.category = 2
        panel.redraw()
        await pilot.press("enter")
        await until(pilot, lambda: value(fake, "DND") == "on")
        await pilot.pause(0.2)
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, JarvisSettingsPage))
        assert ("restart",) not in fake.calls


def test_command_local_or_over_ssh(tmp_path, monkeypatch):
    monkeypatch.setattr(jarvis_settings, "JARVIS", tmp_path / "jarvis")
    monkeypatch.delenv("JARVIS_HYDE", raising=False)
    assert jarvis_settings.command("list") is None
    monkeypatch.setenv("JARVIS_HYDE", "nate@hyde")
    argv, cwd = jarvis_settings.command("set", "TRELLO_BOARD", "my board")
    assert argv[:4] == ["ssh", "-o", "BatchMode=yes", "-o"] and cwd is None
    assert argv[-1].endswith("jarvis.settings set TRELLO_BOARD 'my board'")
    python = tmp_path / "jarvis" / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    argv, cwd = jarvis_settings.command("list")
    assert argv == [str(python), "-m", "jarvis.settings", "list"] and cwd == str(tmp_path / "jarvis")
