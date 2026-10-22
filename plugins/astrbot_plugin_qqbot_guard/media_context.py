"""Recent images tied to actual sender/group/message; no cross-private lookup."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
import uuid


class MediaContextStore:
    def __init__(self, folder, max_sessions=64, ttl=300, max_bytes=256*1024**2):
        self.folder=Path(folder)
        self.path=self.folder/'media.sqlite3'
        self.max_sessions=max_sessions
        self.ttl=ttl
        self.max_bytes=max_bytes

    @staticmethod
    def key(origin):
        return hashlib.sha256(str(origin).encode()).hexdigest()

    def _connect(self,timeout=3):
        self.folder.mkdir(parents=True,mode=0o700,exist_ok=True)
        db=sqlite3.connect(self.path,timeout=timeout)
        self.path.chmod(0o600)
        db.execute('CREATE TABLE IF NOT EXISTS media (origin TEXT PRIMARY KEY, scope TEXT NOT NULL, cid TEXT, payload TEXT NOT NULL, updated REAL NOT NULL)')
        return db

    def _prune(self):
        now=time.time()
        for folder in self.folder.iterdir() if self.folder.exists() else ():
            # 30 minutes exceeds association TTL + 660s queue + 600s answer.
            if folder.is_dir() and not folder.is_symlink() and len(folder.name)==32 and now-folder.stat().st_mtime>1800:
                shutil.rmtree(folder)
        return sum(p.stat().st_size for p in self.folder.rglob('*.jpg') if p.is_file()) if self.folder.exists() else 0

    async def prepare(self, paths):
        self.folder.mkdir(parents=True,mode=0o700,exist_ok=True)
        if await asyncio.to_thread(self._prune)>=self.max_bytes:
            raise ValueError('image_cache_full')
        folder=self.folder/uuid.uuid4().hex
        folder.mkdir(mode=0o700)
        process=None
        try:
            command=[sys.executable,'-I',str(Path(__file__).with_name('media_worker.py')),json.dumps(paths),str(folder)]
            process=await asyncio.create_subprocess_exec(*command,stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,
                env={'PATH':os.defpath,'LANG':'C.UTF-8','OMP_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1'})
            output,_=await asyncio.wait_for(process.communicate(),timeout=12)
            if process.returncode or len(output)>32768:
                raise ValueError('image_decode_failed')
            result=json.loads(output)
            if not result.get('ok'):
                raise ValueError('image_decode_failed')
            media=result['media']
            if not 1<=len(media['frames'])<=6 or any(not self.valid_path(path) for path in media['frames']):
                raise ValueError('image_decode_failed')
            if await asyncio.to_thread(self._prune)>self.max_bytes:
                raise ValueError('image_cache_full')
            return media
        except BaseException:
            if process is not None and process.returncode is None:
                try:process.kill()
                except ProcessLookupError:pass
                await process.wait()
            shutil.rmtree(folder,ignore_errors=True)
            raise

    def valid_path(self, value):
        path=Path(value)
        return (not path.is_symlink() and path.is_file() and path.suffix=='.jpg'
                and path.resolve().is_relative_to(self.folder.resolve()))

    def put(self, origin, scope, cid, identity, media=None, error=None):
        now=time.time()
        data={'media':media,'error':error,'author':identity,'created':now}
        db=self._connect()
        try:
            with db:
                db.execute('DELETE FROM media WHERE updated<?',(time.time()-self.ttl,))
                db.execute('INSERT INTO media VALUES (?,?,?,?,?) ON CONFLICT(origin) DO UPDATE SET scope=excluded.scope,cid=excluded.cid,payload=excluded.payload,updated=excluded.updated',
                           (self.key(origin),scope,cid,json.dumps(data,ensure_ascii=False),now))
                db.execute('DELETE FROM media WHERE origin NOT IN (SELECT origin FROM media ORDER BY updated DESC LIMIT ?)',(self.max_sessions,))
        finally:
            db.close()

    def candidate(self, origin, scope, cid, *, group=False, validate_cid=True):
        if not self.path.exists():return None
        db=self._connect(timeout=0.05 if not validate_cid else 3)
        try:
            row=db.execute('SELECT cid,payload,updated,scope FROM media WHERE origin=?',(self.key(origin),)).fetchone()
            if row and row[3]==scope and row[2]>=time.time()-self.ttl and (not validate_cid or row[0] is None or row[0]==cid):
                if validate_cid and row[0] is None and cid is not None:
                    with db:db.execute('UPDATE media SET cid=? WHERE origin=?',(cid,self.key(origin)))
                data=json.loads(row[1]);data['source']='own_recent_image';data['conversation_id']=row[0]
            elif group:
                rows=list(db.execute('SELECT payload FROM media WHERE scope=? AND origin!=? AND updated>=? ORDER BY updated DESC LIMIT 3',
                                     (scope,self.key(origin),time.time()-90)))
                if len(rows)>1:return {'ambiguous':True,'candidate_count':len(rows)}
                if not rows:return None
                data=json.loads(rows[0][0]);data['source']='same_group_recent_image'
            else:
                return None
            if data.get('media') and not all(self.valid_path(path) for path in data['media']['frames']):
                data.update(media=None,error='image_expired')
            return data
        finally:
            db.close()

    def bind(self, origin, created, cid):
        if cid is None or not self.path.exists():return
        db=self._connect()
        try:
            with db:db.execute('UPDATE media SET cid=? WHERE origin=? AND updated=? AND cid IS NULL',
                              (cid,self.key(origin),created))
        finally:db.close()

    def forget(self, origin):
        if self.path.exists():
            db=self._connect()
            try:
                with db:db.execute('DELETE FROM media WHERE origin=?',(self.key(origin),))
            finally:db.close()


def describe(data):
    if not data:return None
    if data.get('ambiguous'):return {'ambiguous':True,'candidate_count':data['candidate_count']}
    result={key:data[key] for key in ('source','author','created','error') if key in data}
    media=data.get('media') or {}
    result['images']=[{**{key:value for key,value in item.items() if key!='samples'},
                       'samples':[{key:value for key,value in sample.items() if key!='path'} for sample in item['samples']]}
                      for item in media.get('items',[])]
    return result
