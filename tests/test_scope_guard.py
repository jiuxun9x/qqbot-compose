"""Group workers stay separate from private chats and other groups."""
import asyncio
import json
from types import SimpleNamespace
import unittest

import test_guard as base


class ScopeGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        base.GuardTests.setUp(self)
        self.guard.routing['concurrency'].update(max_concurrent=3, queue_enabled=True)
        self.guard.routing['group_reply'].update(probability=0.05, cooldown_seconds=5)
        self.guard._random = lambda: 0
        self.tasks = []
        self.calls = []
        self.hold_question = None
        self.classifying, self.classifier_release = asyncio.Event(), asyncio.Event()

        async def classify(**kwargs):
            body = json.loads(kwargs['prompt'])
            self.calls.append(body)
            if body['question'] == self.hold_question:
                self.classifying.set()
                await self.classifier_release.wait()
            engagement = body.get('engagement') or {}
            return SimpleNamespace(completion_text=json.dumps({
                'intent': 'chat', 'difficulty': 'normal', 'prompt': '',
                'addressed_to_bot': bool(engagement.get('bot_mentioned') or engagement.get('named_prefix')),
                'should_reply': True,
            }))
        self.guard.context.get_provider_by_id = lambda _: SimpleNamespace(text_chat=classify)

    async def asyncTearDown(self):
        await self.guard.terminate()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def event(self, text='小助手你好', sender='a', group='alpha'):
        event = base.Event(private=not group, text=text, messages=[base.guard_module.Plain(text)])
        event.unified_msg_origin = 'napcat:' + ('GroupMessage:' + sender + '_' + group if group else 'FriendMessage:' + sender)
        event.get_group_id = lambda: group
        event.get_sender_id = lambda: sender
        return event

    def start(self, event, *, hold_commit=False):
        ready, finish, committing, commit_release = (asyncio.Event() for _ in range(4))
        async def pipeline():
            await self.guard.guard(event)
            ready.set()
            if not event.stopped:
                await finish.wait()
                await self.guard.group_reply_finished(event)
                if hold_commit:
                    committing.set()
                    await commit_release.wait()
        task = asyncio.create_task(pipeline())
        self.tasks.append(task)
        return SimpleNamespace(task=task, event=event, ready=ready, finish=finish,
                               committing=committing, commit_release=commit_release)

    async def admitted(self, row):
        await asyncio.wait_for(row.ready.wait(), 1)
        self.assertFalse(row.event.stopped)

    async def pending(self, count):
        async def wait():
            while self.guard._gate.pending_count != count:
                await asyncio.sleep(0.005)
        await asyncio.wait_for(wait(), 1)

    async def finish(self, row):
        row.finish.set()
        row.commit_release.set()
        await asyncio.wait_for(row.task, 1)
        await asyncio.sleep(0)

    async def test_private_and_two_different_groups_answer_together(self):
        rows = [self.start(self.event('你好', group='')),
                self.start(self.event(group='alpha')), self.start(self.event(group='beta'))]
        for row in rows:
            await self.admitted(row)
        self.assertEqual(self.guard._request_status()['active_count'], 3)
        self.assertEqual(len({row.scope for row in self.guard._gate.active.values()}), 3)

    async def test_same_group_two_members_wait_but_other_groups_and_private_do_not(self):
        first = self.start(self.event(sender='a'))
        await self.admitted(first)
        second = self.start(self.event(sender='b'))
        await self.pending(1)
        private = self.start(self.event('你好', group=''))
        other = self.start(self.event(group='beta'))
        await self.admitted(private)
        await self.admitted(other)
        self.assertFalse(second.ready.is_set())
        await self.finish(first)
        await self.admitted(second)
        self.assertFalse(private.task.done())
        self.assertFalse(other.task.done())
        self.assertEqual(second.event.get_extra('qqbot_merged_messages'), None)

    async def test_other_busy_scopes_do_not_disable_idle_group_five_percent_policy(self):
        private = self.start(self.event('你好', group=''))
        other = self.start(self.event(group='beta'))
        await self.admitted(private)
        await self.admitted(other)
        ambient = self.start(self.event('这是一个适合闲聊的话题'))
        await self.admitted(ambient)
        self.assertTrue(ambient.event.get_extra('qqbot_group_spontaneous'))
        self.assertEqual(self.guard._request_status()['active_count'], 3)
        await self.finish(ambient)
        self.guard._group_last_reply.clear()
        self.guard._random = lambda: 0.051
        missed = self.start(self.event('另一个话题'))
        await asyncio.wait_for(missed.task, 1)
        self.assertTrue(missed.event.stopped)

    async def test_busy_group_chatter_is_collected_without_queueing_other_groups(self):
        first = self.start(self.event())
        await self.admitted(first)
        before = len(self.calls)
        chatter = self.event('我在和小王说话', sender='b')
        await self.guard.guard(chatter)
        self.assertTrue(chatter.stopped)
        self.assertEqual(len(self.calls), before)
        self.assertEqual(self.guard._gate.pending_count, 0)
        other = self.start(self.event('小助手说两句', group='beta'))
        await self.admitted(other)
        self.assertFalse(first.task.done())
        self.assertFalse(any('小王' in str(item) for item in other.event.get_extra('qqbot_group_context')))

    async def test_same_group_direct_call_preempts_ambient_even_with_free_positions(self):
        self.hold_question = '一个适合闲聊的话题'
        ambient = self.start(self.event(self.hold_question))
        await asyncio.wait_for(self.classifying.wait(), 1)
        direct = self.start(self.event('小助手请回答', sender='b'))
        await self.pending(1)
        other = self.start(self.event(group='beta'))
        await self.admitted(other)
        self.classifier_release.set()
        await asyncio.wait_for(ambient.task, 1)
        await self.admitted(direct)
        self.assertTrue(ambient.event.stopped)
        self.assertFalse(other.task.done())

    async def test_blocked_private_followup_does_not_suppress_another_groups_ambient(self):
        private = self.start(self.event('你好', group=''))
        other = self.start(self.event(group='beta'))
        await self.admitted(private)
        await self.admitted(other)
        extra = self.start(self.event('再补充一句', group=''))
        await self.pending(1)
        ambient = self.start(self.event('一个适合闲聊的话题'))
        await self.admitted(ambient)
        self.assertTrue(ambient.event.get_extra('qqbot_group_spontaneous'))
        self.assertFalse(extra.ready.is_set())

    async def test_stop_cancels_only_the_callers_group_request(self):
        first = self.start(self.event())
        other = self.start(self.event(group='beta'))
        private = self.start(self.event('你好', group=''))
        for row in (first, other, private):
            await self.admitted(row)
        stop = self.event('/stop')
        await self.guard.guard(stop)
        await asyncio.gather(first.task, return_exceptions=True)
        self.assertTrue(first.task.cancelled())
        self.assertFalse(other.task.done())
        self.assertFalse(private.task.done())
        self.assertEqual(self.guard._request_status()['active_count'], 2)

    async def test_one_group_timeout_does_not_cancel_another_group_or_private_chat(self):
        first = self.start(self.event())
        other = self.start(self.event(group='beta'))
        private = self.start(self.event('你好', group=''))
        for row in (first, other, private):
            await self.admitted(row)
        self.guard._set_phase(first.event, 'inference', 0.04)
        watchdog = self.guard._request(first.event).watchdog
        await asyncio.wait_for(asyncio.gather(first.task, return_exceptions=True), 1)
        await asyncio.wait_for(watchdog, 1)
        self.assertTrue(first.event.get_extra('qqbot_total_timeout'))
        self.assertFalse(other.task.done())
        self.assertFalse(private.task.done())

    async def test_same_group_history_tail_preserves_order_without_blocking_another_group(self):
        first = self.start(self.event(sender='a'), hold_commit=True)
        await self.admitted(first)
        second = self.start(self.event(sender='b'))
        await self.pending(1)
        first.finish.set()
        await asyncio.wait_for(first.committing.wait(), 1)
        self.assertFalse(second.ready.is_set())
        other = self.start(self.event(group='beta'))
        await self.admitted(other)
        first.commit_release.set()
        await asyncio.wait_for(first.task, 1)
        await self.admitted(second)
        self.assertFalse(other.task.done())

    async def test_status_reports_three_global_positions_and_one_per_scope(self):
        first = self.start(self.event())
        await self.admitted(first)
        status = self.guard._request_status()
        self.assertEqual(status['max_concurrent'], 3)
        self.assertEqual(status['max_per_scope'], 1)
        self.assertEqual(status['isolation'], 'group_and_private')
        self.assertNotIn('alpha', json.dumps(status))
