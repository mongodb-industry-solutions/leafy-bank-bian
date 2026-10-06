"""iter_with_idle_checkpoint — resume token stays fresh while the filtered stream is idle."""

from shared.change_stream import iter_with_idle_checkpoint


class FakeStream:
    """Scripted change stream: each step is an event dict or None (idle poll), paired with
    the server resume token visible after that step."""

    def __init__(self, steps):
        self._steps = list(steps)
        self.resume_token = None

    @property
    def alive(self):
        return bool(self._steps)

    def try_next(self):
        event, token = self._steps.pop(0)
        self.resume_token = token
        return event


class FakeClock:
    def __init__(self, step):
        self.now = 0.0
        self.step = step

    def __call__(self):
        self.now += self.step
        return self.now


def test_idle_polls_checkpoint_server_token_after_interval():
    stream = FakeStream([(None, {"t": 1}), (None, {"t": 2}), (None, {"t": 3})])
    saved = []
    list(iter_with_idle_checkpoint(stream, saved.append, interval=30, clock=FakeClock(20)))
    # clock: start=20, poll1 sees 40 (<30 elapsed? 20 → no), poll2 sees 60 (40 ≥ 30 → save)
    assert saved == [{"t": 2}]


def test_events_are_yielded_and_reset_idle_timer():
    stream = FakeStream([({"_id": "e1"}, {"t": 1}), (None, {"t": 2})])
    saved = []
    events = list(iter_with_idle_checkpoint(stream, saved.append, interval=30, clock=FakeClock(20)))
    assert events == [{"_id": "e1"}]
    # Only 20s idle since the event — no checkpoint yet.
    assert saved == []


def test_idle_checkpoint_never_runs_before_caller_finishes_event():
    stream = FakeStream([({"_id": "e1"}, {"t": 1}), (None, {"t": 2})])
    order = []
    gen = iter_with_idle_checkpoint(stream, lambda t: order.append(("idle", t)),
                                    interval=0, clock=FakeClock(1))
    for change in gen:
        order.append(("processed", change["_id"]))
    assert order == [("processed", "e1"), ("idle", {"t": 2})]


def test_no_token_means_no_checkpoint():
    stream = FakeStream([(None, None), (None, None)])
    saved = []
    list(iter_with_idle_checkpoint(stream, saved.append, interval=0, clock=FakeClock(100)))
    assert saved == []
