"""Quota sharing, cooldowns, bounded failover, credential and source isolation."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web
import test_guard as base

pool=__import__(base.guard_module.__package__+'.search_pool',fromlist=['*'])
state=__import__(base.guard_module.__package__+'.search_state',fromlist=['*'])
search=__import__(base.guard_module.__package__+'.search',fromlist=['*'])
RESULT={'summary':'实际网页摘录','sources':[{'title':'Official','url':'https://example.com/docs'}],'searched_at':'2026-10-22T08:00:00+00:00'}


def credential(provider='tavily',label='one',group='default',key='fixture-key-0001',**kw):
    return {'provider':provider,'id':label,'quota_group':group,'api_key':key,'enabled':True,'free_plan':True,**kw}


class SearchStateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.now=[datetime(2026,10,22,8,tzinfo=timezone.utc).timestamp()]
        self.db=state.SearchState(Path(self.temp.name)/'search.sqlite3',clock=lambda:self.now[0])
        self.account=pool.account_rows({'accounts':[credential(local_limit=2)]})[0]

    def test_same_account_keys_share_quota_across_instances(self):
        second=pool.account_rows({'accounts':[credential(label='two',key='fixture-key-0002',local_limit=2)]})[0]
        self.assertTrue(self.db.reserve(self.account));self.assertTrue(self.db.reserve(second))
        restarted=state.SearchState(self.db.path,clock=lambda:self.now[0])
        self.assertFalse(restarted.reserve(self.account));self.assertFalse(restarted.reserve(second))
        self.assertEqual(self.db.status([self.account])[0]['used'],2)

    def test_concurrent_requests_cannot_overspend_local_cap(self):
        self.account['limit']=5
        with ThreadPoolExecutor(max_workers=8) as executor:values=list(executor.map(lambda _:self.db.reserve(self.account),range(20)))
        self.assertEqual(sum(values),5);self.assertEqual(self.db.status([self.account])[0]['used'],5)

    def test_rate_limit_blocks_the_shared_group_and_respects_retry_after(self):
        second={**self.account,'id':'tavily:two','revision':'other-key'}
        self.db.fail(self.account,'rate_limit',300)
        self.assertFalse(self.db.reserve(second));self.now[0]+=301;self.assertTrue(self.db.reserve(second))

    def test_quota_failure_waits_for_calendar_reset(self):
        self.db.fail(self.account,'quota');self.now[0]+=86400
        self.assertFalse(self.db.reserve(self.account))
        self.now[0]=datetime(2026,11,1,tzinfo=timezone.utc).timestamp();self.assertTrue(self.db.reserve(self.account))

    def test_invalid_key_can_be_replaced_without_resetting_budget(self):
        self.assertTrue(self.db.reserve(self.account));self.db.fail(self.account,'auth')
        self.assertFalse(self.db.reserve(self.account))
        revised={**self.account,'revision':'new-key-hash'}
        self.assertTrue(self.db.reserve(revised));self.assertFalse(self.db.reserve(revised))
        self.db.fail(revised,'unavailable');self.now[0]+=31
        revised['limit']=3;self.assertTrue(self.db.reserve(revised))

    def test_daily_budget_resets_at_UTC_midnight(self):
        daily={**self.account,'period':'day','limit':1}
        self.assertTrue(self.db.reserve(daily));self.assertFalse(self.db.reserve(daily))
        self.now[0]=datetime(2026,10,23,tzinfo=timezone.utc).timestamp();self.assertTrue(self.db.reserve(daily))

    def test_source_cache_isolated_by_user_conversation_and_expiry(self):
        self.db.put_sources('private-one','cid-one',RESULT)
        self.assertEqual(self.db.get_sources('private-one','cid-one')['sources'],RESULT['sources'])
        self.assertIsNone(self.db.get_sources('private-two','cid-one'));self.assertIsNone(self.db.get_sources('private-one','cid-two'))
        self.now[0]+=1801;self.assertIsNone(self.db.get_sources('private-one','cid-one'))


class SearchPoolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name);self.engine=pool.SearchPool(self.folder)
        self.cfg={'api_key_env':'POOL_FIXTURE_NATIVE','model':'fixture','timeout_seconds':1,'provider_timeout_seconds':0.05,'max_attempts':4}

    def write(self,items):
        self.engine.credentials.write_text(json.dumps({'accounts':items}));self.engine.credentials.chmod(0o600)

    async def test_quota_exhaustion_fails_over_once_then_skips_account_after_restart(self):
        self.write([credential('tinyfish'),credential('tavily')]);calls=[]
        async def execute(account,*args):
            calls.append(account['provider'])
            if account['provider']=='tinyfish':raise search.SearchError('fixture','quota')
            return RESULT
        self.engine._execute=execute
        await self.engine.search('query',self.cfg)
        restarted=pool.SearchPool(self.folder);restarted._execute=execute;await restarted.search('query',self.cfg)
        self.assertEqual(calls,['tinyfish','tavily','tavily'])


    async def test_cancellation_never_starts_a_second_provider(self):
        self.write([credential('tinyfish'),credential('tavily')]);entered=asyncio.Event();calls=[]
        async def execute(account,*args):calls.append(account['provider']);entered.set();await asyncio.Event().wait()
        self.engine._execute=execute
        task=asyncio.create_task(self.engine.search('query',self.cfg));await entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(calls,['tinyfish'])

    async def test_invalid_query_does_not_fan_out_to_other_providers(self):
        self.write([credential('tinyfish'),credential('tavily')]);calls=[]
        async def execute(account,*args):calls.append(account['provider']);raise search.http_failure(400)
        self.engine._execute=execute
        with self.assertRaisesRegex(search.SearchError,'search_pool_exhausted'):await self.engine.search('query',self.cfg)
        self.assertEqual(calls,['tinyfish'])

    async def test_duplicate_key_does_not_create_an_extra_account_or_budget(self):
        rows=pool.account_rows({'accounts':[credential(),credential(label='copy',group='another')]})
        self.assertEqual(len(rows),1)

    async def test_credentials_permissions_free_plan_and_invalid_endpoint_are_bounded(self):
        self.write([credential()]);self.engine.credentials.chmod(0o644)
        with self.assertRaisesRegex(search.SearchError,'search_pool_config'):self.engine._accounts(self.cfg)
        self.write([credential(free_plan=False)])
        with self.assertRaisesRegex(search.SearchError,'search_pool_config'):self.engine._accounts(self.cfg)
        with self.assertRaises(search.SearchError):pool.account_rows({'accounts':[credential('untrusted_provider')]})

    async def test_local_exhaustion_makes_no_network_requests(self):
        self.write([credential(local_limit=1)]);account=self.engine._accounts(self.cfg)[0];self.engine.state.reserve(account)
        async def forbidden(*args):raise AssertionError('exhausted must skip network')
        self.engine._execute=forbidden
        with self.assertRaisesRegex(search.SearchError,'search_pool_exhausted'):await self.engine.search('query',self.cfg)


class SearchProviderHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_payloads_and_chunked_search_results_do_not_put_keys_in_URLs(self):
        seen=[]
        async def respond(request):
            provider=request.path[1:];seen.append(provider)
            self.assertNotIn('fixture-key',str(request.rel_url))
            if provider=='tinyfish':self.assertEqual(request.headers['X-API-Key'],'fixture-key-0001')
            else:
                data=await request.json();self.assertEqual(data['query'],'query')
                if provider=='tavily':self.assertEqual(data['search_depth'],'basic');self.assertFalse(data['auto_parameters'])
                else:self.assertEqual(data['contents'],{'highlights':True})
            raw=json.dumps({'results':[{'title':'doc','url':'https://example.com','content':'verified snippet'}]}).encode()
            response=web.StreamResponse(headers={'Content-Type':'application/json'});await response.prepare(request)
            for i in range(0,len(raw),7):await response.write(raw[i:i+7])
            await response.write_eof();return response
        app=web.Application();app.router.add_route('*','/{provider}',respond)
        runner=web.AppRunner(app);await runner.setup();server=web.TCPSite(runner,'127.0.0.1',0);await server.start()
        port=server._server.sockets[0].getsockname()[1]
        try:
            with tempfile.TemporaryDirectory() as folder:
                engine=pool.SearchPool(folder)
                with patch.dict(pool.ENDPOINTS,{p:f'http://127.0.0.1:{port}/{p}' for p in pool.PROVIDERS}):
                    for provider in pool.PROVIDERS:
                        account=pool.account_rows({'accounts':[credential(provider)]})[0]
                        value=await engine._execute(account,'query',{},2)
                        self.assertEqual(value['summary'],'doc\nverified snippet')
            self.assertEqual(seen,['tinyfish','tavily','exa'])
        finally:await runner.cleanup()

    async def test_raw_search_response_needs_public_sources_with_real_snippets(self):
        for rows in [[{'title':'link only','url':'https://example.com'}],[{'title':'private','url':'http://127.0.0.1','content':'text'}]]:
            with self.assertRaisesRegex(search.SearchError,'search_not_grounded'):pool.parse_results({'results':rows})
        value=pool.parse_results({'results':[{'title':'doc','url':'https://example.com','highlights':['one','two']}]})
        self.assertIn('one\ntwo',value['summary'])
