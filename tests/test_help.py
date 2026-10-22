"""Help remains available while busy and contains no secrets or model IDs."""
import asyncio
import unittest
import test_guard as base


class HelpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):base.GuardTests.setUp(self)
    async def asyncTearDown(self):await self.guard.terminate()

    async def test_help_and_chinese_alias_bypass_models_and_reply_once(self):
        async def forbidden(*args,**kw):raise AssertionError('help must not call any model')
        self.guard._classify_event=forbidden
        for command in ('/help','/帮助'):
            event=base.Event(text=command);await self.guard.guard(event)
            self.assertTrue(event.stopped);self.assertEqual(len(event.sent),1)
            for name in ('/new','/reset','/stop','/状态','/stats','/画图','/搜索','/来源','/文件自动回复','/主动回复'):
                self.assertIn(name,event.sent[0])
            self.assertIsNone(self.guard._gate.for_event(event))

    async def test_help_is_available_in_at_only_group_and_does_not_change_mode(self):
        self.guard._group_modes.set('napcat:group1',False)
        event=base.Event(private=False,text='/help');await self.guard.guard(event)
        self.assertTrue(event.stopped);self.assertEqual(len(event.sent),1)
        self.assertFalse(self.guard._group_modes.get('napcat:group1'))

    async def test_help_during_running_answer_does_not_cancel_or_join_it(self):
        ready=asyncio.Event();hold=asyncio.Event();first=base.Event(text='hello')
        async def answer():
            await self.guard.guard(first);ready.set();await hold.wait()
        task=asyncio.create_task(answer());await ready.wait()
        event=base.Event(text='/help');await self.guard.guard(event)
        self.assertEqual(len(event.sent),1);self.assertFalse(task.done())
        self.assertIsNotNone(self.guard._gate.for_event(first))
        hold.set();await task

    async def test_admin_help_is_selected_by_verified_qq_not_nickname(self):
        self.guard.context.get_config=lambda **kw:{'wake_prefix':['/'],'admins_id':['5678']}
        event=base.Event(text='/help');await self.guard.guard(event)
        self.assertIn('/搜索池',event.sent[0]);self.assertIn('/provider',event.sent[0])
        event=base.Event(text='/help');event.get_sender_id=lambda:'8765';event.get_sender_name=lambda:'管理员'
        await self.guard.guard(event)
        self.assertNotIn('/provider',event.sent[0]);self.assertNotIn('/搜索池',event.sent[0])

    async def test_help_does_not_expose_backend_values_and_uses_current_reply_defaults(self):
        self.guard.routing['group_reply'].update(probability=0.05,cooldown_seconds=5)
        event=base.Event(text='/help');await self.guard.guard(event)
        self.assertIn('5%',event.sent[0]);self.assertIn('5 秒',event.sent[0])
        self.assertNotIn(self.guard.routing['default_model'],event.sent[0])
        self.assertNotIn('http://',event.sent[0]);self.assertNotIn('API_KEY',event.sent[0])


if __name__=='__main__':unittest.main()
