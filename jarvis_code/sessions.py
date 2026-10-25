"""Claude Code's saved conversations and prompt history, read the way Claude Code keeps them,
so /resume and the up arrow see the same things in Jarvis and in plain Claude Code.

Conversations: ~/.claude/projects/<project>/<session-id>.jsonl, one record per line.
Prompt history: ~/.claude/history.jsonl, one line per prompt, tagged with its project.
/manager's threads: ~/.claude/jarvis-manager/<coordinator's session-id>.json, so reopening a
conversation that was in manager mode brings its threads back.
"""

import json
import os
import re
import time
from dataclasses import dataclass

CLAUDE_DIR = os.path.expanduser("~/.claude")
PROJECTS = os.path.join(CLAUDE_DIR, "projects")
HISTORY = os.path.join(CLAUDE_DIR, "history.jsonl")
MANAGERS = os.path.join(CLAUDE_DIR, "jarvis-manager")

PASTED = re.compile(r"\[Pasted text #(\d+)(?: \+\d+ lines)?\]")
NOT_A_PROMPT = ("<command-", "<local-command-", "<bash-", "<system-reminder>", "Caveat:",
                "[Request interrupted")


def project_dir(cwd):
    return os.path.join(PROJECTS, re.sub(r"[^A-Za-z0-9]", "-", cwd))


@dataclass
class Session:
    id: str
    path: str
    title: str
    modified: float
    prompts: int
    branch: str
    cwd: str = ""  # the folder it ran in
    manager: bool = False  # it was in /manager mode: its threads come back with it


def prompt_text(message):
    """The words of a user prompt record, or None if it's a tool result or bookkeeping."""
    content = (message or {}).get("content")
    if isinstance(content, list):
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        if not texts or any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        content = "\n".join(texts)
    if not isinstance(content, str) or not content.strip():
        return None
    if content.lstrip().startswith(NOT_A_PROMPT):
        return None
    return content.strip()


def summarize(path):
    """Title, prompt count, branch, whether it was continued elsewhere, and the folder it ran
    in, of one saved conversation, reading only what's needed."""
    title = ai_title = first_prompt = None
    prompts, branch, continued, cwd = 0, "", False, ""
    with open(path, errors="replace") as f:
        for line in f:
            if '"type":"user"' in line or '"type": "user"' in line:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("isSidechain") or record.get("isMeta") or record.get("isCompactSummary"):
                    continue
                text = prompt_text(record.get("message"))
                if text:
                    prompts += 1
                    first_prompt = first_prompt or text
                branch = record.get("gitBranch") or branch
                cwd = cwd or record.get("cwd") or ""
            elif "custom-title" in line or "ai-title" in line or "continued-in" in line:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                kind = record.get("type")
                if kind == "custom-title":
                    title = record.get("customTitle") or title
                elif kind == "ai-title":
                    ai_title = record.get("aiTitle") or ai_title
                elif kind == "continued-in":
                    continued = True
    return (title or ai_title or first_prompt or "(no prompt)"), prompts, branch, continued, cwd


def _jsonl(directory):
    try:
        return [os.path.join(directory, n) for n in os.listdir(directory) if n.endswith(".jsonl")]
    except OSError:
        return []


def _sessions(paths, limit):
    """Session for each conversation file, newest first, leaving out empty and continued ones,
    and /manager's threads (they come back with their coordinator's conversation)."""
    found = []
    managers, threads = manager_sessions()
    for path in sorted(paths, key=_mtime, reverse=True):
        if limit is not None and len(found) >= limit:
            break
        session_id = os.path.basename(path)[:-6]
        if session_id in threads:
            continue
        try:
            title, prompts, branch, continued, cwd = summarize(path)
            modified = os.path.getmtime(path)
        except OSError:
            continue
        if prompts == 0 or continued:
            continue
        found.append(Session(session_id, path, " ".join(title.split()),
                             modified, prompts, branch, cwd, session_id in managers))
    return found


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def list_sessions(cwd, limit=60):
    """Conversations started in this directory, newest first."""
    return _sessions(_jsonl(project_dir(cwd)), limit)


def list_all_sessions(limit=None):
    """Conversations from every folder, newest first."""
    return _sessions([p for d in _project_dirs() for p in _jsonl(os.path.join(PROJECTS, d))], limit)


def saved_title(path):
    """The conversation's title (set with /rename, or written by Claude Code or add_title), or None."""
    title = None
    try:
        with open(path, errors="replace") as f:
            for line in f:
                if "custom-title" in line or "ai-title" in line:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if record.get("type") == "custom-title" and record.get("customTitle"):
                        return record["customTitle"]
                    if record.get("type") == "ai-title":
                        title = record.get("aiTitle") or title
    except OSError:
        return None
    return title


def add_title(path, session_id, title):
    """Give the conversation a title the way Claude Code does (an ai-title record), so /resume
    here and in plain Claude Code shows it. Headless Claude Code doesn't write one itself."""
    with open(path, "a") as f:
        f.write(json.dumps({"type": "ai-title", "aiTitle": title, "sessionId": session_id},
                           separators=(",", ":")) + "\n")


def find_session(cwd, session_id):
    """The file for a session id (or a unique prefix of one), here or in any project."""
    for directory in [project_dir(cwd)] + [os.path.join(PROJECTS, d) for d in _project_dirs()]:
        path = os.path.join(directory, f"{session_id}.jsonl")
        if os.path.exists(path):
            return path
        try:
            matches = [n for n in os.listdir(directory) if n.startswith(session_id) and n.endswith(".jsonl")]
        except OSError:
            continue
        if len(matches) == 1:
            return os.path.join(directory, matches[0])
    return None


def _project_dirs():
    try:
        return os.listdir(PROJECTS)
    except OSError:
        return []


def load_conversation(path):
    """The records of a saved conversation that the transcript shows again, in order, shaped
    like the engine's own stream messages (user, assistant, compact boundaries)."""
    messages = []
    try:
        f = open(path, errors="replace")
    except OSError:
        return messages
    with f:
        for line in f:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            kind = record.get("type")
            if record.get("isSidechain"):
                continue
            if kind == "user":
                if record.get("isMeta"):
                    continue
                if record.get("isCompactSummary"):
                    messages.append({"type": "system", "subtype": "compact_boundary", "_history": True})
                    continue
                messages.append({"type": "user", "message": record.get("message"),
                                 "tool_use_result": record.get("toolUseResult"),
                                 "isReplay": True, "_history": True,
                                 "timestamp": record.get("timestamp")})
            elif kind == "assistant":
                messages.append({"type": "assistant", "message": record.get("message"),
                                 "parent_tool_use_id": None, "_history": True})
            elif kind == "system" and record.get("subtype") == "compact_boundary":
                messages.append({"type": "system", "subtype": "compact_boundary", "_history": True})
    return messages


# /manager's threads ---------------------------------------------------------------------

def _manager_path(session_id):
    return os.path.join(MANAGERS, re.sub(r"[^A-Za-z0-9-]", "-", session_id) + ".json")


def load_manager(session_id):
    """The threads saved for a conversation in manager mode, oldest first ([] if none)."""
    try:
        with open(_manager_path(session_id)) as f:
            threads = json.load(f).get("threads")
    except (OSError, ValueError, AttributeError):
        return []
    return [t for t in threads if isinstance(t, dict)] if isinstance(threads, list) else []


def save_manager(session_id, threads):
    """Keep a coordinator's threads (dicts: number, title, model, session, status, reply,
    took). With none, the conversation isn't a manager one any more."""
    path = _manager_path(session_id)
    try:
        if not threads:
            if os.path.exists(path):
                os.remove(path)
            return
        os.makedirs(MANAGERS, exist_ok=True)
        with open(path + ".tmp", "w") as f:
            json.dump({"session": session_id, "threads": threads}, f, ensure_ascii=False)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def manager_sessions():
    """(coordinators' session ids, their threads' session ids), from every saved manager."""
    managers, threads = set(), set()
    try:
        names = [n for n in os.listdir(MANAGERS) if n.endswith(".json")]
    except OSError:
        return managers, threads
    for name in names:
        try:
            with open(os.path.join(MANAGERS, name)) as f:
                saved = json.load(f)
            managers.add(saved["session"])
            threads.update(t["session"] for t in saved["threads"] if t.get("session"))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return managers, threads


def time_ago(timestamp, now=None):
    seconds = max(0, (now or time.time()) - timestamp)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


# Prompt history ---------------------------------------------------------------------------

def read_history(cwd, limit=1000):
    """Prompts typed in this directory (in Jarvis or Claude Code), newest first, no repeats."""
    try:
        with open(HISTORY, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 4 * 1024 * 1024))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    prompts, seen = [], set()
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("project") != cwd:
            continue
        text = expand_pastes(entry.get("display", ""), entry.get("pastedContents") or {})
        if text and text not in seen:
            seen.add(text)
            prompts.append(text)
            if len(prompts) >= limit:
                break
    return prompts


def expand_pastes(display, pasted):
    def put_back(match):
        item = pasted.get(match.group(1)) or {}
        return item.get("content", match.group(0)) if item.get("type", "text") == "text" else match.group(0)
    return PASTED.sub(put_back, display)


def add_history(text, cwd, session_id=None):
    entry = {"display": text, "pastedContents": {}, "timestamp": int(time.time() * 1000),
             "project": cwd}
    if session_id:
        entry["sessionId"] = session_id
    try:
        with open(HISTORY, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
