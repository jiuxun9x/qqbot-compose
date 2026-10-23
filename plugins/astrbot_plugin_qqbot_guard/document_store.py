"""Latest extracted attachment batch per conversation, with a short TTL."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time


class DocumentStore:
    def __init__(self, path, max_sessions=64, ttl=1800):
        self.path, self.max_sessions, self.ttl = Path(path), max_sessions, ttl

    @staticmethod
    def _key(origin):
        return hashlib.sha256(str(origin).encode()).hexdigest()

    def _connect(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=3)
        os.chmod(self.path, 0o600)
        db.execute('CREATE TABLE IF NOT EXISTS documents (session TEXT PRIMARY KEY, cid TEXT, payload TEXT NOT NULL, updated REAL NOT NULL)')
        return db

    def put(self, origin, cid, documents=None, error=None, *, error_id=None):
        value = {'documents': documents or [], 'error': error}
        if error:value.update(error_notified=False,error_id=error_id)
        payload = json.dumps(value, ensure_ascii=False)
        if len(payload.encode()) > 256*1024:
            raise ValueError('document_cache_limit')
        db = self._connect()
        try:
            with db:
                db.execute('DELETE FROM documents WHERE updated<?', (time.time()-self.ttl,))
                if error and error_id:
                    previous=db.execute('SELECT cid,payload FROM documents WHERE session=?',(self._key(origin),)).fetchone()
                    if previous and previous[0] in (None,cid):
                        old=json.loads(previous[1])
                        if old.get('error')==error and old.get('error_id')==error_id:
                            value['error_notified']=old.get('error_notified',True)
                            payload=json.dumps(value,ensure_ascii=False)
                db.execute('INSERT INTO documents VALUES (?,?,?,?) ON CONFLICT(session) DO UPDATE SET cid=excluded.cid,payload=excluded.payload,updated=excluded.updated',
                           (self._key(origin), cid, payload, time.time()))
                db.execute('DELETE FROM documents WHERE session NOT IN (SELECT session FROM documents ORDER BY updated DESC LIMIT ?)', (self.max_sessions,))
        finally:
            db.close()

    def get(self, origin, cid):
        if not self.path.exists():
            return None
        db = self._connect()
        try:
            row = db.execute('SELECT cid,payload,updated FROM documents WHERE session=?', (self._key(origin),)).fetchone()
            if not row or row[2] < time.time()-self.ttl or row[0] is not None and row[0] != cid:
                return None
            # A file may arrive before the framework creates its first chat.
            if row[0] is None and cid is not None:
                with db:
                    db.execute('UPDATE documents SET cid=? WHERE session=?', (cid, self._key(origin)))
            return json.loads(row[1])
        finally:
            db.close()

    def claim_error_notice(self, origin, cid, error, error_id=None):
        """Atomically acknowledge one failed batch without removing user material."""
        if not self.path.exists():return False
        db=self._connect()
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute('SELECT cid,payload,updated FROM documents WHERE session=?',(self._key(origin),)).fetchone()
                if not row or row[2]<time.time()-self.ttl or row[0] is not None and row[0]!=cid:return False
                value=json.loads(row[1])
                # Legacy failures may already have been shown many times.
                # They are context, never a reason to replay the old notice.
                if value.get('error')!=error or value.get('error_id')!=error_id or value.get('error_notified',True):return False
                value['error_notified']=True
                db.execute('UPDATE documents SET payload=? WHERE session=?',
                           (json.dumps(value,ensure_ascii=False),self._key(origin)))
                return True
        finally:
            db.close()

    def forget(self, origin):
        if self.path.exists():
            db = self._connect()
            try:
                with db:
                    db.execute('DELETE FROM documents WHERE session=?', (self._key(origin),))
            finally:
                db.close()

    def updated_at(self, origin, cid):
        if not self.path.exists():
            return 0
        db=self._connect()
        try:
            row=db.execute('SELECT cid,updated FROM documents WHERE session=?',(self._key(origin),)).fetchone()
            return row[1] if row and (row[0] is None or row[0]==cid) and row[1]>=time.time()-self.ttl else 0
        finally:
            db.close()


def material_marker(payload):
    digest = hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:20]
    return '[qqdoc:'+digest+']'


def history_contains(history, marker):
    for message in history or []:
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        content = message.get('content')
        if isinstance(content, str) and marker in content:
            return True
        if isinstance(content, list) and any(isinstance(part,dict) and part.get('type')=='text'
                and marker in str(part.get('text','')) for part in content):
            return True
    return False
