"""Bounded downloads and isolated parsing for current QQ attachments only."""
import asyncio
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import time
from urllib.parse import unquote,urljoin,urlsplit
import uuid

import aiohttp
from .file_worker import SUPPORTED_TYPES


class AttachmentError(Exception):
    def __init__(self,code):
        super().__init__(code)
        self.code=code


class PublicResolver(aiohttp.abc.AbstractResolver):
    def __init__(self):self.resolver=aiohttp.resolver.DefaultResolver()
    async def resolve(self,host,port=0,family=socket.AF_INET):
        records=await self.resolver.resolve(host,port,family)
        if not records or any(not ipaddress.ip_address(row['host']).is_global for row in records):
            raise AttachmentError('unsafe_download_url')
        return records
    async def close(self):await self.resolver.close()


def validate_url(value):
    url=urlsplit(value)
    if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.port not in (None,80,443):
        raise AttachmentError('unsafe_download_url')
    try:
        if not ipaddress.ip_address(url.hostname).is_global:raise AttachmentError('unsafe_download_url')
    except ValueError:
        if url.hostname.lower()=='localhost' or url.hostname.lower().endswith(('.localhost','.local')):
            raise AttachmentError('unsafe_download_url')
    return value


class AttachmentReader:
    def __init__(self,folder,max_bytes=10*1024**2,total_chars=24000):
        self.folder=Path(folder)
        self.max_bytes=max_bytes
        self.total_chars=total_chars

    def _prune(self):
        if not self.folder.exists():return
        jobs=[]
        for path in self.folder.iterdir():
            if path.is_symlink() or not path.is_dir() or len(path.name)!=32 or any(c not in '0123456789abcdef' for c in path.name):continue
            size=sum(item.stat().st_size for item in path.iterdir() if item.is_file() and not item.is_symlink())
            jobs.append((path.stat().st_mtime,path,size))
        total=sum(item[2] for item in jobs)
        for stamp,path,size in sorted(jobs):
            if time.time()-stamp>86400 or total>100*1024**2:
                shutil.rmtree(path)
                total-=size

    async def _download(self,url,target):
        resolver=PublicResolver()
        try:return await self._download_with_resolver(url,target,resolver)
        finally:await resolver.close()

    async def _download_with_resolver(self,url,target,resolver):
        validate_url(url)
        connector=aiohttp.TCPConnector(resolver=resolver,limit=1,use_dns_cache=False)
        timeout=aiohttp.ClientTimeout(total=25,connect=8)
        async with aiohttp.ClientSession(connector=connector,timeout=timeout,trust_env=False,
                auto_decompress=False,headers={'Accept-Encoding':'identity'}) as session:
            for _ in range(4):
                validate_url(url)
                async with session.get(url,allow_redirects=False) as response:
                    if response.status in (301,302,303,307,308):
                        url=urljoin(url,response.headers.get('Location',''))
                        continue
                    if response.status!=200:raise AttachmentError('download_failed')
                    if response.content_length and response.content_length>self.max_bytes:raise AttachmentError('file_too_large')
                    size=0
                    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                    with os.fdopen(fd,'wb') as output:
                        async for chunk in response.content.iter_chunked(64*1024):
                            size+=len(chunk)
                            if size>self.max_bytes:raise AttachmentError('file_too_large')
                            output.write(chunk)
                    if not size:raise AttachmentError('empty_document')
                    return
            raise AttachmentError('download_failed')

    async def _source(self,part,target):
        # Do not invoke File.file (its getter can download without a size cap).
        url=str(getattr(part,'url','') or '')
        if url:
            await self._download(url,target)
        else:
            raw=str(getattr(part,'file_','') or '')
            if raw.startswith('file://'):raw=unquote(urlsplit(raw).path)
            path=Path(raw)
            allowed=(self.folder.resolve(),Path('/AstrBot/data/temp').resolve())
            resolved=path.resolve()
            if not raw or path.is_symlink() or not any(resolved.is_relative_to(base) for base in allowed) or not resolved.is_file():
                raise AttachmentError('invalid_attachment')
            if resolved.stat().st_size>self.max_bytes:raise AttachmentError('file_too_large')
            await asyncio.to_thread(shutil.copyfile,resolved,target)
            target.chmod(0o600)

    async def _parse(self,path,name,room,folder,max_images):
        command=[sys.executable,'-I',str(Path(__file__).with_name('file_worker.py')),str(path),name,str(room),str(folder),str(max_images)]
        env={'PATH':os.defpath,'LANG':'C.UTF-8','OMP_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1','MKL_NUM_THREADS':'1'}
        process=await asyncio.create_subprocess_exec(*command,stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,env=env)
        try:
            stdout,_=await asyncio.wait_for(process.communicate(),timeout=15)
        finally:
            if process.returncode is None:
                try:process.kill()
                except ProcessLookupError:pass
                await process.wait()
        if process.returncode or len(stdout)>512*1024:raise AttachmentError('parse_limit')
        result=json.loads(stdout)
        if not result.get('ok'):raise AttachmentError(result.get('error','parse_failed'))
        return result['document']

    async def prepare(self,parts):
        if len(parts)>2:raise AttachmentError('too_many_files')
        self.folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        await asyncio.to_thread(self._prune)
        folder=self.folder/uuid.uuid4().hex
        folder.mkdir(mode=0o700)
        documents=[]
        room=self.total_chars
        try:
            for index,(part,source) in enumerate(parts):
                name=str(getattr(part,'name','') or 'unnamed')[:120]
                extension=Path(name).suffix.lower()
                if extension not in SUPPORTED_TYPES:raise AttachmentError('unsupported_type')
                target=folder/(str(index)+extension)
                await self._source(part,target)
                image_budget=max(0,2-sum(len(item.get('images',[])) for item in documents))//(len(parts)-index)
                document=await self._parse(target,name,max(1,room//(len(parts)-index)),folder,image_budget)
                document.update(name=name,source=source)
                room=max(0,room-len(document['text']))
                documents.append(document)
                part.file_=str(target)
                # The framework will re-use this bounded local download.
                part.url=''
            await asyncio.to_thread(self._prune)
            return documents
        except BaseException:
            shutil.rmtree(folder,ignore_errors=True)
            raise


def user_error(code):
    return {
        'collector_full':'附件接收队列已满，这次没能接收，请稍后重发。',
        'unsupported_type':'这个文件格式暂不支持。可以发 PDF、DOCX、XLSX、TXT、Markdown、CSV 或常见代码文件；旧版 DOC/XLS 请先另存为新格式。',
        'file_too_large':'文件超过 10 MB，请拆分或压缩后重发。',
        'too_many_files':'一次最多读取两个文件，请分批发送。',
        'encrypted_file':'这个文件有密码保护，请先解密后重发。',
        'empty_document':'没有读到可用文字或扫描图片，请重发清晰文件或对应页的图片。',
        'parse_limit':'这个文件解析超时或内容过于复杂，请拆分后重发。',
        'archive_too_large':'文件解压后的内容过大，请拆分后重发。',
    }.get(code,'这次没能读取文件内容，请重新发送文件，或换成 PDF、DOCX、TXT 后再试。')
