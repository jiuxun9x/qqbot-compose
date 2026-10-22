"""Ordering and cancellation invariants without a model or live QQ traffic."""
import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location('request_gate_fixture', Path(__file__).resolve().parents[1] /
    'plugins/astrbot_plugin_qqbot_guard/request_gate.py')
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class RequestGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.gate = module.RequestGate({'max_concurrent': 2, 'queue_wait_seconds': 1},
            lambda row: None, lambda row: None, lambda: None,
            scope_key=lambda event: event.scope)
        self.tasks = []

    async def asyncTearDown(self):
        self.gate.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def start(self, origin, *, mergeable=True, chars=1, allow_queue=True, scope=None):
        event = SimpleNamespace(unified_msg_origin=origin, scope=scope or origin)
        entered, release, cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
        result = []
        async def pipeline():
            row = await self.gate.acquire(event, asyncio.current_task(), mergeable=mergeable,
                                          chars=chars, allow_queue=allow_queue)
            result.append(row)
            entered.set()
            if row is not None:
                await release.wait()
                self.gate.release(asyncio.current_task())
                await cleanup.wait()
        task = asyncio.create_task(pipeline())
        self.tasks.append(task)
        return SimpleNamespace(task=task, entered=entered, release=release, cleanup=cleanup, result=result, event=event)

    async def admitted(self, row):
        await asyncio.wait_for(row.entered.wait(), 1)

    async def finish(self, row):
        row.release.set(); row.cleanup.set()
        await row.task
        await asyncio.sleep(0)

    async def test_two_conversations_parallel_third_waits(self):
        a, b = self.start('a'), self.start('b')
        await self.admitted(a); await self.admitted(b)
        c = self.start('c')
        await asyncio.sleep(0.01)
        self.assertFalse(c.entered.is_set())
        self.assertEqual(len(self.gate.active), 2)
        await self.finish(a); await self.admitted(c)
        self.assertFalse(b.task.done())
        self.assertEqual(len(self.gate.active), 2)

    async def test_same_conversation_waits_for_history_and_coalesces_text(self):
        a = self.start('a'); await self.admitted(a)
        b, c = self.start('a'), self.start('a')
        await asyncio.sleep(0)
        a.release.set()  # reply has been delivered; history is still pending
        await asyncio.sleep(0.01)
        self.assertFalse(b.entered.is_set())
        other = self.start('other'); await self.admitted(other)
        a.cleanup.set(); await a.task
        await self.admitted(b); await self.admitted(c)
        self.assertEqual(b.result[0].events, (b.event, c.event))
        self.assertIsNone(c.result[0])
        self.assertFalse(other.task.done())

    async def test_media_is_never_coalesced_with_text(self):
        a = self.start('a'); await self.admitted(a)
        b, image, c = self.start('a'), self.start('a', mergeable=False), self.start('a')
        await asyncio.sleep(0)
        await self.finish(a); await self.admitted(b)
        self.assertEqual(b.result[0].events, (b.event,))
        self.assertFalse(image.entered.is_set())
        await self.finish(b); await self.admitted(image)
        self.assertEqual(image.result[0].events, (image.event,))
        self.assertFalse(c.entered.is_set())
        await self.finish(image); await self.admitted(c)

    async def test_cancelled_waiter_does_not_leave_a_slot_or_pending_node(self):
        a = self.start('a'); await self.admitted(a)
        b = self.start('a'); await asyncio.sleep(0)
        b.task.cancel(); await asyncio.gather(b.task, return_exceptions=True)
        self.assertEqual(self.gate.pending_count, 0)
        c = self.start('a'); await asyncio.sleep(0)
        await self.finish(a); await self.admitted(c)

    async def test_stop_removes_only_own_pending_and_other_conversation_survives(self):
        a, other = self.start('a'), self.start('other')
        await self.admitted(a); await self.admitted(other)
        b, c = self.start('a'), self.start('third')
        await asyncio.sleep(0)
        self.assertEqual(self.gate.cancel_pending('a'), 1)
        await self.admitted(b); self.assertIsNone(b.result[0])
        a.task.cancel(); await asyncio.gather(a.task, return_exceptions=True)
        await self.admitted(c)
        self.assertFalse(other.task.done())

    async def test_full_queue_keeps_previously_accepted_messages(self):
        self.gate.settings['max_pending_per_session'] = 1
        a = self.start('a'); await self.admitted(a)
        b = self.start('a'); await asyncio.sleep(0)
        c = self.start('a')
        with self.assertRaisesRegex(module.AdmissionError, 'full'):
            await c.task
        self.assertEqual(self.gate.pending_count, 1)
        await self.finish(a); await self.admitted(b)

    async def test_pending_session_and_character_caps(self):
        self.gate.settings.update(max_pending_sessions=1, max_pending_chars=3)
        a, b = self.start('a'), self.start('b')
        await self.admitted(a); await self.admitted(b)
        accepted = self.start('a', chars=2); await asyncio.sleep(0)
        too_long = self.start('a', chars=2)
        with self.assertRaisesRegex(module.AdmissionError, 'full'): await too_long.task
        third = self.start('third')
        with self.assertRaisesRegex(module.AdmissionError, 'full'): await third.task
        self.assertEqual(self.gate.pending_count, 1)
        await self.finish(a); await self.admitted(accepted)

    async def test_queue_wait_expires_and_is_removed(self):
        self.gate.settings['queue_wait_seconds'] = 0.02
        a = self.start('a'); await self.admitted(a)
        b = self.start('a')
        with self.assertRaisesRegex(module.AdmissionError, 'expired'): await b.task
        self.assertEqual(self.gate.pending_count, 0)
        self.assertIsNotNone(self.gate.for_event(a.event))

    async def test_ambient_group_traffic_is_not_queued_under_load(self):
        a, b = self.start('a'), self.start('b')
        await self.admitted(a); await self.admitted(b)
        ambient = self.start('group-ambient', allow_queue=False)
        with self.assertRaisesRegex(module.AdmissionError, 'skipped'): await ambient.task
        self.assertEqual(self.gate.pending_count, 0)

    async def test_shutdown_does_not_start_pending_models(self):
        a = self.start('a'); await self.admitted(a)
        b = self.start('a'); await asyncio.sleep(0)
        self.gate.close(); await self.admitted(b)
        self.assertIsNone(b.result[0])
        self.assertEqual(self.gate.pending_count, 0)
        closed = self.start('c')
        with self.assertRaisesRegex(module.AdmissionError, 'closed'): await closed.task

    async def test_configuration_cannot_exceed_three_active_requests(self):
        self.gate.settings['max_concurrent'] = 999
        self.assertEqual(self.gate.limit, 3)

    async def test_private_and_two_groups_each_have_an_answering_position(self):
        self.gate.settings['max_concurrent'] = 3
        private = self.start('private-a', scope='private-a')
        group_a = self.start('a-member-1', scope='group-a')
        group_b = self.start('b-member-1', scope='group-b')
        for row in (private, group_a, group_b):
            await self.admitted(row)
        self.assertEqual(len(self.gate.active), 3)
        self.assertEqual(len(self.gate.scope_tails), 3)

    async def test_same_group_members_cannot_consume_spare_global_positions(self):
        self.gate.settings['max_concurrent'] = 3
        first = self.start('member-1', scope='group-a')
        await self.admitted(first)
        second = self.start('member-2', scope='group-a')
        third = self.start('member-3', scope='group-a')
        await asyncio.sleep(0.01)
        self.assertFalse(second.entered.is_set())
        self.assertFalse(third.entered.is_set())
        self.assertEqual(len(self.gate.active), 1)
        other = self.start('other-group-member', scope='group-b')
        await self.admitted(other)
        await self.finish(first)
        await self.admitted(second)
        self.assertFalse(third.entered.is_set())
        self.assertFalse(other.task.done())

    async def test_blocked_group_waiter_does_not_hold_up_another_group(self):
        self.gate.settings['max_concurrent'] = 3
        a, b, private = (self.start('a', scope='group-a'), self.start('b', scope='group-b'),
                         self.start('private', scope='private'))
        for row in (a, b, private):
            await self.admitted(row)
        blocked = self.start('a-other', scope='group-a')
        unrelated = self.start('c', scope='group-c')
        await asyncio.sleep(0.01)
        await self.finish(b)
        await self.admitted(unrelated)
        self.assertFalse(blocked.entered.is_set())
        self.assertFalse(private.task.done())
        self.assertFalse(a.task.done())

    async def test_scope_waits_for_history_commit_even_for_a_different_member(self):
        first = self.start('a1', scope='group-a')
        await self.admitted(first)
        second = self.start('a2', scope='group-a')
        first.release.set()
        await asyncio.sleep(0.01)
        self.assertFalse(second.entered.is_set())
        other = self.start('b1', scope='group-b')
        await self.admitted(other)
        first.cleanup.set()
        await first.task
        await self.admitted(second)
        self.assertFalse(other.task.done())

    async def test_same_group_queue_rotates_between_members(self):
        first = self.start('a', scope='group-a')
        await self.admitted(first)
        a1 = self.start('a', scope='group-a', mergeable=False)
        a2 = self.start('a', scope='group-a', mergeable=False)
        b = self.start('b', scope='group-a', mergeable=False)
        await asyncio.sleep(0.01)
        await self.finish(first)
        await self.admitted(a1)
        await self.finish(a1)
        await self.admitted(b)
        self.assertFalse(a2.entered.is_set())
        await self.finish(b)
        await self.admitted(a2)

    async def test_one_scope_cancellation_keeps_other_scopes_and_unblocks_own_successor(self):
        a, b = self.start('a', scope='group-a'), self.start('b', scope='group-b')
        await self.admitted(a)
        await self.admitted(b)
        successor = self.start('a2', scope='group-a')
        await asyncio.sleep(0.01)
        a.task.cancel()
        await asyncio.gather(a.task, return_exceptions=True)
        await self.admitted(successor)
        await asyncio.sleep(0)
        self.assertIs(self.gate.scope_tails['group-a'], successor.task)
        self.assertFalse(b.task.done())
        self.assertEqual(len(self.gate.active), 2)

    async def test_unrelated_busy_and_blocked_followups_do_not_suppress_ambient_group(self):
        self.gate.settings['max_concurrent'] = 3
        private = self.start('private')
        a = self.start('a', scope='group-a')
        ambient = self.start('b', scope='group-b')
        for row in (private, a, ambient):
            await self.admitted(row)
        supplement = self.start('private')
        await asyncio.sleep(0.01)
        self.assertFalse(self.gate.yield_ambient(ambient.event))
        direct = self.start('c', scope='group-c')
        await asyncio.sleep(0.01)
        self.assertTrue(self.gate.yield_ambient(ambient.event))
        self.assertFalse(supplement.entered.is_set())
        self.assertFalse(direct.entered.is_set())

    async def test_same_group_direct_call_preempts_ambient_with_spare_global_capacity(self):
        self.gate.settings['max_concurrent'] = 3
        ambient = self.start('a', scope='group-a')
        await self.admitted(ambient)
        direct = self.start('b', scope='group-a')
        await asyncio.sleep(0.01)
        self.assertTrue(self.gate.yield_ambient(ambient.event))
        self.assertFalse(direct.entered.is_set())
        self.assertTrue(self.gate.busy_for(direct.event))
        other = SimpleNamespace(unified_msg_origin='c', scope='group-b')
        self.assertFalse(self.gate.busy_for(other))
