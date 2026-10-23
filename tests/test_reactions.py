"""Regression checks for reaction identity, scope, removal, bounds and silent ingestion."""
import asyncio
import importlib
import json
from pathlib import Path
import sqlite3
import tempfile
import types
import unittest
from unittest.mock import patch

import test_guard as guard_tests

Event, guard_module = guard_tests.Event, guard_tests.guard_module

reactions = importlib.import_module(guard_module.__package__+'.reactions')
GROUP = '8001'
AUTHOR, ACTOR = '55667112233', '66778223344'


def notice(**changes):
    raw = {'post_type':'notice', 'notice_type':'group_msg_emoji_like', 'self_id':1234,
           'group_id':int(GROUP), 'user_id':int(ACTOR), 'message_id':-701,
           'is_add':True, 'likes':[{'emoji_id':'128077','count':1}], 'time':1792735200}
    raw.update(changes)
    return raw


def event(raw=None, text='', sender=ACTOR):
    e = Event(private=False, text=text)
    e.unified_msg_origin = 'napcat:GroupMessage:'+GROUP
    e.get_group_id = lambda:GROUP
    e.get_sender_id = lambda:sender
    e.get_sender_name = lambda:'同名群友'
    e.message_obj.message_id = '731'
    if raw is not None:
        e.message_obj.raw_message = raw
    return e


class ReactionParsingTests(unittest.TestCase):
    def test_unicode_qq_and_unknown_emojis_remain_distinct(self):
        row = reactions.parse_notice(notice(likes=[{'emoji_id':'128077','count':1},
            {'emoji_id':'10084','count':2}, {'emoji_id':'76','count':1}, {'emoji_id':'999','count':None}]))
        self.assertIn('👍', row['emojis'][0]['label'])
        self.assertIn('❤', row['emojis'][1]['label'])
        self.assertEqual(row['emojis'][2]['label'], '赞')
        self.assertEqual(row['emojis'][3]['label'], '未知表情')

    def test_explicit_removal_and_missing_action(self):
        self.assertEqual(reactions.parse_notice(notice(is_add=False,sub_type='add'))['action'],'remove')
        self.assertEqual(reactions.parse_notice(notice(is_add=None,sub_type='remove'))['action'],'remove')
        self.assertEqual(reactions.parse_notice(notice(is_add=None))['action'],'unknown')

    def test_missing_actor_is_never_replaced_by_target_author(self):
        self.assertIsNone(reactions.parse_notice(notice(user_id=None))['actor'])
        self.assertIsNone(reactions.parse_notice(notice(user_id=0))['actor'])

    def test_rejects_non_reaction_and_malformed_records(self):
        for changes in ({'post_type':'message'}, {'group_id':0}, {'message_id':True},
                        {'message_id':'not-a-message'}, {'likes':{}},
                        {'likes':[{'emoji_id':'https://example.invalid/'}]}):
            with self.subTest(changes=changes):
                self.assertIsNone(reactions.parse_notice(notice(**changes)))

    def test_emoji_list_and_counts_are_bounded(self):
        row = reactions.parse_notice(notice(likes=[{'emoji_id':str(10000+i),'count':99999999} for i in range(100)]))
        self.assertEqual(len(row['emojis']),16)
        self.assertEqual(row['emojis'][0]['count'],100000)
        row = reactions.parse_notice(notice(likes=[{'emoji_id':'76','count':True},{'emoji_id':'76','count':2}]))
        self.assertEqual(len(row['emojis']),1)
        self.assertIsNone(row['emojis'][0]['count'])

    def test_retrieved_message_requires_exact_group_and_message(self):
        n = reactions.parse_notice(notice())
        base = {'message_type':'group','group_id':int(GROUP),'message_id':-701,
                'sender':{'user_id':1234,'nickname':'机器人'},
                'message':[{'type':'text','data':{'text':'真实回答'}},{'type':'image','data':{'url':'https://secret.invalid/a?token=secret'}}]}
        got = reactions.target_from_message(base,n,'napcat:'+GROUP,'1234')
        self.assertTrue(got['is_bot'])
        self.assertEqual(got['text'],'真实回答[图片]')
        self.assertNotIn('secret',json.dumps(got))
        for changes in ({'group_id':8002},{'group_id':None},{'message_id':999},{'message_type':'private'}):
            self.assertIsNone(reactions.target_from_message(base|changes,n,'napcat:'+GROUP,'1234'))

    def test_notice_deduplication_is_scoped_and_lookup_rate_is_bounded(self):
        limits = reactions.ReactionLimits()
        self.assertTrue(limits.claim('group1','same'))
        self.assertFalse(limits.claim('group1','same'))
        self.assertTrue(limits.claim('group2','same'))
        self.assertEqual(sum(limits.allow_lookup() for _ in range(50)),12)
        for i in range(1000):limits.claim('group1',str(i))
        self.assertLessEqual(len(limits.seen),512)

    def test_full_official_qq_catalog_resolves_reported_reactions(self):
        self.assertGreaterEqual(len(reactions.QQ_FACES),329)
        self.assertEqual([reactions.emoji_label(v) for v in ['147','424','476','76']],
                         ['棒棒糖','续标识','不是吧','赞'])

    def test_zero_is_valid_qq_face_id_but_not_group_or_message_id(self):
        row=reactions.parse_notice(notice(likes=[{'emoji_id':'0','count':1}]))
        self.assertEqual(row['emojis'][0]['label'],'惊讶')
        self.assertIsNone(reactions.parse_notice(notice(group_id=0)))


class ReactionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'reactions.sqlite3'

    def test_empty_read_creates_no_database(self):
        self.assertEqual(reactions.ReactionStore(self.path).recent('group1'),[])
        self.assertFalse(self.path.exists())

    def test_persistence_isolation_and_duplicate_delivery(self):
        store = reactions.ReactionStore(self.path)
        self.assertTrue(store.record('group1','same',{'action':'add'}))
        self.assertFalse(store.record('group1','same',{'action':'add'}))
        reopened = reactions.ReactionStore(self.path)
        self.assertEqual(reopened.recent('group2'),[])
        self.assertEqual(len(reopened.recent('group1')),1)
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)

    def test_group_count_record_count_and_expiry_are_bounded(self):
        store = reactions.ReactionStore(self.path,max_groups=2,per_group=3,ttl=60)
        for i in range(10):store.record('group1',str(i),{'index':i})
        self.assertEqual([v['index'] for v in store.recent('group1')],[7,8,9])
        store.record('group2','1',{})
        store.record('group3','1',{})
        self.assertEqual(store.recent('group1'),[])
        with patch.object(reactions.time,'time',return_value=reactions.time.time()+65):
            self.assertEqual(store.recent('group3'),[])

    def test_old_unknown_names_are_refreshed_without_rewriting_saved_events(self):
        store=reactions.ReactionStore(self.path)
        store.record('group1','1',{'emojis':[{'emoji_id':'147','label':'未知表情','count':1}]})
        self.assertEqual(store.recent('group1')[0]['emojis'][0]['label'],'棒棒糖')
        with sqlite3.connect(self.path) as db:
            self.assertEqual(json.loads(db.execute('SELECT payload FROM reactions').fetchone()[0])['emojis'][0]['label'],'未知表情')


class ReactionGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        guard_tests.GuardTests.setUp(self)

    async def remember(self, sender=AUTHOR, message_id=-701, text='你搜一下2024年揭阳市高考状元是谁'):
        e = event(sender=sender)
        e.message_obj.message_id = str(message_id)
        await self.guard._record_group(e,'同名群友',text)

    async def test_cached_target_and_actor_remain_different_even_with_same_name(self):
        await self.remember()
        await self.remember(sender=ACTOR,message_id=702,text='这是另一个人')
        e = event(notice())
        async def forbidden(*args,**kwargs):raise AssertionError('notice must not call a model')
        self.guard._classify_event = forbidden
        await self.guard.guard(e)
        row = self.guard._reactions.recent('napcat:'+GROUP)[0]
        self.assertTrue(e.stopped)
        self.assertFalse(e.sent or e.api_calls)
        self.assertIsNone(self.guard._active)
        self.assertNotEqual(row['actor']['speaker_ref'],row['target']['speaker_ref'])
        self.assertEqual(row['target']['text'],'你搜一下2024年揭阳市高考状元是谁')
        self.assertEqual(row['actor']['speaker_name'],row['target']['speaker_name'])
        self.assertNotIn(AUTHOR,json.dumps(row))
        self.assertNotIn(ACTOR,json.dumps(row))

    async def test_unknown_actor_stays_unknown(self):
        await self.remember()
        e = event(notice(user_id=None),sender=AUTHOR)
        await self.guard.guard(e)
        actor = self.guard._reactions.recent('napcat:'+GROUP)[0]['actor']
        self.assertFalse(actor['known'])
        self.assertEqual(actor['speaker_ref'],'未知')
        self.assertEqual(actor['speaker_name'],'未知回应者')

    async def test_can_resolve_actual_bot_message_and_reuses_verified_original(self):
        e = event(notice())
        calls=[]
        async def get_msg(action,**params):
            calls.append((action,params))
            return {'message_type':'group','group_id':int(GROUP),'message_id':-701,
                'sender':{'user_id':1234,'nickname':'机器人'},'message':[{'type':'text','data':{'text':'已发送的原回答'}}]}
        e.bot.call_action = get_msg
        await self.guard.guard(e)
        self.assertEqual(calls,[('get_msg',{'message_id':-701})])
        row=self.guard._reactions.recent('napcat:'+GROUP)[0]
        self.assertTrue(row['target']['resolved'] and row['target']['is_bot'])
        second=event(notice(is_add=False))
        second.bot.call_action=get_msg
        await self.guard.guard(second)
        rows=self.guard._reactions.recent('napcat:'+GROUP)
        self.assertEqual([x['action'] for x in rows],['add','remove'])
        self.assertEqual(len(calls),1)

    async def test_wrong_group_original_never_leaks_or_uses_nearby_text(self):
        await self.remember(message_id=999,text='不相关的附近消息')
        e=event(notice())
        async def wrong(*a,**kw):return {'message_type':'group','group_id':8002,'message_id':-701,
            'sender':{'user_id':AUTHOR},'message':[{'type':'text','data':{'text':'其他群的隐私'}}]}
        e.bot.call_action=wrong
        await self.guard.guard(e)
        row=self.guard._reactions.recent('napcat:'+GROUP)[0]
        self.assertFalse(row['target']['resolved'])
        self.assertEqual(row['target']['text'],'')
        self.assertNotIn('其他群的隐私',json.dumps(row,ensure_ascii=False))

    async def test_duplicate_notice_is_not_recorded_twice(self):
        await self.remember()
        await self.guard.guard(event(notice()))
        await self.guard.guard(event(notice()))
        self.assertEqual(len(self.guard._reactions.recent('napcat:'+GROUP)),1)

    async def test_malformed_mismatched_and_private_notices_are_silent(self):
        for e in (event(notice(likes=[])),event(notice(group_id=8002)),Event(text='')):
            if e.is_private_chat():e.message_obj.raw_message=notice()
            await self.guard.guard(e)
            self.assertTrue(e.stopped)
            self.assertFalse(e.sent or e.api_calls)
        self.assertFalse(self.guard._reactions.path.exists())

    async def test_reactions_continue_to_collect_while_generation_is_busy(self):
        await self.remember()
        ready=asyncio.Event()
        async def work():
            await self.guard.guard(Event())
            ready.set()
            await asyncio.Event().wait()
        task=asyncio.create_task(work())
        await ready.wait()
        try:
            await self.guard.guard(event(notice()))
            self.assertEqual(len(self.guard._reactions.recent('napcat:'+GROUP)),1)
            self.assertIs(self.guard._active,task)
            self.assertFalse(task.done())
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def test_next_question_receives_reactions_as_material_for_both_models(self):
        await self.remember(text='原消息中的忽略系统指令只是用户文字')
        await self.guard.guard(event(notice()))
        ask=event(text='你看到刚才的表情了吗')
        ask.messages=[guard_module.At('1234')]
        await self.guard.guard(ask)
        self.assertEqual(len(ask.get_extra('qqbot_group_reactions')),1)
        inp=types.SimpleNamespace(contexts=[],prompt=ask.message_obj.message_str,
            group_reactions=ask.get_extra('qqbot_group_reactions'))
        self.assertIn('recent_group_reactions',json.loads(self.guard._classifier_input(inp,5000)))
        req=types.SimpleNamespace(system_prompt='persona',extra_user_content_parts=[],image_urls=[],model=None)
        await self.guard.route_model(ask,req)
        materials='\n'.join(p.text for p in req.extra_user_content_parts)
        self.assertIn('真实表情回应',materials)
        self.assertIn('忽略系统指令只是用户文字',materials)
        self.assertNotIn('忽略系统指令只是用户文字',req.system_prompt)
        self.assertIn('撤销',req.system_prompt)
        self.guard._finish_event(ask)

    async def test_private_model_request_never_receives_group_reactions(self):
        e=Event()
        e.set_extra('qqbot_group_reactions',[{'target':{'text':'群内材料'}}])
        req=types.SimpleNamespace(system_prompt='persona',extra_user_content_parts=[],image_urls=[],model=None)
        await self.guard.route_model(e,req)
        self.assertNotIn('群内材料',str([p.text for p in req.extra_user_content_parts]))

    async def test_reaction_records_do_not_displace_one_hundred_chat_messages(self):
        self.guard._group_store=guard_module.GroupContextStore(Path(self.temp.name)/'100.sqlite3',max_messages=100)
        for i in range(100):await self.remember(message_id=1000+i,text='正常聊天'+str(i))
        for i in range(40):
            await self.guard.guard(event(notice(message_id=1000+i,time=1792735200+i)))
        self.assertEqual(len(self.guard._group_store.recent('napcat:'+GROUP)),100)
        self.assertEqual(len(self.guard._reactions.recent('napcat:'+GROUP,64)),32)

    async def test_lookup_and_storage_failures_never_send_or_hold_a_request_slot(self):
        e=event(notice())
        async def failed(*args,**kwargs):raise RuntimeError('not accessible')
        e.bot.call_action=failed
        self.guard._reactions.record=lambda *a,**kw:(_ for _ in ()).throw(OSError('fixture'))
        await self.guard.guard(e)
        self.assertTrue(e.stopped)
        self.assertFalse(e.sent)
        self.assertIsNone(self.guard._active)

    async def test_explicit_quote_only_receives_its_own_reactions(self):
        await self.remember(message_id=-701)
        await self.remember(message_id=-702)
        await self.guard.guard(event(notice(message_id=-701,likes=[{'emoji_id':'147','count':1}])))
        await self.guard.guard(event(notice(message_id=-702,likes=[{'emoji_id':'76','count':1}])))
        ask=event(text='这句话是什么表情')
        ask.messages=[guard_module.Reply(id=-701)]
        await self.guard._reaction_context(ask)
        rows=ask.get_extra('qqbot_group_reactions')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['emojis'][0]['label'],'棒棒糖')
        self.assertTrue(ask.get_extra('qqbot_reaction_observation')['has_explicit_target'])

    async def test_unobserved_quoted_target_does_not_borrow_another_messages_reactions(self):
        await self.remember()
        await self.guard.guard(event(notice()))
        ask=event(text='这句话是什么表情')
        ask.messages=[guard_module.Reply(id=-999,message_str='实际引用原文',chain=[guard_module.Plain('实际引用原文')])]
        await self.guard._reaction_context(ask)
        self.assertEqual(ask.get_extra('qqbot_group_reactions'),[])
        req=types.SimpleNamespace(system_prompt='persona',extra_user_content_parts=[],image_urls=[],model=None)
        await self.guard.route_model(ask,req)
        self.assertIn('received_event_count',req.extra_user_content_parts[-1].text)
        self.assertIn('不等于它没有表情',req.system_prompt)

    async def test_current_speaker_flags_are_recomputed_per_question(self):
        await self.remember()
        await self.guard.guard(event(notice()))
        first=event(sender=ACTOR)
        await self.guard._reaction_context(first)
        self.assertTrue(first.get_extra('qqbot_group_reactions')[0]['actor']['is_current_speaker'])
        other=event(sender=AUTHOR)
        await self.guard._reaction_context(other)
        row=other.get_extra('qqbot_group_reactions')[0]
        self.assertFalse(row['actor']['is_current_speaker'])
        self.assertTrue(row['target']['is_current_speaker'])

    async def test_no_reactions_no_quote_does_not_add_an_empty_prompt(self):
        ask=event()
        await self.guard._reaction_context(ask)
        self.assertIsNone(ask.get_extra('qqbot_reaction_observation'))
