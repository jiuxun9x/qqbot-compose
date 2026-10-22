"""Verified sources, source validation, and existing QQ reply/format policy."""
import asyncio
import base64
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp import web
from PIL import Image
import test_guard as base

search=__import__(base.guard_module.__package__+'.search',fromlist=['*'])
RESULT={'summary':'官方免费额度为每月 1000 积分。','sources':[{'title':'官方文档','url':'https://docs.tavily.com/documentation/api-credits'}],
        'searched_at':'2026-10-22T08:00:00+00:00'}




class SearchParsingTests(unittest.TestCase):
    def test_command_is_explicit_and_has_a_boundary(self):
        self.assertEqual(search.explicit_search_query('小助手，／搜索： 今天合肥天气'),'今天合肥天气')
        self.assertEqual(search.explicit_search_query('/search query'),'query')
        self.assertEqual(search.explicit_search_query('/搜索'),'')
        for text in ('我提到 /搜索 新闻','/searching x','他说帮我搜索','别联网'):
            self.assertIsNone(search.explicit_search_query(text))

    def test_classifier_search_contract_keeps_query_but_not_arbitrary_endpoints(self):
        value=base.guard_module.parse_intent(json.dumps({'intent':'search','difficulty':'normal','prompt':'Tavily 免费额度','endpoint':'http://localhost'}))
        self.assertEqual(value,{'intent':'search','difficulty':'normal','prompt':'Tavily 免费额度'})




    def test_sources_reject_credentials_private_addresses_and_non_web_schemes(self):
        for value in ['file:///etc/passwd','javascript:alert(1)','https://user:pass@example.com','http://127.0.0.1',
                      'http://10.0.0.1','http://localhost','https://example.internal','https://example.com/\nsecret']:
            self.assertIsNone(search.safe_source_url(value))
        self.assertEqual(search.safe_source_url('https://docs.tavily.com/path'),'https://docs.tavily.com/path')

    def test_sources_are_idempotent_in_history(self):
        once=search.append_sources('答案',RESULT)
        self.assertEqual(search.append_sources(once,RESULT),once)
        self.assertIn(RESULT['sources'][0]['url'],once)







class SearchGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        base.GuardTests.setUp(self);self.calls=[]
        async def lookup(query,config):self.calls.append(query);return dict(RESULT)
        self.guard._search.search=lookup

    async def asyncTearDown(self):await self.guard.terminate()

    async def test_explicit_command_prepares_verified_material_for_normal_pipeline(self):
        async def forbidden(*args,**kwargs):raise AssertionError('explicit search must not need a classifier')
        self.guard._classify_event=forbidden
        event=base.Event(text='/搜索 Tavily 免费额度');await self.guard.guard(event)
        self.assertFalse(event.stopped);self.assertEqual(self.calls,['Tavily 免费额度'])
        req=SimpleNamespace(prompt=event.message_obj.message_str,system_prompt='既有人设',contexts=[],image_urls=[],extra_user_content_parts=[])
        await self.guard.route_model(event,req)
        self.assertTrue(req.system_prompt.startswith('既有人设'))
        self.assertTrue(any('实际联网检索取得' in part.text for part in req.extra_user_content_parts))

    async def test_natural_search_reuses_the_small_model_decision(self):
        async def classify(*args,**kwargs):return {'intent':'search','difficulty':'normal','prompt':'最新算法论文'}
        self.guard._classify_event=classify
        event=base.Event(text='帮我搜索最新算法论文');await self.guard.guard(event)
        self.assertEqual(self.calls,['最新算法论文']);self.assertFalse(event.stopped)

    async def test_normal_chat_and_negation_do_not_trigger_search(self):
        for text in ('你好','别联网，只聊聊','代码里的二分搜索怎么写'):
            event=base.Event(text=text);await self.guard.guard(event);self.guard._finish_event(event,history_pending=False)
        self.assertEqual(self.calls,[])

    async def test_group_at_only_policy_still_precedes_search(self):
        self.guard._group_modes.set('napcat:group1',False)
        ignored=base.Event(private=False,text='/搜索 当前天气');await self.guard.guard(ignored)
        self.assertTrue(ignored.stopped);self.assertEqual(self.calls,[])
        called=base.Event(private=False,text='/搜索 当前天气',messages=[base.At('1234')]);await self.guard.guard(called)
        self.assertFalse(called.stopped);self.assertEqual(self.calls,['当前天气'])

    async def test_random_group_interjection_does_not_start_search(self):
        event=base.Event(private=False,text='今天什么新闻');event.set_extra('qqbot_intent',{'intent':'search','prompt':'今天新闻'})
        self.assertTrue(await self.guard.search_if_requested(event,event.message_obj.message_str,True))
        self.assertEqual(self.calls,[]);self.assertTrue(event.stopped)

    async def test_failure_never_falls_back_to_claiming_search_success(self):
        async def fail(*args):raise search.SearchError('search_not_grounded')
        self.guard._search.search=fail
        event=base.Event(text='/搜索 今天新闻');await self.guard.guard(event)
        self.assertTrue(event.stopped);self.assertIsNone(event.get_extra('qqbot_search_result'))
        self.assertIn('没有成功取得可靠来源',event.sent[0]);self.assertIsNone(self.guard._gate.for_event(event))

    async def test_sources_survive_plain_text_and_saved_history(self):
        event=base.Event();event.set_extra('qqbot_route',{'decision':'normal'});event.set_extra('qqbot_search_result',RESULT)
        event.set_extra('qqbot_show_sources',True)
        response=SimpleNamespace(completion_text='<qqbot:plain>\n**每月1000积分。**',result_chain=None)
        await self.guard.prepare_answer(event,response)
        event.result=SimpleNamespace(chain=[base.guard_module.Plain(response.completion_text)],is_llm_result=lambda:True)
        await self.guard.present_answer(event)
        self.assertIn(RESULT['sources'][0]['url'],event.result.chain[0].text);self.assertNotIn('**',event.result.chain[0].text)
        part=base.guard_module.TextPart(text='<qqbot:plain>\n每月1000积分。')
        await self.guard.clean_history_marker(event,SimpleNamespace(messages=[SimpleNamespace(role='assistant',content=[part])]),response)
        self.assertIn(RESULT['sources'][0]['url'],part.text)

    async def test_math_still_renders_and_sources_remain_clickable_text(self):
        event=base.Event();event.set_extra('qqbot_route',{'decision':'hard'});event.set_extra('qqbot_search_result',RESULT)
        event.set_extra('qqbot_show_sources',True)
        response=SimpleNamespace(completion_text='<qqbot:latex>\n$x=2$',result_chain=None)
        await self.guard.prepare_answer(event,response)
        output=io.BytesIO();Image.new('RGB',(12,12),'blue').save(output,format='PNG');encoded=base64.b64encode(output.getvalue()).decode()
        async def render(url,body,**kwargs):
            self.assertNotIn('https:',body['content']);return {'pages':[encoded]}
        self.guard._post_json=render
        event.result=SimpleNamespace(chain=[base.guard_module.Plain(response.completion_text)],is_llm_result=lambda:True)
        await self.guard.present_answer(event)
        self.assertTrue(event.result.chain[0].encoded)
        self.assertIn(RESULT['sources'][0]['url'],event.result.chain[-1].text)

    async def test_default_search_answer_hides_links_and_followup_retrieves_them(self):
        event=base.Event(text='/搜索 Tavily 额度');await self.guard.guard(event)
        response=SimpleNamespace(completion_text='<qqbot:plain>\n每月1000积分。[文档](https://docs.tavily.com/documentation/api-credits)',result_chain=None)
        await self.guard.prepare_answer(event,response)
        self.assertNotIn('https://',response.completion_text);self.assertNotIn('来源：',response.completion_text)
        self.guard._finish_event(event,history_pending=False)
        follow=base.Event(text='/来源');await self.guard.guard(follow)
        self.assertTrue(follow.stopped);self.assertIn(RESULT['sources'][0]['url'],follow.sent[0])
        self.assertEqual(self.calls,['Tavily 额度'])

    async def test_explicit_source_request_can_show_links_but_negative_request_wins(self):
        for text,show in [('/搜索 Tavily 免费额度，附来源链接',True),('/搜索 Tavily 免费额度，不要来源链接',False)]:
            event=base.Event(text=text);await self.guard.guard(event)
            self.assertEqual(event.get_extra('qqbot_show_sources'),show)
            self.guard._finish_event(event,history_pending=False)

    async def test_source_followup_is_isolated_and_reset_clears_cache(self):
        self.guard._search_state.put_sources(base.Event.unified_msg_origin,None,RESULT)
        other=base.Event(text='/来源');other.unified_msg_origin='napcat:FriendMessage:other'
        await self.guard.guard(other);self.assertNotIn('https://',other.sent[0])
        reset=base.Event(text='/reset');await self.guard.guard(reset)
        self.assertIsNone(self.guard._search_state.get_sources(base.Event.unified_msg_origin,None))

    async def test_source_link_filter_keeps_code_addresses_and_removes_prose_citations(self):
        text='说明 [文档](https://example.com/docs)。\n```python\napi="https://api.example.com"\n```\n参考：https://example.com/more。'
        result=search.hide_source_links(text)
        self.assertNotIn('https://example.com',result);self.assertIn('https://api.example.com',result)

    async def test_pool_status_requires_real_admin_and_never_calls_search(self):
        denied=base.Event(text='/搜索池');await self.guard.guard(denied)
        self.assertIn('管理员',denied.sent[0]);self.assertEqual(self.calls,[])
        self.guard.context.get_config=lambda **kwargs:{'wake_prefix':['/'],'admins_id':['5678']}
        event=base.Event(text='/搜索池');await self.guard.guard(event)
        self.assertIn('本地调用计数',event.sent[0]);self.assertEqual(self.calls,[])
