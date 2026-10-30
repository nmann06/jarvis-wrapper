"""Runs of tool calls, folded to one line the way Claude Code folds them:

    ∴ Thought for 9s (ctrl+o to expand)
    ● Read(jarvis/timers.py)  (+3 more · 1 failed)
      ⎿  Read 120 lines

Tool calls one after another share one line, the latest call, with a count of the ones before
it. Jarvis's words end a run, and so do your prompts and notices (errors, interruptions); the
next call starts a new run under them. Thinking doesn't end one, and can start one: the run
carries on past it, and its thoughts fold into one line, the latest, with their time added up,
so no two ∴ lines sit one above the other. Calls that need you (a question, a plan to approve)
or show their own progress (agents) stand on their own, and a call waiting on a permission
prompt comes out of the fold while it waits. A click on the line (or ctrl+o, with everything
else) shows the whole run; ▲ collapse folds it again.

Kept free of Textual so it can be tested on its own. A run's members are tool entries: anything
with display, verbose, state, waiting, set_class(add, name) and redraw(); its thoughts have
display, verbose, shown (done thinking), folded and also (the thoughts their line stands for),
set_class and redraw(), which sets display. What they join is decided by their run_role
("join", "alone", "think", "pass"; anything else, or none, ends the run).
"""

from rich.text import Text

from . import theme

ALONE = {"AskUserQuestion", "ExitPlanMode", "Agent", "Task"}


def role(name, quiet=False):
    """How a tool call takes part in runs: quiet ones (shown only with ctrl+o) pass through."""
    if quiet:
        return "pass"
    return "alone" if name in ALONE else "join"


class ToolRun:
    def __init__(self):
        self.members = []    # the tool entries, oldest first
        self.thoughts = []   # the thinking among them, oldest first
        self.widgets = []    # the members, thoughts and quiet calls, in order
        self.opened = False  # clicked open, with ctrl+o off
        self.lead = False    # it starts right under a reply: no gap above its first line
        self.hidden = set()  # ids of what the fold keeps out of sight

    @property
    def last(self):
        return self.members[-1] if self.members else None

    @property
    def expanded(self):
        return self.opened or any(w.verbose for w in self.members + self.thoughts)

    def add(self, entry):
        previous = self.last
        self.begin(entry)
        self.members.append(entry)
        self.widgets.append(entry)
        entry.run = self
        self.changed(entry)
        if previous is not None:
            previous.redraw()  # its line no longer speaks for the run

    def think(self, thought):
        self.begin(thought)
        self.thoughts.append(thought)
        self.widgets.append(thought)
        thought.run = self
        self.changed()

    def pass_by(self, widget):
        self.widgets.append(widget)

    def begin(self, widget):
        if not self.widgets:
            self.lead = widget.has_class("after-reply")

    def folded(self):
        """The members out of sight: all but the latest, unless one is waiting on you."""
        if self.expanded:
            return []
        return [m for m in self.members[:-1] if not m.waiting]

    def note(self):
        """(hidden, failed, rejected): what the latest call's line stands in for; None if nothing."""
        hidden = self.folded()
        if not hidden:
            return None
        return (len(hidden), sum(m.state == "error" for m in hidden), sum(m.state == "rejected" for m in hidden))

    def is_folded_line(self, entry):
        return entry is self.last and self.note() is not None

    def hides(self, widget):
        """Whether widget is out of sight for the fold (opening the run shows it)."""
        return id(widget) in self.hidden

    def closer(self):
        """What carries ▲ collapse once the run is opened: the latest call, or thought."""
        return self.last or (self.thoughts[-1] if self.thoughts else None)

    def open(self):
        self.opened = True
        self.changed()

    def close(self):
        self.opened = False
        self.changed()

    def changed(self, member=None):
        """Show and hide the members, and draw the latest's line again (unless member, which
        changed, just was)."""
        self.hidden = {id(m) for m in self.folded()}
        for m in self.members:
            m.display = id(m) not in self.hidden
        self.fold_thoughts()
        if self.lead:  # whatever shows first sits right under the reply
            first = next((w for w in self.widgets if w.display), None)
            for w in self.widgets:
                w.set_class(w is first, "after-reply")
        if self.last is not None and member is not self.last:
            self.last.redraw()

    def fold_thoughts(self):
        """Folded, the latest thought done stands for the others, which go out of sight."""
        done = [t for t in self.thoughts if t.shown]
        standing = done[-1] if done and not self.expanded else None
        for t in self.thoughts:
            folded = standing is not None and t is not standing
            also = [d for d in done if d is not standing] if t is standing else []
            if folded and t.shown:
                self.hidden.add(id(t))
            if (t.folded, t.also) != (folded, also):
                t.folded, t.also = folded, also
                t.redraw()


class Runs:
    """The runs of one transcript: which run the next tool call joins."""

    def __init__(self):
        self.current = None

    def add(self, widget):
        kind = getattr(widget, "run_role", None)
        if kind == "join":
            if self.current is None:
                self.current = ToolRun()
            self.current.add(widget)
        elif kind == "think":
            if self.current is None:
                self.current = ToolRun()
            self.current.think(widget)
        elif kind == "pass":
            if self.current is not None:
                self.current.pass_by(widget)
        else:
            self.current = None

    def clear(self):
        self.current = None


def note_text(note):
    """  (+3 more · 1 failed), after the latest call of a folded run."""
    hidden, failed, rejected = note
    text = Text(f"  (+{hidden} more", style=theme.DIM)
    for n, word in ((failed, "failed"), (rejected, "rejected")):
        if n:
            text.append(" · ", style=theme.DIM)
            text.append(f"{n} {word}", style=theme.ERROR)
    text.append(")", style=theme.DIM)
    return text
