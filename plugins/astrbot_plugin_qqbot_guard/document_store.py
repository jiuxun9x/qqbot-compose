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

    def put(self, origin, cid, documents=None, error=None):
        payload = json.dumps({'documents': documents or [], 'error': error}, ensure_ascii=False)
        if len(payload.encode()) > 256*1024:
            raise ValueError('document_cache_limit')
        db = self._connect()
        try:
            with db:
                db.execute('DELETE FROM documents WHERE updated<?', (time.time()-self.ttl,))
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
