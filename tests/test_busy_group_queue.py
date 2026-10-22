"""A busy bot answers direct group calls, without queuing ambient chatter."""
import asyncio
import json
from types import SimpleNamespace
import unittest

import test_guard as base


class BusyGroupQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        base.GuardTests.setUp(self)
        self.guard.routing['concurrency'].update(max_concurrent=1, queue_enabled=True)
        self.guard.routing['group_reply'].update(probability=0.05, cooldown_seconds=5)
        self.guard._random = lambda: 0
        self.tasks = []
        self.calls = []
        self.classifier_entered = asyncio.Event()
        self.classifier_release = asyncio.Event()
        self.hold_classifier = False

        async def classify(**kwargs):
            body = json.loads(kwargs['prompt'])
            self.calls.append(body)
            engagement = body.get('engagement') or {}
            if self.hold_classifier and body['question'] == '一个适合闲聊的话题':
                self.classifier_entered.set()
                await self.classifier_release.wait()
            return SimpleNamespace(completion_text=json.dumps({
                'intent': 'chat', 'difficulty': 'normal', 'prompt': '',
                'addressed_to_bot': bool(engagement.get('bot_mentioned') or engagement.get('named_prefix')),
                'should_reply': '先别回' not in body['question'],
            }))
        self.guard.context.get_provider_by_id = lambda _: SimpleNamespace(text_chat=classify)

    async def asyncTearDown(self):
        await self.guard.terminate()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def event(self, text, sender='a', private=False, parts=()):
        event = base.Event(private=private, text=text,
                           messages=list(parts) + [base.guard_module.Plain(text)])
        event.unified_msg_origin = 'napcat:' + ('FriendMessage:' + sender if private else 'GroupMessage:' + sender + '_group1')
        event.get_sender_id = lambda: sender
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

    async def pending(self, count):
        async def wait():
            while self.guard._gate.pending_count != count:
                await asyncio.sleep(0.005)
        await asyncio.wait_for(wait(), 1)

    async def test_busy_same_sender_chatter_is_background_without_queue_or_classifier(self):
        first = self.start(self.event('小助手你好'))
        await self.admitted(first)
        count = len(self.calls)
        for index in range(12):
            event = self.event('我和小王聊的第' + str(index) + '句')
            await asyncio.wait_for(self.guard.guard(event), 0.2)
            self.assertTrue(event.stopped)
            self.assertEqual(event.sent, [])
            if index:
                self.assertTrue(any('我和小王聊的第' + str(index - 1) + '句' in str(item)
                                    for item in event.get_extra('qqbot_group_context')))
        background = await self.guard._record_group(event, '测试成员', '[检查后续背景]')
        self.assertTrue(all(any('我和小王聊的第' + str(index) + '句' in str(item) for item in background)
                            for index in range(12)))
        self.assertEqual(len(self.calls), count)
        self.assertEqual(self.guard._gate.pending_count, 0)
        self.assertEqual(len(self.guard._gate.active), 1)

    async def test_busy_other_sender_chatter_is_silent(self):
        first = self.start(self.event('你好', private=True))
        await self.admitted(first)
        event = self.event('这个话题也太有意思了', sender='b')
        await self.guard.guard(event)
        self.assertTrue(event.stopped)
        self.assertEqual(event.sent, [])
        self.assertEqual(self.calls, [])

    async def test_real_mention_queues_and_answers_once(self):
        first = self.start(self.event('你好', private=True))
        await self.admitted(first)
        event = self.event('解释一下刚才的结论', sender='b', parts=[base.At('1234')])
        following = self.start(event)
        await self.pending(1)
        self.assertEqual(self.guard._gate.pending_count, 1)
        self.assertFalse(following.ready.is_set())
        first.finish.set()
        await first.task
        await self.admitted(following)
        self.assertFalse(event.stopped)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(event.get_extra('qqbot_group_spontaneous'))

    async def test_name_call_queues_but_same_waiter_chatter_does_not(self):
        first = self.start(self.event('你好', private=True))
        await self.admitted(first)
        event = self.event('小助手说说你的看法', sender='b')
        following = self.start(event)
        await self.pending(1)
        chatter = self.event('小王你看到了吗', sender='b')
        await asyncio.wait_for(self.guard.guard(chatter), 0.2)
        self.assertTrue(chatter.stopped)
        self.assertEqual(chatter.sent, [])
        self.assertEqual(self.guard._gate.pending_count, 1)
        first.finish.set()
        await first.task
        await self.admitted(following)
        self.assertFalse(event.stopped)
        self.assertNotIn('小王你看到了吗', event.message_obj.message_str)
        self.assertEqual(len(self.calls), 1)

    async def test_busy_quote_of_bot_name_is_not_a_direct_call(self):
        first = self.start(self.event('你好', private=True))
        await self.admitted(first)
        event = self.event('这句话真有趣', parts=[base.guard_module.Reply(message_str='小助手解释一下')])
        await self.guard.guard(event)
        self.assertTrue(event.stopped)
        self.assertEqual(self.guard._gate.pending_count, 0)
        self.assertEqual(self.calls, [])

    async def test_busy_mentions_to_other_member_or_all_do_not_queue(self):
        first = self.start(self.event('你好', private=True))
        await self.admitted(first)
        for member in ('another-member', 'all'):
            event = self.event('大家看一下', parts=[base.At(member)])
            await self.guard.guard(event)
            self.assertTrue(event.stopped)
            self.assertEqual(event.sent, [])
        self.assertEqual(self.guard._gate.pending_count, 0)
        self.assertEqual(self.calls, [])

    async def test_classifier_already_running_skips_ambient_answer_when_direct_call_waits(self):
        self.hold_classifier = True
        event = self.event('一个适合闲聊的话题')
        ambient = self.start(event)
        await asyncio.wait_for(self.classifier_entered.wait(), 1)
        direct_event = self.event('现在回答我', sender='b', parts=[base.At('1234')])
        direct = self.start(direct_event)
        await self.pending(1)
        self.assertEqual(self.guard._gate.pending_count, 1)
        self.classifier_release.set()
        await asyncio.wait_for(ambient.task, 1)
        await self.admitted(direct)
        self.assertTrue(event.stopped)
        self.assertNotIn('qqbot_route', event.extras)
        self.assertFalse(direct_event.stopped)
        self.assertEqual(event.sent, [])

    async def test_idle_ambient_still_uses_five_percent_chance(self):
        event = self.event('一个适合闲聊的话题')
        row = self.start(event)
        await self.admitted(row)
        self.assertFalse(event.stopped)
        self.assertTrue(event.get_extra('qqbot_group_spontaneous'))
        row.finish.set()
        await row.task
        self.guard._group_last_reply.clear()
        self.guard._random = lambda: 0.051
        missed = self.event('另一个适合闲聊的话题')
        await self.guard.guard(missed)
        self.assertTrue(missed.stopped)

    async def test_private_supplements_still_queue_and_merge(self):
        first = self.start(self.event('你好', private=True))
        await self.admitted(first)
        second_event = self.event('补充一下', private=True)
        second = self.start(second_event)
        third_event = self.event('只说三句话', private=True)
        third = self.start(third_event)
        await self.pending(2)
        self.assertEqual(self.guard._gate.pending_count, 2)
        first.finish.set()
        await first.task
        await self.admitted(second)
        await self.admitted(third)
        self.assertEqual(second_event.message_obj.message_str, '补充一下\n只说三句话')
        self.assertTrue(third_event.stopped)

    async def test_busy_status_command_stays_usable_without_queuing(self):
        first = self.start(self.event('你好', private=True))
        await self.admitted(first)
        event = self.event('/状态')
        await self.guard.guard(event)
        self.assertTrue(event.stopped)
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(self.guard._gate.pending_count, 0)
        self.assertFalse(first.task.done())

    async def test_at_only_group_does_not_queue_name_call(self):
        await asyncio.to_thread(self.guard._group_modes.set, 'napcat:group1', False)
        first = self.start(self.event('你好', private=True))
        await self.admitted(first)
        event = self.event('小助手请回答')
        await self.guard.guard(event)
        self.assertTrue(event.stopped)
        self.assertEqual(self.guard._gate.pending_count, 0)
        self.assertEqual(self.calls, [])

    async def test_queued_direct_call_still_respects_do_not_reply(self):
        first = self.start(self.event('你好', private=True))
        await self.admitted(first)
        event = self.event('小助手先别回')
        following = self.start(event)
        await self.pending(1)
        self.assertEqual(self.guard._gate.pending_count, 1)
        first.finish.set()
        await first.task
        await asyncio.wait_for(following.task, 1)
        self.assertTrue(event.stopped)
        self.assertEqual(event.sent, [])
        self.assertNotIn('qqbot_route', event.extras)
