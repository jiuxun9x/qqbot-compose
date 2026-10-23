"""Small, bounded SQLite store for public group context; never accepts DMs."""
from pathlib import Path
import json
import os
import sqlite3
import time


class GroupContextStore:
    def __init__(self, path, max_messages=20, max_chars=400, max_groups=64, ttl=1800):
        self.path = Path(path)
        self.max_messages = max(1, min(int(max_messages), 100))
        self.max_chars = max(80, min(int(max_chars), 1000))
        self.max_groups = max(1, min(int(max_groups), 128))
        self.ttl = max(60, int(ttl))

    def record(self, chat_key, sender, content, *, identity=None):
        """Atomically read the preceding context and append one public message."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        now = time.time()
        db = sqlite3.connect(self.path, timeout=3)
        try:
            os.chmod(self.path, 0o600)
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS groups (chat_key TEXT PRIMARY KEY, last_seen REAL NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, chat_key TEXT NOT NULL, sender TEXT NOT NULL, content TEXT NOT NULL, created REAL NOT NULL)")
                db.execute("CREATE INDEX IF NOT EXISTS messages_chat_id ON messages(chat_key, id)")
                db.execute("CREATE TABLE IF NOT EXISTS message_objects (message_id INTEGER PRIMARY KEY, metadata TEXT NOT NULL)")
                db.execute("DELETE FROM messages WHERE created < ?", (now - self.ttl,))
                db.execute("DELETE FROM groups WHERE last_seen < ?", (now - self.ttl,))
                db.execute("INSERT INTO groups VALUES (?, ?) ON CONFLICT(chat_key) DO UPDATE SET last_seen=excluded.last_seen", (chat_key, now))
                stale = [row[0] for row in db.execute("SELECT chat_key FROM groups ORDER BY last_seen DESC LIMIT -1 OFFSET ?", (self.max_groups,))]
                for key in stale:
                    db.execute("DELETE FROM messages WHERE chat_key=?", (key,))
                    db.execute("DELETE FROM groups WHERE chat_key=?", (key,))
                rows = list(db.execute("SELECT m.sender,m.content,m.created,o.metadata FROM messages m LEFT JOIN message_objects o ON o.message_id=m.id WHERE m.chat_key=? ORDER BY m.id DESC LIMIT ?", (chat_key, self.max_messages)))
                cursor=db.execute("INSERT INTO messages(chat_key,sender,content,created) VALUES(?,?,?,?)", (chat_key, str(sender)[:40], str(content)[:self.max_chars], now))
                safe={key:identity[key] for key in ('speaker_ref','is_bot','message_ref','in_reply_to','quoted_message_ref','quoted_speaker_ref','mentions') if identity and key in identity}
                db.execute('INSERT OR REPLACE INTO message_objects VALUES (?,?)',(cursor.lastrowid,json.dumps(safe,ensure_ascii=False)))
                db.execute("DELETE FROM messages WHERE chat_key=? AND id NOT IN (SELECT id FROM messages WHERE chat_key=? ORDER BY id DESC LIMIT ?)", (chat_key, chat_key, self.max_messages))
                db.execute('DELETE FROM message_objects WHERE message_id NOT IN (SELECT id FROM messages)')
            result=[]
            for name,text,created,metadata in reversed(rows):
                item={'speaker':name,'text':text}
                if metadata:
                    decoded=json.loads(metadata)
                    if decoded:
                        item.update(decoded)
                        item['age_seconds']=max(0,round(now-created))
                # Old records contain only nicknames; never invent their IDs.
                result.append(item)
            return result
        finally:
            db.close()

    def recent(self, chat_key):
        """Read only this group's unexpired messages, without adding a fake turn."""
        if not self.path.exists():
            return []
        now = time.time()
        db = sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True, timeout=3)
        try:
            rows = db.execute('SELECT m.sender,m.content,o.metadata FROM messages m LEFT JOIN message_objects o ON o.message_id=m.id WHERE m.chat_key=? AND m.created>=? ORDER BY m.id DESC LIMIT ?',
                              (chat_key, now-self.ttl, self.max_messages)).fetchall()
            return [dict(speaker=name, text=text, **(json.loads(metadata) if metadata else {}))
                    for name, text, metadata in reversed(rows)]
        finally:
            db.close()
