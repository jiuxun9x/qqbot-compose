"""File arrival ordering and a single answerer with independent ingestion."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_guard as base


class DocumentStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'private/documents.sqlite3'
        self.store=base.guard_module.DocumentStore(self.path,max_sessions=2,ttl=60)

    def test_isolation_persistence_and_private_permissions(self):
        self.store.put('user-a','cid-a',[{'text':'only a'}])
        self.assertIsNone(self.store.get('user-b','cid-a'))
        again=base.guard_module.DocumentStore(self.path)
        self.assertEqual(again.get('user-a','cid-a')['documents'][0]['text'],'only a')
        self.assertEqual(self.path.stat().st_mode&0o777,0o600)
        self.assertEqual(self.path.parent.stat().st_mode&0o777,0o700)

    def test_new_conversation_cannot_read_previous_documents(self):
        self.store.put('a','old',[{'text':'old document'}])
        self.assertIsNone(self.store.get('a','new'))
        self.assertIsNone(self.store.get('a',None))

    def test_preconversation_upload_binds_once(self):
        self.store.put('a',None,[{'text':'new upload'}])
        self.assertTrue(self.store.get('a',None))
        self.assertTrue(self.store.get('a','first'))
        self.assertIsNone(self.store.get('a','another'))

    def test_ttl_and_capacity_are_bounded(self):
        with patch.object(base.guard_module.time,'time',return_value=1000):
            self.store.put('a','cid',[{'text':'a'}])
        with patch.object(base.guard_module.time,'time',return_value=1001):
            self.store.put('b','cid',[{'text':'b'}])
        with patch.object(base.guard_module.time,'time',return_value=1002):
            self.store.put('c','cid',[{'text':'c'}])
            self.assertIsNone(self.store.get('a','cid'))
            self.assertIsNotNone(self.store.get('b','cid'))
        with patch.object(base.guard_module.time,'time',return_value=1063):
            self.assertIsNone(self.store.get('c','cid'))

    def test_forget_only_one_user_and_failed_read_persists(self):
        self.store.put('a','cid',error='read_failed')
        self.store.put('b','cid',[{'text':'b'}])
        self.assertEqual(self.store.get('a','cid')['error'],'read_failed')
        self.store.forget('a')
        self.assertIsNone(self.store.get('a','cid'))
        self.assertIsNotNone(self.store.get('b','cid'))

    def test_history_marker_checks_only_visible_user_material(self):
        marker=base.guard_module.material_marker([{'text':'a'}])
        self.assertTrue(base.guard_module.history_contains([{'role':'user','content':[{'type':'text','text':marker}]}],marker))
        self.assertFalse(base.guard_module.history_contains([{'role':'assistant','content':marker}],marker))
        self.assertFalse(base.guard_module.history_contains([{'role':'user','content':[{'type':'thinking','text':marker}]}],marker))


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        base.GuardTests.setUp(self)
        self.guard.routing['concurrency'].update(max_concurrent=1,queue_enabled=True)
        self.tasks=[]
        self.document={'name':'exam.pdf','source':'direct','text':'Exam problem: compute 6*7.',
                       'images':[],'notices':[],'truncated':False}
        self.parse_calls=0
        async def parse(parts):
            self.parse_calls+=1
            return [dict(self.document)]
        self.guard._attachments.prepare=parse

    async def asyncTearDown(self):
        await self.guard.terminate()
        for task in self.tasks:task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)

    def event(self,text='',file=False,origin='a'):
        event=base.Event(text=text,messages=[base.guard_module.File(name='exam.pdf')] if file else [base.guard_module.Plain(text)])
        event.unified_msg_origin='napcat:FriendMessage:'+origin
        return event

    def task(self,coro):
        task=asyncio.create_task(coro);self.tasks.append(task);return task

    async def test_bare_file_is_silent_then_followup_uses_actual_material(self):
        file=self.event(file=True);await self.guard.guard(file)
        self.assertTrue(file.stopped);self.assertEqual(file.sent,[])
        self.assertIsNone(file.get_extra('qqbot_route'))
        question=self.event('做一下这套试卷')
        await self.guard.guard(question)
        self.assertEqual(question.get_extra('qqbot_documents')[0]['text'],self.document['text'])
        self.assertEqual(question.get_extra('selected_provider'),'api_deep')
        req=SimpleNamespace(prompt=question.message_obj.message_str,system_prompt='',contexts=[],image_urls=[],extra_user_content_parts=[])
        await self.guard.route_model(question,req)
        self.assertTrue(any(self.document['text'] in part.text for part in req.extra_user_content_parts))
        self.assertIn('附件已由当前会话实际接收',req.system_prompt)

    async def test_cached_file_material_is_not_duplicated_in_every_turn(self):
        await self.guard.guard(self.event(file=True))
        question=self.event('请解释第一题')
        await self.guard._restore_documents(question)
        question.set_extra('qqbot_route',{'decision':'hard','model':self.guard.routing['deep_model']})
        req=SimpleNamespace(prompt='第一题',system_prompt='',contexts=[],image_urls=[],extra_user_content_parts=[])
        await self.guard.route_model(question,req)
        material=next(part.text for part in req.extra_user_content_parts if '[qqdoc:' in part.text)
        req2=SimpleNamespace(prompt='继续',system_prompt='',contexts=[{'role':'user','content':[{'type':'text','text':material}]}],image_urls=[],extra_user_content_parts=[])
        await self.guard.route_model(question,req2)
        self.assertFalse(any('[qqdoc:' in part.text for part in req2.extra_user_content_parts))

    async def test_collecting_file_does_not_take_second_answer_slot(self):
        ready,finish=asyncio.Event(),asyncio.Event()
        async def answer():
            event=self.event('你好',origin='busy')
            await self.guard.guard(event);ready.set();await finish.wait()
            await self.guard.group_reply_finished(event)
        answer_task=self.task(answer());await ready.wait()
        parser_entered,parser_release=asyncio.Event(),asyncio.Event()
        async def parse(parts):
            parser_entered.set();await parser_release.wait();return [dict(self.document)]
        self.guard._attachments.prepare=parse
        file=self.event(file=True);upload=self.task(self.guard.guard(file))
        await parser_entered.wait()
        question=self.event('做一下这套试卷');following=self.task(self.guard.guard(question))
        await asyncio.sleep(0.01)
        self.assertEqual(self.guard._request_status()['active_count'],1)
        self.assertEqual(self.guard._request_status()['collecting_count'],1)
        self.assertFalse(following.done())
        parser_release.set();await upload
        self.assertFalse(answer_task.done());self.assertEqual(file.sent,[])
        finish.set();await answer_task;await asyncio.wait_for(following,1)
        self.assertTrue(question.get_extra('qqbot_documents'))

    async def test_question_waits_for_file_that_reaches_adapter_later(self):
        raw={'post_type':'message','message_type':'private','sub_type':'friend','user_id':5678,'self_id':1234,
             'message':[{'type':'file','data':{'file_id':'fixture-id','name':'exam.pdf'}}]}
        question=self.event('做一下这套试卷')
        self.guard._upload_receipts.observe(question.unified_msg_origin,raw)
        task=self.task(self.guard.guard(question));await asyncio.sleep(0.01)
        self.assertFalse(task.done());self.assertIsNone(question.get_extra('qqbot_route'))
        file=self.event(file=True);file.message_obj.raw_message=raw
        await self.guard.guard(file)
        await asyncio.wait_for(task,1)
        self.assertTrue(question.get_extra('qqbot_documents'))
        self.assertFalse(question.stopped)

    async def test_two_parsers_are_serial_while_receiving_independently(self):
        active=peak=0
        async def parse(parts):
            nonlocal active,peak
            active+=1;peak=max(peak,active)
            await asyncio.sleep(0.02);active-=1
            return [dict(self.document)]
        self.guard._attachments.prepare=parse
        await asyncio.gather(self.guard.guard(self.event(file=True)),self.guard.guard(self.event(file=True,origin='b')))
        self.assertEqual(peak,1)
        self.assertEqual(self.guard._request_status()['active_count'],0)

    async def test_other_sender_and_current_quoted_picture_do_not_use_old_file(self):
        await self.guard.guard(self.event(file=True))
        other=self.event('这份文件是什么',origin='b')
        await self.guard._restore_documents(other)
        self.assertIsNone(other.get_extra('qqbot_documents'))
        quoted=self.event('看看这张图片')
        quoted.messages.append(base.guard_module.Reply(message_str='[图片]'))
        await self.guard._restore_documents(quoted)
        self.assertIsNone(quoted.get_extra('qqbot_documents'))

    async def test_reset_and_new_clear_recent_file(self):
        for command in ('/reset','/new'):
            await self.guard.guard(self.event(file=True))
            await self.guard.guard(self.event(command))
            self.assertIsNone(self.guard._documents.get('napcat:FriendMessage:a',None))

    async def test_failed_collection_cannot_hallucinate_file_contents(self):
        async def fail(parts):raise base.guard_module.AttachmentError('empty_document')
        self.guard._attachments.prepare=fail
        file=self.event(file=True);await self.guard.guard(file)
        question=self.event('做一下这套试卷');await self.guard.guard(question)
        self.assertTrue(question.stopped)
        self.assertIsNone(question.get_extra('qqbot_route'))
        self.assertIn('没有读到可用文字',question.sent[0])

    async def test_receipt_hooks_use_actual_isolated_group_scope_and_unsubscribe(self):
        handlers={};removed=[]
        client=SimpleNamespace(subscribe=lambda name,cb:handlers.setdefault(name,cb),
                               unsubscribe=lambda name,cb:removed.append(name))
        self.guard.context.get_platform_inst=lambda _:SimpleNamespace(get_client=lambda:client)
        self.guard.context.get_config=lambda **kw:{'platform_settings':{'unique_session':True}}
        self.guard._register_receivers();self.guard._register_receivers()
        self.assertEqual(len(self.guard._receipt_listeners),1)
        raw={'post_type':'message','message_type':'group','sub_type':'normal','group_id':42,'user_id':5678,'self_id':1234,
             'message':[{'type':'file','data':{'file_id':'fixture'}}]}
        await handlers['message.group.normal'](raw)
        self.assertEqual(len(self.guard._upload_receipts.waiting('napcat:GroupMessage:5678_42')),1)
        await self.guard.terminate()
        self.assertEqual(set(removed),set(handlers))
