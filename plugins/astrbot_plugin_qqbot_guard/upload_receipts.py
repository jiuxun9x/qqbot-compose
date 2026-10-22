"""Observe file arrivals before the OneBot adapter resolves their download URL."""
import asyncio
from collections import OrderedDict
import hashlib
import time


EVENT_NAMES = ('message.private.friend','message.private.group','message.private.other',
               'message.group.normal','message.group.anonymous','message.group.notice',
               'notice.offline_file','notice.group_upload')


def raw_files(raw):
    if not hasattr(raw, 'get'):
        return []
    if raw.get('post_type')=='notice' and raw.get('notice_type') in ('offline_file','group_upload'):
        file = raw.get('file')
        return [file] if isinstance(file,dict) else []
    if raw.get('post_type')=='message' and isinstance(raw.get('message'),list):
        return [part['data'] for part in raw['message'] if isinstance(part,dict)
                and part.get('type')=='file' and isinstance(part.get('data'),dict)]
    return []


def file_keys(raw):
    return [hashlib.sha256(str(item.get('file_id') or item.get('id') or
            (str(raw.get('message_id',''))+':'+str(item.get('name') or item.get('file','')))).encode()).hexdigest()
            for item in raw_files(raw)]


class UploadReceipts:
    def __init__(self, ttl=90, limit=64):
        self.ttl,self.limit = ttl,limit
        self.rows = OrderedDict()

    def _prune(self):
        for key,(stamp,future) in list(self.rows.items()):
            if time.monotonic()-stamp > self.ttl or future.done():
                if not future.done(): future.set_result('expired')
                del self.rows[key]

    def observe(self, origin, raw):
        self._prune()
        for file_key in file_keys(raw):
            key = (origin,file_key)
            if key in self.rows:
                continue
            if len(self.rows) >= self.limit:
                break
            self.rows[key] = (time.monotonic(),asyncio.get_running_loop().create_future())

    def finish(self, origin, raw, status='ready'):
        for key in file_keys(raw):
            item = self.rows.pop((origin,key),None)
            if item and not item[1].done(): item[1].set_result(status)

    def waiting(self, origin):
        self._prune()
        return [future for (session,_),(_,future) in self.rows.items() if session==origin]

    def cancel(self, origin=None):
        for key,(_,future) in list(self.rows.items()):
            if origin is None or key[0]==origin:
                if not future.done(): future.set_result('cancelled')
                del self.rows[key]
