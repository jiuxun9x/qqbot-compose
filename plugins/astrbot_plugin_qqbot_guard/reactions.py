"""Bounded, group-scoped records of NapCat message reactions, without LLM calls."""
from collections import OrderedDict, deque
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import unicodedata

from .identity import reference


NOTICE_TYPE = 'group_msg_emoji_like'
REACTION_PROMPT = (
    '群表情回应是独立的互动行为，不是新的聊天发言，也不代表原消息作者说了表情描述。'
    '按 actor、target 分清回应者和被回应消息作者，使用稳定成员与消息标识关联；未知身份不得猜成当前提问者。'
    'action=remove 表示撤销，unknown 表示动作未确认；count 只是通知携带的数量，不能当作总人数或当前总点赞数。'
    '表情含义结合原消息和当前问题理解，不凭一个表情断定某人的情绪或观点。'
    'target.resolved=false 时没有核验原消息内容，不能用附近另一条消息替代。'
    '表情 ID、内部标识和这些元数据不要直接输出；仅在当前话题相关时自然使用，不主动逐条播报。'
    '当前记录中的名称优先于历史里“不认识”的描述；缺少名称时只说暂时没认出来，'
    '不要编造或谈论后台、字典、解析、模型等内部原因。'
    'actor.is_current_speaker 才表示当前说话的人点的表情；不能把所有回应都说成“你贴的”。'
    '明确引用时只讨论 observation 指定消息上的 events；没有收到匹配事件不等于它没有表情。'
)

# Public ID/name facts from NapCat's bundled face_config.json; no image downloads.
_NAMES = json.loads(Path(__file__).with_name('face_names.json').read_text())
QQ_FACES, UNICODE_FACES = _NAMES['qq'], _NAMES['unicode']


def identifier(value, *, signed=False, allow_zero=False):
    if isinstance(value, bool) or value is None:
        return None
    value = str(value)
    digits = value[1:] if signed and value.startswith('-') else value
    if not digits.isascii() or not digits.isdigit() or len(digits) > 20 or not allow_zero and not int(digits):
        return None
    return str(int(value))


def emoji_label(emoji_id):
    if emoji_id in QQ_FACES:
        return QQ_FACES[emoji_id]
    value = int(emoji_id)
    if emoji_id in UNICODE_FACES:
        return chr(value)+' '+UNICODE_FACES[emoji_id]
    if 0x1F000 <= value <= 0x1FAFF or 0x2600 <= value <= 0x27BF:
        glyph = chr(value)
        return glyph + ' ' + unicodedata.name(glyph, '未知 Unicode 表情')
    return '未知表情'


def parse_notice(raw):
    if not hasattr(raw, 'get') or raw.get('post_type') != 'notice' or raw.get('notice_type') != NOTICE_TYPE:
        return None
    group = identifier(raw.get('group_id'))
    message = identifier(raw.get('message_id'), signed=True)
    if not group or not message:
        return None
    actor = identifier(raw.get('user_id'))
    action = ('add' if raw['is_add'] else 'remove') if isinstance(raw.get('is_add'), bool) else {
        'add': 'add', 'remove': 'remove'}.get(raw.get('sub_type'), 'unknown')
    likes = raw.get('likes')
    if not isinstance(likes, list):
        return None
    emojis, seen = [], set()
    for item in likes[:16]:
        if not isinstance(item, dict):
            continue
        emoji_id = identifier(item.get('emoji_id'),allow_zero=True)
        if emoji_id is None or emoji_id in seen or len(emoji_id) > 10:
            continue
        seen.add(emoji_id)
        count = item.get('count')
        count = min(count, 100000) if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None
        emojis.append({'emoji_id': emoji_id, 'label': emoji_label(emoji_id), 'count': count})
    if not emojis:
        return None
    stamp = raw.get('time')
    stamp = stamp if isinstance(stamp, int) and not isinstance(stamp, bool) else int(time.time())
    canonical = {'group': group, 'message': message, 'actor': actor, 'action': action,
                 'emojis': sorted(emojis, key=lambda x: x['emoji_id']), 'time': stamp}
    return canonical | {'emojis': emojis, 'event_ref': hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()}


def target_from_message(data, notice, scope, self_id):
    """Only accept a get_msg result with the exact group and message IDs."""
    if not isinstance(data, dict) or data.get('message_type') != 'group':
        return None
    if identifier(data.get('group_id')) != notice['group'] or identifier(data.get('message_id'), signed=True) != notice['message']:
        return None
    sender = data.get('sender')
    if not isinstance(sender, dict) or not isinstance(data.get('message'), list):
        return None
    sender_id = identifier(sender.get('user_id'))
    if not sender_id:
        return None
    text = []
    labels = {'image': '[图片]', 'mface': '[表情包]', 'record': '[语音]',
              'video': '[视频]', 'file': '[文件]', 'face': '[QQ 表情]'}
    for item in data['message'][:64]:
        if not isinstance(item, dict):
            continue
        part = item.get('data') or {}
        if item.get('type') == 'text' and isinstance(part, dict) and isinstance(part.get('text'), str):
            text.append(part['text'][:600])
        elif item.get('type') in labels:
            text.append(labels[item['type']])
        elif item.get('type') == 'at':
            text.append('[@成员]')
    return {'message_ref': reference(scope, notice['message'], '消息'), 'resolved': True,
            'speaker_ref': reference(scope, sender_id), 'speaker_name': str(sender.get('card') or sender.get('nickname') or '群成员')[:40],
            'is_bot': sender_id == str(self_id), 'text': ''.join(text)[:600]}


class ReactionLimits:
    def __init__(self):
        self.seen = OrderedDict()
        self.lookups = deque()

    def claim(self, scope, event_ref):
        now = time.monotonic()
        while self.seen and (now - next(iter(self.seen.values())) > 60 or len(self.seen) >= 512):
            self.seen.popitem(last=False)
        key = (scope, event_ref)
        if key in self.seen:
            return False
        self.seen[key] = now
        return True

    def allow_lookup(self):
        now = time.monotonic()
        while self.lookups and now - self.lookups[0] > 60:
            self.lookups.popleft()
        if len(self.lookups) >= 12:
            return False
        self.lookups.append(now)
        return True


class ReactionStore:
    def __init__(self, path, *, ttl=1800, max_groups=64, per_group=32):
        self.path = Path(path)
        self.ttl = max(60, int(ttl))
        self.max_groups = max(1, min(int(max_groups), 128))
        self.per_group = max(1, min(int(per_group), 64))

    def record(self, chat_key, event_ref, payload):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        now = time.time()
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded) > 8192:
            return False
        db = sqlite3.connect(self.path, timeout=3)
        try:
            os.chmod(self.path, 0o600)
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS reactions (id INTEGER PRIMARY KEY, chat_key TEXT NOT NULL, event_ref TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL, UNIQUE(chat_key,event_ref))')
                db.execute('CREATE INDEX IF NOT EXISTS reaction_chat_id ON reactions(chat_key,id)')
                db.execute('DELETE FROM reactions WHERE created < ?', (now-self.ttl,))
                inserted = db.execute('INSERT OR IGNORE INTO reactions(chat_key,event_ref,payload,created) VALUES(?,?,?,?)',
                                     (chat_key, event_ref, encoded, now)).rowcount == 1
                db.execute('DELETE FROM reactions WHERE chat_key=? AND id NOT IN (SELECT id FROM reactions WHERE chat_key=? ORDER BY id DESC LIMIT ?)',
                           (chat_key, chat_key, self.per_group))
                stale = [row[0] for row in db.execute('SELECT chat_key FROM reactions GROUP BY chat_key ORDER BY MAX(created) DESC, MAX(id) DESC LIMIT -1 OFFSET ?', (self.max_groups,))]
                for key in stale:
                    db.execute('DELETE FROM reactions WHERE chat_key=?', (key,))
            return inserted
        finally:
            db.close()

    def recent(self, chat_key, limit=16):
        if not self.path.exists():
            return []
        now = time.time()
        db = sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True, timeout=3)
        try:
            rows = db.execute('SELECT payload,created FROM reactions WHERE chat_key=? AND created>=? ORDER BY id DESC LIMIT ?',
                              (chat_key, now-self.ttl, max(1, min(int(limit), self.per_group)))).fetchall()
            result = []
            for payload, created in reversed(rows):
                row = json.loads(payload) | {'age_seconds': max(0, round(now-created))}
                # Refresh names for already received events; never rewrite chat data.
                for item in row.get('emojis', []):
                    item['label'] = emoji_label(item['emoji_id'])
                result.append(row)
            return result
        finally:
            db.close()
