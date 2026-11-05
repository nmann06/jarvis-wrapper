import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


@pytest.fixture(autouse=True)
def no_system_clipboard(monkeypatch):
    """Copies stay in the app (app.clipboard), off the desktop's clipboard."""
    from jarvis_code import app
    monkeypatch.setattr(app, "system_copy", lambda text: False)


@pytest.fixture(autouse=True)
def private_presence(monkeypatch, tmp_path):
    """Test windows report what they're doing in a folder of their own, not the real one that
    hyde's nightly restart reads."""
    from jarvis_code import presence
    monkeypatch.setattr(presence, "DIR", str(tmp_path / "jarvis-windows"))
