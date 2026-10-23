# jarvis-wrapper

The Jarvis window: Claude Code with a screen of its own, in blue. It runs the real `claude`
headless (`claude -p`, stream-json both ways) and draws the conversation in a Textual app, with
Jarvis's persona on top. What it does and how it's used are in "The Jarvis window" in the
[jarvis](https://github.com/nmann06/jarvis) README and its `docs/terminal.md`.

On hyde it lives in `~/jarvis-wrapper`; `jarvis` in the shell (`~/.bashrc`) runs `jarvis-code`,
and `jarvis` in PowerShell (`deploy/jarvis.ps1` in jarvis) copies `jarvis_code/`, `launch.py`
and `requirements.txt` from here to the Windows laptop with `jarvis --update`.

## Setup

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./jarvis-code
```

## Tests

```
.venv/bin/python -m pytest
```

They run against a fake Claude Code (`tests/fake_claude.py`); `jarvis-code --log FILE` records
everything said with the real one.
