"""Actual animation pixels, bounded media storage and source ownership."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

ROOT=Path(__file__).resolve().parents[1]/'plugins/astrbot_plugin_qqbot_guard'
def load(name):
    spec=importlib.util.spec_from_file_location('media_fixture_'+name,ROOT/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module
worker=load('media_worker')
store_module=load('media_context')
identity=load('identity')


def make_gif(path,count=8):
    frames=[]
    for index in range(count):
        frame=Image.new('RGB',(140,80),'white')
        ImageDraw.Draw(frame).rectangle((5+(index%9)*12,25,25+(index%9)*12,55),fill='red')
        frame.putpixel((0,0),(index%256,(index//256)%256,37))
        frames.append(frame)
    frames[0].save(path,save_all=True,append_images=frames[1:],duration=100,loop=0,disposal=2,optimize=False)


class MediaWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.output=self.root/'output';self.output.mkdir()

    def test_gif_samples_first_and_last_actual_composited_frames(self):
        path=self.root/'motion.gif';make_gif(path)
        result=worker.normalize([str(path)],self.output)
        self.assertEqual(len(result['frames']),6)
        item=result['items'][0]
        self.assertTrue(item['animated'])
        self.assertEqual((item['samples'][0]['index'],item['samples'][-1]['index']),(0,7))
        with Image.open(result['frames'][0]) as first,Image.open(result['frames'][-1]) as last:
            self.assertGreater(first.getpixel((15,40))[0],180)
            self.assertLess(first.getpixel((15,40))[1],60)
            self.assertGreater(last.getpixel((99,40))[0],180)
            self.assertLess(last.getpixel((99,40))[1],60)
            self.assertGreater(last.getpixel((15,40))[1],180)

    def test_two_animations_share_six_frame_budget(self):
        path=self.root/'motion.gif';make_gif(path)
        result=worker.normalize([str(path),str(path)],self.output)
        self.assertEqual(len(result['frames']),6)
        self.assertEqual([len(item['samples']) for item in result['items']],[3,3])

    def test_transparency_becomes_white_and_static_size_is_bounded(self):
        path=self.root/'transparent.png';Image.new('RGBA',(3000,30),(0,0,0,0)).save(path)
        result=worker.normalize([str(path)],self.output)
        self.assertFalse(result['items'][0]['animated'])
        with Image.open(result['frames'][0]) as image:
            self.assertEqual(image.width,2560)
            self.assertEqual(image.getpixel((10,10)),(255,255,255))

    def test_corrupt_and_unsupported_images_are_rejected(self):
        path=self.root/'bad.gif';path.write_bytes(b'not an image')
        with self.assertRaises(Exception):worker.normalize([str(path)],self.output)
        path=self.root/'bitmap.bmp';Image.new('RGB',(10,10)).save(path)
        with self.assertRaisesRegex(ValueError,'unsupported_image'):worker.normalize([str(path)],self.output)

    def test_scan_limit_is_explicit_instead_of_claiming_whole_animation(self):
        path=self.root/'long.gif';make_gif(path,250)
        result=worker.normalize([str(path)],self.output)
        self.assertTrue(result['items'][0]['truncated'])
        self.assertEqual(result['items'][0]['scanned_frames'],240)


class MediaStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.store=store_module.MediaContextStore(self.root/'media')
        self.store.folder.mkdir()
        folder=self.store.folder/('a'*32);folder.mkdir()
        frame=folder/'0.jpg';Image.new('RGB',(10,10),'red').save(frame)
        self.media={'frames':[str(frame)],'items':[{'format':'PNG','animated':False,'total_frames':1,
                    'scanned_frames':1,'samples':[{'index':0,'at_ms':0,'path':str(frame)}]}]}
        self.author={'speaker_ref':'成员-alpha','speaker_name':'同名昵称','is_bot':False,'message_ref':'消息-a'}

    def put(self,origin='user-a',scope='group-a',cid='chat-a'):
        self.store.put(origin,scope,cid,self.author,self.media)

    def test_own_recent_image_has_author_and_stable_message(self):
        self.put()
        row=self.store.candidate('user-a','group-a','chat-a')
        self.assertEqual(row['author']['speaker_ref'],'成员-alpha')
        self.assertEqual(row['author']['message_ref'],'消息-a')

    def test_cross_group_and_cross_private_lookups_are_rejected(self):
        self.put()
        self.assertIsNone(self.store.candidate('user-a','group-b','chat-a'))
        self.assertIsNone(self.store.candidate('user-b','group-a','chat-a',group=False))
        self.assertIsNone(self.store.candidate('user-b','group-b','chat-a',group=True))

    def test_new_conversation_does_not_inherit_old_image(self):
        self.put()
        self.assertIsNone(self.store.candidate('user-a','group-a','chat-new'))

    def test_first_created_conversation_binds_image_once(self):
        self.put(cid=None)
        self.assertIsNotNone(self.store.candidate('user-a','group-a','chat-first'))
        self.assertIsNone(self.store.candidate('user-a','group-a','chat-second'))

    def test_unique_other_member_image_keeps_original_author(self):
        self.put()
        row=self.store.candidate('user-b','group-a','b-chat',group=True)
        self.assertEqual(row['source'],'same_group_recent_image')
        self.assertEqual(row['author'],self.author)

    def test_multiple_other_members_images_are_ambiguous(self):
        self.put()
        self.put('user-c','group-a','chat-c')
        row=self.store.candidate('user-b','group-a','chat-b',group=True)
        self.assertTrue(row['ambiguous'])
        self.assertNotIn('media',row)

    def test_failure_replaces_stale_image_and_forget_clears_association(self):
        self.put()
        self.store.put('user-a','group-a','chat-a',self.author,error='image_decode_failed')
        row=self.store.candidate('user-a','group-a','chat-a')
        self.assertIsNone(row['media'])
        self.assertEqual(row['error'],'image_decode_failed')
        self.store.forget('user-a')
        self.assertIsNone(self.store.candidate('user-a','group-a','chat-a'))

    def test_expired_image_is_not_selected(self):
        self.put()
        with patch.object(store_module.time,'time',return_value=store_module.time.time()+301):
            self.assertIsNone(self.store.candidate('user-a','group-a','chat-a'))

    def test_prompt_metadata_omits_local_paths(self):
        self.put()
        import json
        text=json.dumps(store_module.describe(self.store.candidate('user-a','group-a','chat-a')))
        self.assertNotIn(str(self.root),text)
        self.assertNotIn('path',text)

    def test_same_nickname_is_not_same_member_or_bot(self):
        class Event:
            message_obj=type('Message',(),{'message_id':'12'})()
            def get_platform_id(self):return 'napcat'
            def get_group_id(self):return 'group-a'
            def get_sender_id(self):return self.sender
            def get_sender_name(self):return '小助手'
            def get_self_id(self):return '999'
        first,second=Event(),Event();first.sender='111';second.sender='222'
        a,b=identity.message_identity(first),identity.message_identity(second)
        self.assertNotEqual(a['speaker_ref'],b['speaker_ref'])
        self.assertFalse(a['is_bot']);self.assertFalse(b['is_bot'])
        self.assertTrue(identity.message_identity(first,bot=True)['is_bot'])


class ContextBudgetTests(unittest.TestCase):
    def test_200k_changes_only_main_provider_context_and_history_budget(self):
        spec=importlib.util.spec_from_file_location('context_budget_fixture',ROOT.parents[1]/'scripts/context-budget.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        config={'agent_runner':{'config':{'compression':{'max_turns':100,'trim_turns':10,'fallback_max_tokens':49152}}},
                'provider':[{'id':'api_chat','max_context_tokens':49152,'model':'current-model'},
                            {'id':'api_deep','max_context_tokens':49152,'model':'current-model'},
                            {'id':'api_classifier','max_context_tokens':16384}], 'admins_id':['111']}
        updated=module.with_budget(config)
        self.assertEqual(config['provider'][0]['max_context_tokens'],49152)
        self.assertEqual([p['max_context_tokens'] for p in updated['provider']],[204800,204800,16384])
        self.assertEqual(updated['agent_runner']['config']['compression'],
                         {'max_turns':100,'trim_turns':10,'fallback_max_tokens':204800})
        self.assertEqual(updated['admins_id'],config['admins_id'])
