"""Bounded parallel requests, with an ordered worker for each scope.

Waiting events remain in their original pipeline so attachment cleanup cannot
run before they are processed. Only consecutive text-only events may coalesce.
An origin owns its history; a scope owns one answering position (one QQ group
or one private conversation). Neither origin nor scope may run twice at once.
All mutations run on the asyncio loop; callbacks must not await.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass, field
import time


class AdmissionError(Exception):
    pass


@dataclass
class Request:
    event: object
    task: asyncio.Task
    origin: str
    events: tuple
    scope: str
    started: float = field(default_factory=time.monotonic)
    kind: str = "routing"
    deadline: float = 0
    image_job: str | None = None
    watchdog: asyncio.Task | None = None
    progress: asyncio.Task | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class Pending:
    event: object
    task: asyncio.Task
    future: asyncio.Future
    mergeable: bool
    chars: int


class RequestGate:
    def __init__(self, settings, on_grant, on_release, on_change, *, scope_key=None):
        self.settings = settings
        self.on_grant, self.on_release, self.on_change = on_grant, on_release, on_change
        self.active = OrderedDict()
        self.tails = {}
        self.scope_tails = {}
        self.pending = OrderedDict()
        self.closed = False
        self.scope_key = scope_key or (lambda event: event.unified_msg_origin)

    @property
    def limit(self):
        return max(1, min(3, int(self.settings.get("max_concurrent", 1))))

    @property
    def pending_count(self):
        return sum(len(rows) for rows in self.pending.values())

    def for_event(self, event):
        return next((row for row in self.active.values() if row.event is event), None)

    def for_origin(self, origin):
        return next((row for row in self.active.values() if row.origin == origin), None)

    def for_scope(self, event):
        scope = self.scope_key(event)
        return next((row for row in self.active.values() if row.scope == scope), None)

    def pending_in_scope(self, event):
        scope = self.scope_key(event)
        return sum(len(rows) for rows in self.pending.values()
                   if rows and self.scope_key(rows[0].event) == scope)

    def busy_for(self, event):
        return (not self._available(event.unified_msg_origin, self.scope_key(event))
                or bool(self.pending_in_scope(event)))

    def yield_ambient(self, event):
        # Other groups and private conversations are independent. An ambient
        # reply yields to this group's direct calls, or to a globally full
        # queue, rather than becoming silent whenever any other scope is busy.
        ready_waiter = any(rows and self._scope_available(origin, self.scope_key(rows[0].event))
                           for origin, rows in self.pending.items())
        return bool(self.pending_in_scope(event) or
                    (ready_waiter and len(self.active) >= self.limit))

    def has_origin(self, origin):
        tail = self.tails.get(origin)
        return origin in self.pending or bool(tail and not tail.done())

    def _scope_available(self, origin, scope):
        tail = self.tails.get(origin)
        worker = self.scope_tails.get(scope)
        return (tail is None or tail.done()) and (worker is None or worker.done())

    def _available(self, origin, scope):
        return len(self.active) < self.limit and self._scope_available(origin, scope)

    def _grant(self, event, task, events):
        row = Request(event, task, event.unified_msg_origin, events, self.scope_key(event))
        self.active[task] = row
        self.tails[row.origin] = task
        self.scope_tails[row.scope] = task
        task.add_done_callback(self._done)
        self.on_grant(row)
        self.on_change()
        return row

    async def acquire(self, event, task, *, mergeable=False, chars=0, allow_queue=True):
        if self.closed:
            raise AdmissionError("closed")
        if task in self.active:
            return self.active[task]
        origin = event.unified_msg_origin
        self._pump()
        if not self.pending.get(origin) and self._available(origin, self.scope_key(event)):
            return self._grant(event, task, (event,))
        if not allow_queue or not self.settings.get("queue_enabled", True):
            raise AdmissionError("skipped")
        rows = self.pending.get(origin, ())
        if ((not rows and len(self.pending) >= int(self.settings.get("max_pending_sessions", 8)))
                or len(rows) >= int(self.settings.get("max_pending_per_session", 5))
                or chars + sum(row.chars for row in rows) > int(self.settings.get("max_pending_chars", 8000))):
            raise AdmissionError("full")
        future = asyncio.get_running_loop().create_future()
        item = Pending(event, task, future, mergeable, chars)
        self.pending.setdefault(origin, deque()).append(item)
        self.on_change()
        try:
            return await asyncio.wait_for(future, timeout=max(0.01, float(self.settings.get("queue_wait_seconds", 660))))
        except asyncio.TimeoutError:
            raise AdmissionError("expired") from None
        finally:
            # Also handles cancellation by the framework's own /stop command.
            rows = self.pending.get(origin)
            if rows and item in rows:
                rows.remove(item)
                if not rows:
                    del self.pending[origin]
            self._pump()
            self.on_change()

    def _pump(self):
        if self.closed:
            return
        for origin in list(self.pending):
            rows = self.pending.get(origin)
            if not rows or not self._available(origin, self.scope_key(rows[0].event)):
                continue
            while rows and (rows[0].task.done() or rows[0].future.done()):
                rows.popleft()
            if not rows:
                del self.pending[origin]
                continue
            first = rows.popleft()
            batch = [first]
            while first.mergeable and rows and rows[0].mergeable:
                follower = rows.popleft()
                if not follower.future.done() and not follower.task.done():
                    batch.append(follower)
            if not rows:
                del self.pending[origin]
            else:
                # A continuous media/attachment sender must not indefinitely
                # stay ahead of other members waiting in the same QQ group.
                self.pending.move_to_end(origin)
            row = self._grant(first.event, first.task, tuple(item.event for item in batch))
            first.future.set_result(row)
            for follower in batch[1:]:
                follower.future.set_result(None)

    def release(self, task, *, keep_tail=True):
        row = self.active.pop(task, None)
        if row:
            self.on_release(row)
            if not keep_tail and self.tails.get(row.origin) is task:
                del self.tails[row.origin]
            if not keep_tail and self.scope_tails.get(row.scope) is task:
                del self.scope_tails[row.scope]
        # The task may still be committing history. Its conversation tail is
        # released only by _done; other conversations may use the free slot.
        self._pump()
        self.on_change()

    def _done(self, task):
        self.release(task)
        for origin, tail in list(self.tails.items()):
            if tail is task:
                del self.tails[origin]
        for scope, tail in list(self.scope_tails.items()):
            if tail is task:
                del self.scope_tails[scope]
        self._pump()
        self.on_change()

    def cancel_pending(self, origin=None):
        count = 0
        for key in list(self.pending):
            if origin is not None and key != origin:
                continue
            for row in self.pending.pop(key):
                if not row.future.done():
                    row.future.set_result(None)
                    count += 1
        self.on_change()
        return count

    def close(self):
        self.closed = True
        self.cancel_pending()
