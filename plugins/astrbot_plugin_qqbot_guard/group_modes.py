"""Persistent per-group reply preference; no message contents or model calls."""
import os
from pathlib import Path
import sqlite3
import time


class GroupReplyModes:
    def __init__(self, path, max_groups=512, default=True):
        self.path = Path(path)
        self.max_groups = max(1, min(int(max_groups), 4096))
        self.default = bool(default)

    @staticmethod
    def _key(chat_key):
        if not isinstance(chat_key, str) or not chat_key or len(chat_key) > 200:
            raise ValueError('invalid_group')
        return chat_key

    def get(self, chat_key):
        key = self._key(chat_key)
        if not self.path.exists():
            return self.default
        db = sqlite3.connect('file:'+str(self.path)+'?mode=ro', uri=True, timeout=3)
        try:
            row = db.execute('SELECT enabled FROM reply_modes WHERE chat_key=?', (key,)).fetchone()
            return self.default if row is None else bool(row[0])
        finally:
            db.close()

    def set(self, chat_key, enabled):
        key = self._key(chat_key)
        if type(enabled) is not bool:
            raise ValueError('invalid_mode')
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=3)
        try:
            os.chmod(self.path, 0o600)
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS reply_modes (chat_key TEXT PRIMARY KEY, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), updated_at REAL NOT NULL)')
            exists = db.execute('SELECT 1 FROM reply_modes WHERE chat_key=?', (key,)).fetchone()
            if exists is None and db.execute('SELECT COUNT(*) FROM reply_modes').fetchone()[0] >= self.max_groups:
                raise ValueError('group_mode_limit')
            db.execute('INSERT INTO reply_modes VALUES (?,?,?) ON CONFLICT(chat_key) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at',
                       (key, int(enabled), time.time()))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
