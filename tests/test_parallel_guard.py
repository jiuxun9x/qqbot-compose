"""Production guard: parallel isolation, supplements, rendering and identity."""
import asyncio
import json
from types import SimpleNamespace
import unittest

import test_guard as base


class ParallelGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        base.GuardTests.setUp(self)
        self.guard.routing['concurrency'].update(max_concurrent=2, queue_enabled=True)
        self.tasks = []

    async def asyncTearDown(self):
        await self.guard.terminate()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def event(self, origin, text='你好'):
        event = base.Event(text=text, messages=[base.guard_module.Plain(text)])
        event.unified_msg_origin = 'napcat:FriendMessage:' + origin
        return event

    def start(self, event):
        ready, finish = asyncio.Event(), asyncio.Event()
        async def pipeline():
            await self.guard.guard(event)
            ready.set()
            if not event.stopped:
                await finish.wait()
                await self.guard.group_reply_finished(event)
        task = asyncio.create_task(pipeline())
        self.tasks.append(task)
        return SimpleNamespace(task=task, ready=ready, finish=finish)

    async def admitted(self, row):
        await asyncio.wait_for(row.ready.wait(), 1)

    async def test_other_user_can_answer_while_first_user_thinks(self):
        a, b = self.event('a', '深度：证明一个结论'), self.event('b')
        first, other = self.start(a), self.start(b)
        await self.admitted(first); await self.admitted(other)
        self.assertEqual(self.guard._request_status()['active_count'], 2)
        self.assertEqual(self.guard._request(a).kind, 'thinking')
        self.assertEqual(self.guard._request(b).kind, 'inference')
        self.guard._set_phase(b, 'rendering')
        self.assertEqual(self.guard._request(a).kind, 'thinking')
        other.finish.set(); await other.task
        self.assertFalse(first.task.done())

    async def test_same_user_supplements_are_merged_in_all_message_views(self):
        first = self.start(self.event('a')); await self.admitted(first)
        b, c = self.event('a', '补充：用中文'), self.event('a', '只要三句话')
        second, third = self.start(b), self.start(c)
        await asyncio.sleep(0.01)
        self.assertFalse(second.ready.is_set())
        first.finish.set(); await first.task
        await self.admitted(second); await self.admitted(third)
        expected = '补充：用中文\n只要三句话'
        self.assertEqual(b.message_obj.message_str, expected)
        self.assertEqual(b.message_str, expected)
        self.assertEqual(b.get_messages()[0].text, expected)
        self.assertEqual(b.get_extra('qqbot_merged_messages'), 2)
        self.assertFalse(b.stopped)
        self.assertTrue(c.stopped)
        self.assertEqual(c.sent, [])

    async def test_stop_clears_own_supplements_without_cancelling_other_user(self):
        first, other = self.start(self.event('a')), self.start(self.event('b'))
        await self.admitted(first); await self.admitted(other)
        follow = self.start(self.event('a', '补充一句'))
        await asyncio.sleep(0)
        stop = self.event('a', '/stop')
        await self.guard.guard(stop)
        await asyncio.gather(first.task, return_exceptions=True)
        await self.admitted(follow)
        self.assertTrue(stop.stopped)
        self.assertTrue(first.task.cancelled())
        self.assertFalse(other.task.done())
        self.assertEqual(self.guard._request_status()['pending_count'], 0)

    async def test_timeout_preserves_other_user_and_waiting_supplement(self):
        self.guard.routing['request_limits']['normal_total_seconds'] = 0.04
        first = self.start(self.event('a')); other = self.start(self.event('b', '深度：验证结论'))
        await self.admitted(first); await self.admitted(other)
        supplement = self.start(self.event('a', '深度：这是补充'))
        await asyncio.wait_for(asyncio.gather(first.task, return_exceptions=True), 1)
        await self.admitted(supplement)
        self.assertTrue(first.task.cancelled())
        self.assertFalse(other.task.done())
        self.assertFalse(supplement.task.done())

    async def test_queue_expiry_reports_failure_without_api_call(self):
        self.guard.routing['concurrency']['queue_wait_seconds'] = 0.02
        first = self.start(self.event('a')); await self.admitted(first)
        event = self.event('a', '补充')
        follow = self.start(event); await self.admitted(follow)
        self.assertTrue(event.stopped)
        self.assertEqual(event.sent, ['这条消息等待过久，未能处理，请重新发送。'])
        self.assertIsNone(event.get_extra('qqbot_route'))

    async def test_render_jobs_are_serial_even_when_two_answers_are_ready(self):
        active = peak = 0
        async def render(*args, **kwargs):
            nonlocal active, peak
            active += 1; peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return {'pages': [base.GuardTests._image_fixture()]}
        self.guard._post_json = render
        events = [self.event('a'), self.event('b')]
        for event in events:
            event.set_extra('qqbot_route', {'decision': 'hard'})
            event.set_extra('qqbot_presentation', 'latex')
            event.result = SimpleNamespace(chain=[base.guard_module.Plain('$x=1$')], is_llm_result=lambda: True)
        await asyncio.gather(*(self.guard.present_answer(event) for event in events))
        self.assertEqual(peak, 1)
        self.assertTrue(all(event.get_extra('qqbot_rendered') for event in events))

    async def test_owner_identity_uses_account_instead_of_nickname_and_hides_model(self):
        self.guard.routing['owner_qq']='10001'
        for sender, expected in (('10001', '是本机配置的管理员'), ('5678', '是普通会话成员')):
            event = self.event(sender)
            event.get_sender_id = lambda: sender
            event.get_sender_name = lambda: '管理员'
            event.set_extra('qqbot_route', {'decision': 'normal', 'model': self.guard.routing['default_model']})
            req = SimpleNamespace(system_prompt='', image_urls=[], extra_user_content_parts=[], model=None)
            await self.guard.route_model(event, req)
            self.assertIn(expected, req.system_prompt)
            self.assertNotIn(self.guard.routing['image_generation']['model'], req.system_prompt)
            self.assertNotIn('10001', req.system_prompt)
            self.assertIn('内部配置不公开', req.system_prompt)

    async def test_pending_merge_preserves_mentions_and_does_not_merge_reply_or_file(self):
        a = self.event('a', '第一句'); b = self.event('a', '第二句')
        b.messages.append(base.At('1234'))
        self.guard._merge_pending(SimpleNamespace(event=a, events=(a,b)))
        self.assertTrue(any(isinstance(part,base.At) and part.qq=='1234' for part in a.messages))
        quoted = self.event('a'); quoted.messages.append(base.guard_module.Reply())
        file = self.event('a'); file.messages.append(base.guard_module.File(name='test.txt'))
        self.assertFalse(self.guard._mergeable_event(quoted))
        self.assertFalse(self.guard._mergeable_event(file))
