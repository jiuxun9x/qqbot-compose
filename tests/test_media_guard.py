"""File opt-in, nonquoted image follow-ups, and verified speaker metadata."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from PIL import Image
import test_guard as base


class MediaGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        base.GuardTests.setUp(self)
        self.guard.routing['concurrency'].update(max_concurrent=3,queue_enabled=True)
        self.config={'wake_prefix':['/','小助手'],'admins_id':['111']}
        self.guard.context.get_config=lambda **kw:self.config
        self.use_recent=None
        self.calls=[]
        async def classify(**kw):
            body=json.loads(kw['prompt']);self.calls.append(body)
            engagement=body.get('engagement') or {}
            use=self.guard._media_reference_hint(body['question']) if self.use_recent is None else self.use_recent
            return SimpleNamespace(completion_text=json.dumps({'intent':'chat','difficulty':'normal','prompt':'',
                'addressed_to_bot':bool(engagement.get('bot_mentioned') or engagement.get('named_prefix')),
                'should_reply':True,'use_recent_media':bool(body.get('recent_media') and use)}))
        self.guard.context.get_provider_by_id=lambda _:SimpleNamespace(text_chat=classify)
        async def parse(parts):
            return [{'name':'example.txt','source':'direct','text':'文件里有三棵松树。','images':[],'notices':[],'truncated':False}]
        self.guard._attachments.prepare=parse

    async def asyncTearDown(self):
        await self.guard.terminate()

    def event(self,text='',sender='111',group='',parts=()):
        e=base.Event(private=not group,text=text,messages=list(parts)+([base.guard_module.Plain(text)] if text else []))
        e.unified_msg_origin='napcat:'+('GroupMessage:'+sender+'_'+group if group else 'FriendMessage:'+sender)
        e.get_sender_id=lambda:sender;e.get_group_id=lambda:group;e.get_sender_name=lambda:'同名昵称'
        e.message_obj.message_id='message-'+sender+'-'+str(len(self.calls))
        return e

    def request(self,text='这是什么'):
        return SimpleNamespace(prompt=text,system_prompt='',contexts=[],image_urls=[],extra_user_content_parts=[])

    async def cache(self,sender='111',group='g1'):
        e=self.event(sender=sender,group=group)
        folder=self.guard._media.folder/('a'*(31)+str(len(list(self.guard._media.folder.glob('*'))) if self.guard._media.folder.exists() else 0))
        folder.mkdir(mode=0o700,parents=True)
        path=folder/'0.jpg';Image.new('RGB',(20,20),'red').save(path)
        media={'frames':[str(path)],'items':[{'format':'JPEG','animated':False,'total_frames':1,
                'scanned_frames':1,'samples':[{'index':0,'at_ms':0,'path':str(path)}]}]}
        self.guard._media.put(e.unified_msg_origin,self.guard._request_scope(e),None,
                              base.guard_module.message_identity(e),media)
        return e,media

    async def test_file_auto_default_is_off_and_bare_upload_is_silent(self):
        event=self.event(parts=[base.guard_module.File(name='example.txt')])
        await self.guard.guard(event)
        self.assertTrue(event.stopped);self.assertEqual(event.sent,[])
        self.assertEqual(self.calls,[])
        self.assertIsNotNone(self.guard._documents.get(event.unified_msg_origin,None))

    async def test_private_file_auto_opt_in_sends_file_into_answer_pipeline(self):
        command=self.event('/文件自动回复 开')
        await self.guard.guard(command)
        event=self.event(parts=[base.guard_module.File(name='example.txt')])
        await self.guard.guard(event)
        self.assertFalse(event.stopped)
        self.assertTrue(event.get_extra('qqbot_auto_file'))
        req=self.request('')
        await self.guard.route_model(event,req)
        self.assertIn('附件',req.prompt)
        self.assertTrue(any('三棵松树' in part.text for part in req.extra_user_content_parts))

    async def test_file_mode_survives_store_reload_and_is_scoped(self):
        command=self.event('/文件自动回复 开',group='g1')
        await self.guard.guard(command)
        self.guard._file_modes=base.guard_module.GroupReplyModes(self.guard._file_modes.path,default=False)
        self.assertTrue(await self.guard._file_auto_enabled(self.event(group='g1',sender='222')))
        self.assertFalse(await self.guard._file_auto_enabled(self.event(group='g2')))
        self.assertFalse(await self.guard._file_auto_enabled(self.event()))

    async def test_only_admin_can_change_group_file_mode(self):
        event=self.event('/文件自动回复 开',sender='222',group='g1')
        await self.guard.guard(event)
        self.assertIn('管理员',event.sent[0])
        self.assertFalse(await self.guard._file_auto_enabled(event))

    async def test_group_at_only_mode_overrides_auto_file_without_real_mention(self):
        event=self.event(group='g1')
        self.guard._file_modes.set(self.guard._request_scope(event),True)
        self.guard._group_modes.set('napcat:g1',False)
        event.messages=[base.guard_module.File(name='example.txt')]
        await self.guard.guard(event)
        self.assertTrue(event.stopped);self.assertEqual(event.sent,[])
        mentioned=self.event(group='g1',parts=[base.guard_module.File(name='example.txt'),base.At('1234')])
        await self.guard.guard(mentioned)
        self.assertFalse(mentioned.stopped)

    async def test_file_off_still_answers_followup_instruction(self):
        event=self.event(parts=[base.guard_module.File(name='example.txt')])
        await self.guard.guard(event)
        follow=self.event('刚才的文件里有什么？')
        await self.guard.guard(follow)
        self.assertFalse(follow.stopped)
        self.assertIn('三棵松树',str(follow.get_extra('qqbot_documents')))

    async def test_nonquoted_question_attaches_own_recent_image(self):
        original,media=await self.cache()
        event=self.event('这张图里是什么',group='g1',parts=[base.At('1234')])
        await self.guard.guard(event)
        req=self.request()
        await self.guard.route_model(event,req)
        self.assertEqual(req.image_urls,media['frames'])
        self.assertEqual(event.get_extra('qqbot_recent_media_used')['author']['speaker_ref'],
                         base.guard_module.message_identity(original)['speaker_ref'])

    async def test_silent_upload_retains_pixels_after_framework_temp_file_cleanup(self):
        source=Path(self.temp.name)/'incoming.png';Image.new('RGB',(24,24),'red').save(source)
        part=base.guard_module.Image()
        async def resolve():return str(source)
        part.convert_to_file_path=resolve
        upload=self.event(group='g1',parts=[part])
        await self.guard.guard(upload)
        self.assertTrue(upload.stopped)
        source.unlink()
        followup=self.event('这张图是什么',group='g1',parts=[base.At('1234')])
        await self.guard.guard(followup)
        req=self.request();await self.guard.route_model(followup,req)
        self.assertTrue(req.image_urls)
        self.assertTrue(all(Path(path).exists() for path in req.image_urls))

    async def test_unrelated_chat_does_not_attach_recent_image(self):
        await self.cache()
        self.use_recent=False
        event=self.event('今天有点累',group='g1',parts=[base.At('1234')])
        await self.guard.guard(event)
        req=self.request('今天有点累')
        await self.guard.route_model(event,req)
        self.assertEqual(req.image_urls,[])
        self.assertIsNone(event.get_extra('qqbot_recent_media_used'))

    async def test_image_from_another_member_keeps_its_own_author(self):
        original,media=await self.cache(sender='111')
        event=self.event('这张图片是什么',sender='222',group='g1',parts=[base.At('1234')])
        await self.guard.guard(event)
        used=event.get_extra('qqbot_recent_media_used')
        self.assertNotEqual(used['author']['speaker_ref'],base.guard_module.message_identity(event)['speaker_ref'])
        req=self.request();await self.guard.route_model(event,req)
        text='\n'.join(part.text for part in req.extra_user_content_parts)
        self.assertIn(used['author']['speaker_ref'],text)
        self.assertIn(base.guard_module.message_identity(event)['speaker_ref'],text)
        self.assertEqual(req.image_urls,media['frames'])

    async def test_ambiguous_other_members_images_ask_for_a_quote(self):
        await self.cache(sender='222');await self.cache(sender='333')
        event=self.event('这张图片是什么',group='g1',parts=[base.At('1234')])
        await self.guard.guard(event)
        req=self.request();await self.guard.route_model(event,req)
        self.assertTrue(event.stopped)
        self.assertTrue(any('多张图片' in text for text in event.sent))
        self.assertEqual(req.image_urls,[])

    async def test_current_quote_prevents_recent_image_association(self):
        await self.cache()
        event=self.event('解释这段话',group='g1',parts=[base.At('1234'),base.guard_module.Reply(message_str='一段文字')])
        await self.guard._find_recent_media(event,event.message_obj.message_str)
        self.assertIsNone(event.get_extra('qqbot_recent_media_candidate'))

    async def test_group_context_distinguishes_same_nickname_and_bot_reply(self):
        a=self.event('第一人发言',sender='111',group='g1')
        b=self.event('第二人发言',sender='222',group='g1')
        await self.guard._record_group(a,'同名昵称','甲说的话')
        await self.guard._record_group(b,'同名昵称','乙说的话')
        await self.guard._record_group(b,'小助手','机器人回答',bot=True)
        previous=await self.guard._record_group(b,'同名昵称','检查')
        self.assertNotEqual(previous[0]['speaker_ref'],previous[1]['speaker_ref'])
        self.assertFalse(previous[0]['is_bot']);self.assertFalse(previous[1]['is_bot'])
        self.assertTrue(previous[2]['is_bot'])
        self.assertEqual(previous[2]['in_reply_to'],base.guard_module.message_identity(b)['message_ref'])

    async def test_image_reference_followups_are_not_coalesced_across_objects(self):
        await self.cache()
        event=self.event('这张图是什么',group='g1')
        await self.guard._find_recent_media(event,event.message_obj.message_str)
        self.assertFalse(self.guard._mergeable_event(event))

    async def test_queued_question_keeps_image_object_snapshot(self):
        original,media=await self.cache()
        question=self.event('这张图是什么',group='g1')
        self.guard._snapshot_recent_media(question,question.message_obj.message_str)
        self.guard._media.put(original.unified_msg_origin,self.guard._request_scope(original),None,
                              base.guard_module.message_identity(original),error='later-image-failed')
        await self.guard._find_recent_media(question,question.message_obj.message_str)
        self.assertEqual(question.get_extra('qqbot_recent_media_candidate')['media']['frames'],media['frames'])

    async def test_snapshot_cannot_cross_a_changed_conversation(self):
        original,media=await self.cache()
        self.guard._media.put(original.unified_msg_origin,self.guard._request_scope(original),'old-cid',
                              base.guard_module.message_identity(original),media)
        question=self.event('这张图是什么',group='g1')
        self.guard._snapshot_recent_media(question,question.message_obj.message_str)
        async def changed(_):return 'new-cid'
        self.guard.context.conversation_manager=SimpleNamespace(get_curr_conversation_id=changed)
        await self.guard._find_recent_media(question,question.message_obj.message_str)
        self.assertIsNone(question.get_extra('qqbot_recent_media_candidate'))

    async def test_resolving_snapshot_binds_preconversation_upload_once(self):
        original,_=await self.cache()
        question=self.event('这张图是什么',group='g1')
        async def current(_):return 'first-cid'
        self.guard.context.conversation_manager=SimpleNamespace(get_curr_conversation_id=current)
        await self.guard._find_recent_media(question,question.message_obj.message_str)
        self.assertIsNotNone(self.guard._media.candidate(original.unified_msg_origin,self.guard._request_scope(original),'first-cid'))
        self.assertIsNone(self.guard._media.candidate(original.unified_msg_origin,self.guard._request_scope(original),'second-cid'))
