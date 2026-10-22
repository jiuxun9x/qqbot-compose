"""Authorized search credentials, shared budgets and bounded failover."""
import asyncio
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

import aiohttp
from .search import SearchError,http_failure,safe_source_url
from .search_state import SearchState


PROVIDERS={'tinyfish':('day',1000,12000),'tavily':('month',900,1000),'exa':('month',200,1000)}
ENDPOINTS={'tinyfish':'https://api.search.tinyfish.ai','tavily':'https://api.tavily.com/search','exa':'https://api.exa.ai/search'}


def load_credentials(path):
    path=Path(path)
    if not path.exists():return {'accounts':[]}
    if path.is_symlink() or not path.is_file() or path.stat().st_size>32768 or stat.S_IMODE(path.stat().st_mode)&0o077:
        raise SearchError('search_pool_config')
    try:data=json.loads(path.read_text())
    except (ValueError,UnicodeError):raise SearchError('search_pool_config') from None
    if not isinstance(data,dict) or not isinstance(data.get('accounts'),list) or len(data['accounts'])>16:raise SearchError('search_pool_config')
    return data


def account_rows(data):
    accounts=[];seen=set();identifiers=set()
    for item in data['accounts']:
        if not isinstance(item,dict) or not item.get('enabled',True):continue
        if item.get('free_plan') is not True:raise SearchError('search_pool_config')
        provider=item.get('provider');key=item.get('api_key','');label=item.get('id','');group=item.get('quota_group','default')
        if provider not in PROVIDERS or not all(isinstance(value,str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,32}',value) for value in (label,group)):
            raise SearchError('search_pool_config')
        if not isinstance(key,str) or not 10<=len(key)<=512 or any(c.isspace() for c in key):raise SearchError('search_pool_config')
        revision=hashlib.sha256(key.encode()).hexdigest();identifier=provider+':'+label
        if (provider,revision) in seen:continue
        if identifier in identifiers:raise SearchError('search_pool_config')
        seen.add((provider,revision));identifiers.add(identifier)
        period,default,maximum=PROVIDERS[provider]
        try:limit=max(1,min(int(item.get('local_limit',default)),maximum))
        except (ValueError,TypeError):raise SearchError('search_pool_config') from None
        accounts.append({'provider':provider,'id':identifier,'quota_group':provider+':'+group,'key':key,'revision':revision,'period':period,'limit':limit})
    order={'tinyfish':0,'tavily':1,'exa':2}
    return sorted(accounts,key=lambda item:order[item['provider']])


def parse_results(data,max_sources=2):
    if not isinstance(data,dict) or not isinstance(data.get('results'),list):raise SearchError('search_invalid_response')
    sources=[];summaries=[];seen=set()
    for row in data['results'][:10]:
        if not isinstance(row,dict):continue
        url=safe_source_url(row.get('url'))
        if not url or url in seen:continue
        content=row.get('content') or row.get('snippet') or row.get('text')
        if not content and isinstance(row.get('highlights'),list):content='\n'.join(value for value in row['highlights'] if isinstance(value,str))
        if not isinstance(content,str) or not content.strip():continue
        seen.add(url);title=re.sub(r'[\x00-\x1f\x7f]+',' ',str(row.get('title') or '网页'))[:120]
        sources.append({'title':title,'url':url})
        summaries.append(title+'\n'+content.strip()[:3500])
        if len(sources)>=max_sources:break
    if not sources:raise SearchError('search_not_grounded')
    return {'summary':'\n\n'.join(summaries)[:12000],'sources':sources,'searched_at':datetime.now(timezone.utc).isoformat(timespec='seconds')}


class SearchPool:
    def __init__(self,folder):
        self.folder=Path(folder);self.credentials=self.folder/'search-accounts.json'
        self.state=SearchState(self.folder/'search-state.sqlite3')
        self.last_attempts=[]

    def _accounts(self,config):
        return account_rows(load_credentials(self.credentials))

    def status(self,config):
        rows=self._accounts(config)
        return self.state.status(rows)

    async def _execute(self,account,query,config,timeout):
        provider=account['provider']
        if provider=='tinyfish':kwargs={'params':{'query':query},'headers':{'X-API-Key':account['key']}};method='GET'
        elif provider=='tavily':
            kwargs={'json':{'query':query,'search_depth':'basic','auto_parameters':False,'max_results':3,
                'include_answer':False,'include_raw_content':False,'include_images':False},'headers':{'Authorization':'Bearer '+account['key']}};method='POST'
        else:
            kwargs={'json':{'query':query,'type':'auto','numResults':3,'contents':{'highlights':True}},'headers':{'x-api-key':account['key']}};method='POST'
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout,connect=min(timeout,5)),trust_env=False) as client:
            async with client.request(method,ENDPOINTS[provider],allow_redirects=False,**kwargs) as response:
                if response.status!=200:
                    try:retry=float(response.headers.get('Retry-After',60))
                    except ValueError:retry=60
                    raise http_failure(response.status,retry)
                raw=bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    raw.extend(chunk)
                    if len(raw)>1024**2:raise SearchError('search_size_limit')
        try:data=json.loads(raw)
        except (ValueError,UnicodeError):raise SearchError('search_invalid_response') from None
        return parse_results(data,int(config.get('max_sources',2)))

    async def search(self,query,config):
        query=str(query).strip()
        if not query:raise SearchError('search_empty_query')
        if len(query)>int(config.get('max_query_chars',800)):raise SearchError('search_query_limit')
        accounts=await asyncio.to_thread(self._accounts,config)
        started=time.monotonic();attempts=[]
        maximum=max(1,min(int(config.get('max_attempts',4)),4))
        total=float(config.get('timeout_seconds',45));last=None
        async with asyncio.timeout(total):
            for account in accounts:
                if len(attempts)>=maximum:break
                if not await asyncio.to_thread(self.state.reserve,account):continue
                attempts.append({'provider':account['provider'],'id':account['id']})
                remaining=total-(time.monotonic()-started)
                if remaining<=0:break
                timeout=min(float(config.get('provider_timeout_seconds',8)),remaining)
                try:
                    result=await self._execute(account,query,config,timeout)
                    attempts[-1]['result']='success';self.last_attempts=attempts
                    return result
                except asyncio.CancelledError:raise
                except Exception as error:
                    reason=error.reason if isinstance(error,SearchError) else 'unavailable'
                    attempts[-1]['result']=reason
                    await asyncio.to_thread(self.state.fail,account,reason,getattr(error,'retry_after',0))
                    last=error
                    if reason=='bad_query':break
        self.last_attempts=attempts
        raise SearchError('search_pool_exhausted') from None
