"""Small persistent search budgets/cooldowns and conversation-bound sources."""
from datetime import datetime,timedelta,timezone
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time


def quota_window(period,now):
    dt=datetime.fromtimestamp(now,timezone.utc)
    if period=='month':
        following=(dt.replace(day=28)+timedelta(days=4)).replace(day=1,hour=0,minute=0,second=0,microsecond=0)
        return dt.strftime('M:%Y-%m'),following.timestamp()
    return dt.strftime('D:%Y-%m-%d'),dt.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()+86400


class SearchState:
    def __init__(self,path,*,clock=time.time):self.path=Path(path);self.clock=clock

    @contextmanager
    def _db(self):
        self.path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        db=sqlite3.connect(self.path,timeout=3)
        os.chmod(self.path,0o600)
        db.executescript('''
            CREATE TABLE IF NOT EXISTS budgets (group_id TEXT, window TEXT, used INTEGER NOT NULL, PRIMARY KEY(group_id,window));
            CREATE TABLE IF NOT EXISTS cooldowns (target TEXT PRIMARY KEY, revision TEXT, until REAL NOT NULL, reason TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sources (scope TEXT PRIMARY KEY, payload TEXT NOT NULL, updated REAL NOT NULL);
        ''')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback();raise
        finally:db.close()

    def reserve(self,account):
        now=self.clock();window,reset=quota_window(account['period'],now)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            for target in ('account:'+account['id'],'group:'+account['quota_group']):
                row=db.execute('SELECT revision,until FROM cooldowns WHERE target=?',(target,)).fetchone()
                revision=account['revision'] if target.startswith('account:') else ''
                if row and row[0]==revision and row[1]>now:return False
            used=db.execute('SELECT used FROM budgets WHERE group_id=? AND window=?',(account['quota_group'],window)).fetchone()
            if used and used[0]>=account['limit']:return False
            db.execute('INSERT INTO budgets VALUES (?,?,1) ON CONFLICT(group_id,window) DO UPDATE SET used=used+1',(account['quota_group'],window))
            # Reserve conservatively: cancellations and uncertain failures are
            # counted too. Restarting or switching keys cannot reset a budget.
            db.execute('DELETE FROM cooldowns WHERE until<=?',(now,))
            cutoff=datetime.fromtimestamp(now-100*86400,timezone.utc)
            db.execute("DELETE FROM budgets WHERE (window LIKE 'D:%' AND window<?) OR (window LIKE 'M:%' AND window<?)",
                       (cutoff.strftime('D:%Y-%m-%d'),cutoff.strftime('M:%Y-%m')))
            return True

    def fail(self,account,reason,retry_after=0):
        now=self.clock()
        if reason=='quota':until=quota_window(account['period'],now)[1];target='group:'+account['quota_group'];revision=''
        elif reason=='rate_limit':until=now+max(60,min(float(retry_after),86400));target='group:'+account['quota_group'];revision=''
        elif reason=='auth':until=now+365*86400;target='account:'+account['id'];revision=account['revision']
        else:until=now+30;target='account:'+account['id'];revision=account['revision']
        with self._db() as db:
            db.execute('INSERT INTO cooldowns VALUES (?,?,?,?) ON CONFLICT(target) DO UPDATE SET until=CASE WHEN cooldowns.revision=excluded.revision THEN MAX(cooldowns.until,excluded.until) ELSE excluded.until END,revision=excluded.revision,reason=excluded.reason',
                       (target,revision,until,reason))

    def status(self,accounts):
        now=self.clock();rows=[]
        with self._db() as db:
            for account in accounts:
                window,reset=quota_window(account['period'],now)
                used=db.execute('SELECT used FROM budgets WHERE group_id=? AND window=?',(account['quota_group'],window)).fetchone()
                blocks=db.execute('SELECT target,revision,until,reason FROM cooldowns WHERE target IN (?,?)',
                    ('account:'+account['id'],'group:'+account['quota_group'])).fetchall()
                reasons=[(until,reason) for target,revision,until,reason in blocks if until>now and (not target.startswith('account:') or revision==account['revision'])]
                until,reason=max(reasons,default=(0,''))
                count=used[0] if used else 0
                rows.append({'provider':account['provider'],'id':account['id'],'used':count,'limit':account['limit'],
                             'available':count<account['limit'] and not reason,'reason':reason or ('local_budget' if count>=account['limit'] else ''),
                             'retry_at':until or (reset if count>=account['limit'] else 0)})
        return rows

    @staticmethod
    def _scope(origin,cid):return hashlib.sha256((str(origin)+'\0'+str(cid or '')).encode()).hexdigest()

    def put_sources(self,origin,cid,result):
        payload=json.dumps({'sources':result['sources'],'searched_at':result['searched_at']},ensure_ascii=False)
        if len(payload)>12000:raise ValueError('source_cache_limit')
        with self._db() as db:
            db.execute('DELETE FROM sources WHERE updated<?',(self.clock()-1800,))
            db.execute('INSERT INTO sources VALUES (?,?,?) ON CONFLICT(scope) DO UPDATE SET payload=excluded.payload,updated=excluded.updated',
                       (self._scope(origin,cid),payload,self.clock()))
            db.execute('DELETE FROM sources WHERE scope NOT IN (SELECT scope FROM sources ORDER BY updated DESC LIMIT 128)')

    def get_sources(self,origin,cid):
        with self._db() as db:
            row=db.execute('SELECT payload,updated FROM sources WHERE scope=?',(self._scope(origin,cid),)).fetchone()
        if row and self.clock()-row[1]<1800:return json.loads(row[0])
        return None

    def clear_sources(self,origin,cid):
        with self._db() as db:
            db.execute('DELETE FROM sources WHERE scope IN (?,?)',(self._scope(origin,cid),self._scope(origin,None)))
