"""Voice admission, identity, clean history, and QQ group reply policy."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest

import test_guard as base


class VoiceGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        base.GuardTests.setUp(self)
        self.calls=[];self.text='你好呀'
        async def prepare(event,record,max_seconds):
            self.calls.append(('prepare',record))
            return {'path':'/fixture/voice.wav','job':'/fixture','duration_seconds':2}
        async def transcribe(audio,config):self.calls.append(('transcribe',None));return self.text
        self.guard._voice.prepare=prepare;self.guard._voice.transcribe=transcribe
        self.guard._voice.cleanup=lambda audio:self.calls.append(('cleanup',None))

    async def asyncTearDown(self):await self.guard.terminate()

    def event(self,group=False,text='',at=False,quote=False):
        record=base.guard_module.Record(file='opaque.amr')
        parts=[base.At('1234')] if at else []
        parts.append(base.guard_module.Reply(id='quoted',sender_id='999',sender_nickname='别人',chain=[record]) if quote else record)
        event=base.Event(private=not group,text=text,messages=parts)
        event.message_obj.message_id='fixture-message'
        return event

    async def test_private_bare_voice_becomes_text_and_normal_pipeline_continues(self):
        event=self.event();await self.guard.guard(event)
        self.assertFalse(event.stopped)
        self.assertIn('你好呀',event.message_obj.message_str)
        self.assertFalse(self.guard._voice_parts(event))
        self.assertTrue(any(isinstance(part,base.guard_module.Plain) for part in event.get_messages()))
        self.assertEqual(self.calls[-1][0],'cleanup')

    async def test_group_only_at_mode_skips_voice_recognition_without_real_at(self):
        event=self.event(group=True);self.guard._group_modes.set('napcat:group1',False)
        await self.guard.guard(event)
        self.assertTrue(event.stopped);self.assertEqual(self.calls,[])

    async def test_quoted_voice_preserves_author_and_current_question(self):
        event=self.event(group=True,text='他说的什么？',at=True,quote=True)
        await self.guard.guard(event)
        self.assertFalse(event.stopped);self.assertEqual(event.message_obj.message_str,'他说的什么？')
        info=self.guard._quote_info(event)
        self.assertIn('你好呀',info['text']);self.assertFalse(info['same_sender'])
        self.assertEqual(event.get_extra('qqbot_voice_transcript')['source'],'quoted')

    async def test_bare_group_voice_follows_existing_probability(self):
        event=self.event(group=True);await self.guard.guard(event)
        self.assertTrue(self.calls);self.assertTrue(event.stopped);self.assertEqual(event.sent,[])

    async def test_spoken_name_is_a_direct_group_call_after_recognition(self):
        self.text='小助手你好'
        event=self.event(group=True);await self.guard.guard(event)
        self.assertFalse(event.stopped);self.assertTrue(event.get_extra('qqbot_voice_named_call'))

    async def test_transcript_slash_is_not_a_management_command(self):
        self.text='/reset';event=self.event();await self.guard.guard(event)
        self.assertFalse(event.stopped)
        self.assertTrue(event.message_obj.message_str.startswith('语音内容：'))
        self.assertEqual(event.sent,[])

    async def test_silent_voice_does_not_reach_classifier_or_answer(self):
        async def prepare(*args):raise base.guard_module.VoiceError('voice_silent')
        self.guard._voice.prepare=prepare
        event=self.event();await self.guard.guard(event)
        self.assertTrue(event.stopped);self.assertIsNone(event.get_extra('qqbot_route'))
        self.assertTrue(any('没有听清' in value for value in event.sent))
        self.assertIsNone(self.guard._gate.for_event(event))

    async def test_failed_unaddressed_group_voice_is_silent(self):
        async def prepare(*args):raise base.guard_module.VoiceError('voice_decode_failed')
        self.guard._voice.prepare=prepare
        event=self.event(group=True);await self.guard.guard(event)
        self.assertTrue(event.stopped);self.assertEqual(event.sent,[])

    async def test_raw_audio_and_path_are_removed_from_model_request(self):
        event=self.event();await self.guard.guard(event)
        req=SimpleNamespace(prompt=event.message_obj.message_str,system_prompt='',contexts=[],image_urls=[],audio_urls=['/fixture/voice.wav'],
            extra_user_content_parts=[base.guard_module.TextPart(text='[Audio Attachment: path /fixture/voice.wav]')])
        await self.guard.route_model(event,req)
        self.assertEqual(req.audio_urls,[])
        self.assertFalse(any('/fixture/voice.wav' in part.text for part in req.extra_user_content_parts))

    async def test_same_group_busy_voice_does_not_add_unaddressed_tasks(self):
        first=base.Event(private=False,text='小助手你好',messages=[base.At('1234')])
        entered=asyncio.Event();hold=asyncio.Event()
        async def active():await self.guard.guard(first);entered.set();await hold.wait()
        task=asyncio.create_task(active());await entered.wait()
        try:
            event=self.event(group=True);await self.guard.guard(event)
            self.assertTrue(event.stopped);self.assertEqual(self.calls,[])
        finally:task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_multiple_voice_parts_fail_without_guessing(self):
        event=self.event();event.messages.append(base.guard_module.Record(file='another.amr'))
        await self.guard.guard(event)
        self.assertTrue(event.stopped);self.assertEqual(self.calls,[])
        self.assertTrue(any('一段语音' in text for text in event.sent))
