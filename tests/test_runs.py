"""Runs of tool calls folding to one line (runs.py), without a window."""

from jarvis_code import runs, theme


class Tool:
    """Stands in for a ToolEntry: what runs.py reads and sets."""

    def __init__(self, name="Bash", quiet=False, state="done", after_reply=False):
        self.run_role = runs.role(name, quiet)
        self.state = state
        self.verbose = False
        self.waiting = False
        self.display = True
        self.run = None
        self.classes = {"after-reply"} if after_reply else set()
        self.redraws = 0

    def has_class(self, name):
        return name in self.classes

    def set_class(self, add, name):
        (self.classes.add if add else self.classes.discard)(name)

    def redraw(self):
        self.redraws += 1


class Other(Tool):
    """A reply, prompt, notice (role None: ends the run) or a quiet call (role "pass")."""

    def __init__(self, role=None):
        super().__init__()
        self.run_role = role


class Thought(Tool):
    """Stands in for a ThinkingEntry: shown once it's done, unless the fold hides it."""

    def __init__(self, shown=True, after_reply=False):
        super().__init__(after_reply=after_reply)
        self.run_role = "think"
        self.shown = shown
        self.folded = False
        self.also = []
        self.display = shown

    def redraw(self):
        super().redraw()
        self.display = self.shown and not self.folded

    def finish(self):
        self.shown = True
        self.redraw()
        self.run.changed()


def shown(items):
    return [item.display for item in items]


def test_calls_one_after_another_share_the_latest_line():
    transcript = runs.Runs()
    calls = [Tool(), Tool("Read"), Tool("Grep")]
    for call in calls:
        transcript.add(call)
    run = calls[0].run
    assert all(call.run is run for call in calls) and run.members == calls
    assert shown(calls) == [False, False, True]
    assert run.note() == (2, 0, 0)
    assert run.is_folded_line(calls[2]) and not run.is_folded_line(calls[1])


def test_one_call_on_its_own_has_no_count():
    transcript = runs.Runs()
    transcript.add(call := Tool())
    assert call.display and call.run.note() is None and not call.run.is_folded_line(call)


def test_words_end_a_run_and_the_next_call_starts_another():
    transcript = runs.Runs()
    first = [Tool(), Tool()]
    for call in first:
        transcript.add(call)
    transcript.add(Other())  # Jarvis's reply (or a prompt, a notice, an error)
    second = [Tool(), Tool(), Tool()]
    for call in second:
        transcript.add(call)
    assert first[0].run is not second[0].run
    assert first[0].run.note() == (1, 0, 0) and second[0].run.note() == (2, 0, 0)
    assert shown(first + second) == [False, True, False, False, True]


def test_thinking_stays_in_sight_and_the_run_goes_on_past_it():
    transcript = runs.Runs()
    one, thought, two = Tool(), Thought(), Tool()
    for item in (one, thought, two):
        transcript.add(item)
    assert one.run is two.run is thought.run
    assert shown([one, thought, two]) == [False, True, True]


def test_thoughts_in_a_run_fold_into_the_latest():
    transcript = runs.Runs()
    items = [Thought(), Tool(), Thought(), Tool(), Thought(), Tool()]
    for item in items:
        transcript.add(item)
    first, _, second, _, third, _ = items
    run = third.run
    assert all(item.run is run for item in items)
    assert shown(items) == [False, False, False, False, True, True]  # one ∴ line, then the calls' line
    assert third.also == [first, second] and not first.also
    assert run.hides(first) and run.hides(items[1]) and not run.hides(third)
    run.open()  # a click shows everything in order again
    assert shown(items) == [True] * 6 and not third.also
    run.close()
    assert shown(items) == [False, False, False, False, True, True]
    for item in items:  # and so does ctrl+o
        item.verbose = True
    run.changed()
    assert shown(items) == [True] * 6


def test_thoughts_one_after_another_fold_too():
    transcript = runs.Runs()
    first, quiet, second = Thought(), Other("pass"), Thought()
    for item in (first, quiet, second, Other()):  # a quiet call between, then the reply
        transcript.add(item)
    assert shown([first, second]) == [False, True] and second.also == [first]
    third = Thought()
    transcript.add(third)  # after the reply: a run of its own
    assert third.run is not second.run and third.display


def test_a_thought_still_going_leaves_the_last_one_standing():
    transcript = runs.Runs()
    first, call, second = Thought(), Tool(), Thought(shown=False)
    for item in (first, call, second):
        transcript.add(item)
    assert shown([first, call, second]) == [True, True, False]
    assert not first.also and not first.run.hides(second)  # nothing to find in it yet
    second.finish()  # done thinking: it takes over the line
    assert shown([first, call, second]) == [False, True, True] and second.also == [first]


def test_calls_that_need_you_stand_on_their_own():
    transcript = runs.Runs()
    before, question, after = Tool(), Tool("AskUserQuestion"), Tool()
    for item in (Tool(), before, question, after, Tool("Agent"), Tool("ExitPlanMode")):
        transcript.add(item)
    assert question.run is None and question.display
    assert before.display and before.run.note() == (1, 0, 0)  # the run before the question ends there
    assert after.run.members == [after]  # and the next call starts one of its own


def test_quiet_calls_pass_through():
    transcript = runs.Runs()
    one, quiet, two = Tool(), Tool("TaskUpdate", quiet=True), Tool()
    for item in (one, quiet, two):
        transcript.add(item)
    assert one.run is two.run and quiet.run is None and two.run.note() == (1, 0, 0)


def test_the_count_says_what_failed():
    transcript = runs.Runs()
    calls = [Tool(state="error"), Tool(state="rejected"), Tool(state="done"), Tool(state="running")]
    for call in calls:
        transcript.add(call)
    assert calls[0].run.note() == (3, 1, 1)
    text = runs.note_text((3, 1, 1))
    assert text.plain == "  (+3 more · 1 failed · 1 rejected)"
    assert any(span.style == theme.ERROR and text.plain[span.start:span.end] == "1 failed" for span in text.spans)
    assert runs.note_text((2, 0, 0)).plain == "  (+2 more)"


def test_a_call_failing_out_of_sight_draws_the_line_again():
    transcript = runs.Runs()
    hidden, latest = Tool(state="running"), Tool(state="running")
    transcript.add(hidden)
    transcript.add(latest)
    before = latest.redraws
    hidden.state = "error"
    hidden.run.changed(hidden)
    assert latest.redraws == before + 1 and latest.run.note() == (1, 1, 0)


def test_a_call_waiting_on_a_permission_prompt_comes_out_of_the_fold():
    transcript = runs.Runs()
    calls = [Tool(), Tool(), Tool()]
    for call in calls:
        transcript.add(call)
    calls[0].waiting = True
    calls[0].run.changed(calls[0])
    assert shown(calls) == [True, False, True] and calls[0].run.note() == (1, 0, 0)
    calls[0].waiting = False
    calls[0].run.changed(calls[0])
    assert shown(calls) == [False, False, True]


def test_a_click_opens_the_run_and_collapse_folds_it():
    transcript = runs.Runs()
    calls = [Tool(), Tool(), Tool()]
    for call in calls:
        transcript.add(call)
    run = calls[0].run
    run.open()
    assert shown(calls) == [True, True, True] and run.note() is None and not run.is_folded_line(calls[2])
    run.close()
    assert shown(calls) == [False, False, True] and run.note() == (2, 0, 0)


def test_ctrl_o_shows_every_call():
    transcript = runs.Runs()
    calls = [Tool(), Tool()]
    for call in calls:
        transcript.add(call)
    for call in calls:
        call.verbose = True
    calls[-1].run.changed()
    assert shown(calls) == [True, True] and calls[0].run.note() is None


def test_a_run_right_under_a_reply_keeps_no_gap_above_its_line():
    transcript = runs.Runs()
    first, thought, second = Tool(after_reply=True), Thought(shown=False), Tool()
    transcript.add(first)
    transcript.add(thought)  # still thinking: the spinner says so
    transcript.add(second)
    assert second.has_class("after-reply") and not first.has_class("after-reply")
    thought.finish()
    assert thought.has_class("after-reply") and not second.has_class("after-reply")
    second.run.open()
    assert first.has_class("after-reply") and not thought.has_class("after-reply")


def test_a_new_call_draws_the_old_latest_again_without_its_count():
    transcript = runs.Runs()
    transcript.add(one := Tool())
    before = one.redraws
    transcript.add(Tool())
    assert one.redraws == before + 1 and one is not one.run.last


def test_a_thought_right_under_a_reply_starts_the_run_without_a_gap():
    transcript = runs.Runs()
    thought, call = Thought(shown=False, after_reply=True), Tool()
    transcript.add(thought)
    transcript.add(call)
    assert call.has_class("after-reply")  # what shows first sits under the reply
    thought.finish()
    assert thought.has_class("after-reply") and not call.has_class("after-reply")


def test_clear_starts_afresh():
    transcript = runs.Runs()
    transcript.add(one := Tool())
    transcript.clear()
    transcript.add(two := Tool())
    assert one.run is not two.run
