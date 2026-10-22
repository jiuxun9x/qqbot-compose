"""Bounded private drawing state and last-image cache; no account credentials."""
import base64
import hashlib
import os
from pathlib import Path
import sqlite3
import time
import uuid


class DrawingStore:
    def __init__(self, folder, max_sessions=32, ttl=86400):
        self.folder = Path(folder)
        self.max_sessions, self.ttl = max(1, int(max_sessions)), max(1, float(ttl))

    @staticmethod
    def _key(origin):
        return hashlib.sha256(origin.encode()).hexdigest()

    def _connect(self):
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        db_path = self.folder / 'drawings.sqlite3'
        db = sqlite3.connect(db_path, timeout=3)
        os.chmod(db_path, 0o600)
        db.row_factory = sqlite3.Row
        db.execute('''CREATE TABLE IF NOT EXISTS drawings (
            origin TEXT PRIMARY KEY, job_id TEXT NOT NULL, prompt TEXT NOT NULL,
            status TEXT NOT NULL, started REAL NOT NULL, updated REAL NOT NULL,
            cached_file TEXT, cached_prompt TEXT, cached_at REAL, message_id TEXT,
            conversation_id TEXT)''')
        return db

    def _unlink(self, name):
        # Only our UUID-named cache files can be removed, never caller paths.
        if name and len(name) == 36 and name.endswith('.img') and all(c in '0123456789abcdef' for c in name[:-4]):
            (self.folder / name).unlink(missing_ok=True)

    def _prune(self, db):
        rows = db.execute('SELECT origin,cached_file,updated FROM drawings ORDER BY updated DESC').fetchall()
        for i, row in enumerate(rows):
            if i >= self.max_sessions or row['updated'] < time.time() - self.ttl:
                db.execute('DELETE FROM drawings WHERE origin=?', (row['origin'],))
                self._unlink(row['cached_file'])

    def get(self, origin):
        if not (self.folder / 'drawings.sqlite3').exists():
            return None
        with self._connect() as db:
            self._prune(db)
            row = db.execute('SELECT * FROM drawings WHERE origin=?', (self._key(origin),)).fetchone()
            return dict(row) if row else None

    def begin(self, origin, prompt, conversation_id=None):
        now, job_id = time.time(), uuid.uuid4().hex
        with self._connect() as db:
            db.execute('''INSERT INTO drawings(origin,job_id,prompt,status,started,updated,conversation_id)
                VALUES(?,?,?,'generating',?,?,?) ON CONFLICT(origin) DO UPDATE SET
                job_id=excluded.job_id,prompt=excluded.prompt,status=excluded.status,
                started=excluded.started,updated=excluded.updated,conversation_id=excluded.conversation_id''',
                (self._key(origin), job_id, prompt, now, now, conversation_id))
            self._prune(db)
        return self.get(origin)

    def update(self, origin, job_id, status, message_id=None):
        with self._connect() as db:
            db.execute('UPDATE drawings SET status=?,updated=?,message_id=? WHERE origin=? AND job_id=?',
                       (status, time.time(), str(message_id) if message_id else None, self._key(origin), job_id))

    def save_image(self, origin, job_id, encoded):
        data = base64.b64decode(encoded, validate=True)
        name = uuid.uuid4().hex + '.img'
        path = self.folder / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as file:
            file.write(data)
        with self._connect() as db:
            row = db.execute('SELECT cached_file,prompt FROM drawings WHERE origin=? AND job_id=?',
                             (self._key(origin), job_id)).fetchone()
            if not row:
                self._unlink(name)
                return False
            db.execute('UPDATE drawings SET cached_file=?,cached_prompt=?,cached_at=?,status=?,updated=? '
                       'WHERE origin=? AND job_id=?',
                       (name, row['prompt'], time.time(), 'sending', time.time(), self._key(origin), job_id))
            self._unlink(row['cached_file'])
        return True

    def cached_image(self, origin):
        row = self.get(origin)
        if not row or not row['cached_file'] or (row['cached_at'] or 0) < time.time() - self.ttl:
            return None
        path = self.folder / row['cached_file']
        if not path.is_file() or path.stat().st_size > 12 * 1024**2:
            return None
        return base64.b64encode(path.read_bytes()).decode()

    def forget(self, origin):
        if not (self.folder / 'drawings.sqlite3').exists():
            return
        with self._connect() as db:
            row = db.execute('SELECT cached_file FROM drawings WHERE origin=?', (self._key(origin),)).fetchone()
            db.execute('DELETE FROM drawings WHERE origin=?', (self._key(origin),))
            if row:
                self._unlink(row['cached_file'])
