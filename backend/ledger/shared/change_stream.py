"""Change-stream iteration that keeps the persisted resume token fresh while idle.

Workers save ``change["_id"]`` after processing each event, but their ``$match`` filters
are narrow — hours can pass with no matching event while the cluster oplog keeps
growing. A token that only advances on matches goes stale, and every restart then
resumes from it, forcing the server to scan the whole oplog since then (observed:
median 1.7h, ~400k oplog entries per open).

``stream.resume_token`` is the server's postBatchResumeToken once the current batch is
drained: it advances past filtered-out oplog entries too. Checkpointing it on idle
polls bounds restart replay to ~``interval`` seconds.

Safety: ``try_next()`` returns None only after every event the server returned has been
yielded, and a generator resumes only after the caller's loop body (process + save)
finishes — so the idle token never moves past an unprocessed event.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

IDLE_CHECKPOINT_SECONDS = 30.0


def iter_with_idle_checkpoint(
    stream: Any,
    checkpoint: Callable[[Any], None],
    interval: float = IDLE_CHECKPOINT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[dict]:
    """Yield change events; on idle polls, persist ``stream.resume_token`` at most every ``interval`` s."""
    last_checkpoint = clock()
    while stream.alive:
        change = stream.try_next()
        if change is not None:
            yield change
            # The caller saved this event's token in its loop body.
            last_checkpoint = clock()
            continue
        token = stream.resume_token
        if token is not None and clock() - last_checkpoint >= interval:
            checkpoint(token)
            last_checkpoint = clock()
