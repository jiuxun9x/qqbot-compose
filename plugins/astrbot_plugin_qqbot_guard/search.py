"""Search intent, source hygiene and user-facing error helpers."""
import asyncio
from datetime import datetime,timezone
import ipaddress
import json
import os
import re
import unicodedata
from urllib.parse import urlsplit
import uuid

import aiohttp


class SearchError(ValueError):
    def __init__(self,code,reason='unavailable',retry_after=0):
        self.code,self.reason,self.retry_after=code,reason,retry_after
        super().__init__(code)


def http_failure(status,retry_after=0):
    reason='auth' if status==401 else 'quota' if status in (402,432,433) else 'rate_limit' if status==429 else 'bad_query' if status in (400,422) else 'unavailable'
    return SearchError('search_provider_failed',reason,retry_after)


def source_preference(text):
    """Only clear current requests bypass the small model's optional flag."""
    text=unicodedata.normalize('NFKC',str(text or '')).strip()
    if re.search(r'(?:不要|不用|不必|别|无需|不带|不附).{0,8}(?:来源|出处|链接|网址)|(?:来源|出处|链接|网址).{0,4}(?:不要|不用|别发)',text):return False
    if re.search(r'/(?:来源|sources)(?=$|\s)|(?:给|发|附|带|列|提供).{0,6}(?:来源|出处|链接|网址)|(?:来源|出处|链接|网址).{0,4}(?:呢|哪|什么|发我|给我|链接)|(?:从哪|哪里).{0,5}(?:查|搜|看|得到)|官网(?:地址|链接|网址)',text,re.I):return True
    return None


def explicit_sources(text):
    text=unicodedata.normalize('NFKC',str(text or '')).strip()
    text=re.sub(r'^(?:小助手|小助手)[\s,:：]*','',text)
    return bool(re.fullmatch(r'/(?:来源|sources)\s*',text,re.I))


def hide_source_links(text,sources=()):
    pieces=re.split(r'(```.*?```)',text,flags=re.S)
    for i in range(0,len(pieces),2):
        part=pieces[i]
        part=re.sub(r'\\href\{https?://[^}]+\}\{([^}]*)\}',r'\1',part)
        part=re.sub(r'\\url\{https?://[^}]+\}','',part)
        part=re.sub(r'\[([^]\n]+)\]\(https?://[^\s)]+\)',r'\1',part)
        part=re.sub(r'^\s*\[[^]\n]+\]:\s*https?://\S+.*$','',part,flags=re.M)
        part=re.sub(r'https?://[^\s<>\]）}，。；！？]+','',part)
        pieces[i]=part
    text=''.join(pieces)
    for item in sources:text=text.replace(item['url'],'')
    return text.strip()


def explicit_search_query(text):
    text=unicodedata.normalize('NFKC',str(text or '')).strip()
    text=re.sub(r'^(?:小助手|小助手)[\s,:：]*','',text)
    match=re.match(r'^/(?:搜索|联网|search)(?=$|\s|:)\s*:?\s*(.*)$',text,re.I|re.S)
    return match.group(1).strip() if match else None


def search_hint(text):
    # Only causes the small model to inspect intent; never authorizes a search.
    return bool(re.search(r'搜|查|最新|最近|今天|目前|实时|现在|现价|天气|新闻|比分|search|look\s*up',str(text),re.I))


def safe_source_url(value):
    if not isinstance(value,str) or not value or len(value)>2048 or any(c.isspace() or ord(c)<32 for c in value):return None
    try:
        parsed=urlsplit(value);host=parsed.hostname or '';port=parsed.port
        if parsed.scheme not in ('http','https') or parsed.username is not None or parsed.password is not None or port not in (None,80,443):return None
        if not host or '.' not in host or host.lower().endswith(('.local','.localhost','.internal','.invalid')):return None
        try:
            if not ipaddress.ip_address(host).is_global:return None
        except ValueError:pass
    except ValueError:return None
    return value






def sources_text(result):
    return '来源：\n'+'\n'.join(str(index)+'. '+source['title']+'\n'+source['url'] for index,source in enumerate(result['sources'],1))


def append_sources(text,result):
    suffix=sources_text(result)
    return text if suffix in text else text.rstrip()+'\n\n'+suffix




def user_error(code):
    if code=='search_empty_query':return '用 /搜索 加上你想查的内容，例如：/搜索 今天合肥天气。'
    if code=='search_query_limit':return '搜索问题有些长，请压缩到 800 字以内。'
    return '这次联网搜索没有成功取得可靠来源，请稍后再试。'
