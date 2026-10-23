"""Failed uploads cannot poison later chat or replay the same notice."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest

import test_guard as base


class ErrorNoticeStoreTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=base.guard_module.DocumentStore(Path(self.temp.name)/'documents.sqlite3')

    def test_notice_claim_is_persistent_and_only_once(self):
        self.store.put('one','cid',error='unsupported_type',error_id='file-one')
        self.assertTrue(self.store.claim_error_notice('one','cid','unsupported_type','file-one'))
        again=base.guard_module.DocumentStore(self.store.path)
        self.assertFalse(again.claim_error_notice('one','cid','unsupported_type','file-one'))
        self.assertTrue(again.get('one','cid')['error_notified'])
        self.assertEqual(again.get('one','cid')['error'],'unsupported_type')

    def test_concurrent_claims_cannot_repeat_the_notice(self):
        self.store.put('one','cid',error='unsupported_type',error_id='file-one')
        with ThreadPoolExecutor(max_workers=6) as workers:
            result=list(workers.map(lambda _:self.store.claim_error_notice('one','cid','unsupported_type','file-one'),range(15)))
        self.assertEqual(sum(result),1)

    def test_duplicate_transport_event_preserves_claim_but_new_upload_can_report(self):
        self.store.put('one','cid',error='unsupported_type',error_id='file-one')
        self.assertTrue(self.store.claim_error_notice('one','cid','unsupported_type','file-one'))
        self.store.put('one','cid',error='unsupported_type',error_id='file-one')
        self.assertFalse(self.store.claim_error_notice('one','cid','unsupported_type','file-one'))
        self.store.put('one','cid',error='unsupported_type',error_id='file-two')
        self.assertFalse(self.store.claim_error_notice('one','cid','unsupported_type','file-one'))
        self.assertTrue(self.store.claim_error_notice('one','cid','unsupported_type','file-two'))

    def test_other_user_or_conversation_cannot_acknowledge_an_error(self):
        self.store.put('one','cid',error='read_failed',error_id='file')
        self.assertFalse(self.store.claim_error_notice('two','cid','read_failed','file'))
        self.assertFalse(self.store.claim_error_notice('one','other','read_failed','file'))
        self.assertFalse(self.store.claim_error_notice('one','cid','unsupported_type','file'))
        self.assertTrue(self.store.claim_error_notice('one','cid','read_failed','file'))

    def test_successful_replacement_cannot_be_acknowledged_as_an_old_error(self):
        self.store.put('one','cid',error='unsupported_type',error_id='file')
        self.store.put('one','cid',[{'text':'real document'}])
        self.assertFalse(self.store.claim_error_notice('one','cid','unsupported_type','file'))
        self.assertEqual(self.store.get('one','cid')['documents'],[{'text':'real document'}])

    def test_legacy_errors_are_not_replayed_and_expiry_is_not_extended(self):
        self.store.put('one',None,error='unsupported_type')
        with sqlite3.connect(self.store.path) as db:
            stamp=db.execute('SELECT updated FROM documents').fetchone()[0]
            db.execute('UPDATE documents SET payload=?',(json.dumps({'documents':[],'error':'unsupported_type'}),))
        self.assertFalse(self.store.claim_error_notice('one',None,'unsupported_type'))
        with sqlite3.connect(self.store.path) as db:self.assertEqual(db.execute('SELECT updated FROM documents').fetchone()[0],stamp)


class AttachmentFailureGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        base.GuardTests.setUp(self)
        async def unsupported(parts):raise base.guard_module.AttachmentError('unsupported_type')
        self.guard._attachments.prepare=unsupported

    async def asyncTearDown(self):await self.guard.terminate()

    def event(self,text='',file_id=None,origin='one'):
        event=base.Event(text=text,messages=[base.guard_module.File(name='fixture.zip')] if file_id else [base.guard_module.Plain(text)])
        event.unified_msg_origin='napcat:FriendMessage:'+origin
        if file_id:event.message_obj.raw_message={'post_type':'message','message_id':123,'message':[{'type':'file','data':{'file_id':file_id,'name':'fixture.zip'}}]}
        return event

    async def normal(self,text,origin='one'):
        event=self.event(text,origin=origin);await self.guard.guard(event)
        self.assertFalse(event.stopped,text);self.assertEqual(event.sent,[],text)
        self.guard._finish_event(event,history_pending=False)
        return event

    async def test_quiet_failed_upload_does_not_block_ordinary_chat_or_new_work(self):
        upload=self.event(file_id='one');await self.guard.guard(upload)
        self.assertTrue(upload.stopped);self.assertEqual(upload.sent,[])
        for text in ('今天怎么样','讲一个笑话','帮我写一个Python文件','不用读文件了，聊聊别的'):
            event=await self.normal(text)
            self.assertIsNone(event.get_extra('qqbot_document_error'))
        self.assertFalse(self.guard._documents.get(upload.unified_msg_origin,None)['error_notified'])

    async def test_file_followup_reports_once_then_later_chat_remains_answerable(self):
        upload=self.event(file_id='one');await self.guard.guard(upload)
        follow=self.event('读取刚才的压缩包');await self.guard.guard(follow)
        self.assertTrue(follow.stopped);self.assertEqual(len(follow.sent),1)
        self.assertIn('ZIP 请先解压',follow.sent[0])
        await self.normal('今天吃点什么')
        follow=await self.normal('读文件内容')
        self.assertEqual(follow.get_extra('qqbot_document_error'),'unsupported_type')
        req=SimpleNamespace(prompt='读文件内容',system_prompt='',contexts=[],image_urls=[],extra_user_content_parts=[])
        await self.guard.route_model(follow,req)
        self.assertIn('不得声称已经解压',req.system_prompt)
        self.assertIsNone(follow.get_extra('qqbot_documents'))

    async def test_immediate_notice_is_not_repeated_by_followup_or_duplicate_upload(self):
        first=self.event('读取这个压缩包',file_id='one');await self.guard.guard(first)
        self.assertEqual(len(first.sent),1)
        duplicate=self.event('读取这个压缩包',file_id='one');await self.guard.guard(duplicate)
        self.assertTrue(duplicate.stopped);self.assertEqual(duplicate.sent,[])
        await self.normal('再看看文件')
        await self.normal('讲讲二分查找')
        different=self.event('读取这个压缩包',file_id='two');await self.guard.guard(different)
        self.assertEqual(len(different.sent),1)

    async def test_notice_delivery_failure_does_not_recache_or_repeat_the_error(self):
        first=self.event('读取这个压缩包',file_id='one');attempts=[]
        async def fail_send(value):attempts.append(value);raise RuntimeError('fixture delivery failure')
        first.send=fail_send;await self.guard.guard(first)
        self.assertEqual(len(attempts),1)
        self.assertEqual(self.guard._documents.get(first.unified_msg_origin,None)['error'],'unsupported_type')
        duplicate=self.event('读取这个压缩包',file_id='one');duplicate.send=fail_send;await self.guard.guard(duplicate)
        self.assertEqual(len(attempts),1)
        await self.normal('现在能聊天吗')

    async def test_legacy_error_already_in_production_never_replays(self):
        origin=self.event().unified_msg_origin
        self.guard._documents.put(origin,None,error='unsupported_type')
        with sqlite3.connect(self.guard._documents.path) as db:
            db.execute('UPDATE documents SET payload=?',(json.dumps({'documents':[],'error':'unsupported_type'}),))
        await self.normal('说点什么')
        follow=await self.normal('读取刚才的压缩包')
        self.assertEqual(follow.get_extra('qqbot_document_error'),'unsupported_type')

    async def test_new_valid_file_recovers_without_clearing_conversations(self):
        upload=self.event(file_id='one');await self.guard.guard(upload)
        async def valid(parts):return [{'name':'fixture.pdf','source':'direct','text':'real evidence','images':[],'notices':[]}]
        self.guard._attachments.prepare=valid
        upload=self.event(file_id='two');upload.messages[0].name='fixture.pdf';await self.guard.guard(upload)
        event=await self.normal('读取这个文件')
        self.assertEqual(event.get_extra('qqbot_documents')[0]['text'],'real evidence')
        self.assertIsNone(event.get_extra('qqbot_document_error'))

    async def test_failed_batch_and_notice_are_isolated_between_users(self):
        first=self.event('读取这个压缩包',file_id='one');await self.guard.guard(first)
        other=self.event('读取这个压缩包',file_id='one',origin='two');await self.guard.guard(other)
        self.assertEqual(len(first.sent),1);self.assertEqual(len(other.sent),1)
        await self.normal('说点什么',origin='three')

    async def test_zip_is_rejected_before_any_download_attempt(self):
        reader=base.guard_module.AttachmentReader(Path(self.temp.name)/'unsupported')
        calls=[]
        async def forbidden(*args):calls.append(True);raise AssertionError('must not download zip')
        reader._source=forbidden
        with self.assertRaisesRegex(base.guard_module.AttachmentError,'unsupported_type'):
            await reader.prepare([(SimpleNamespace(name='fixture.zip',url='https://example.com/archive.zip'),'direct')])
        self.assertEqual(calls,[])


if __name__=='__main__':unittest.main()
