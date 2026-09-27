"""Durable execution state and append-only audit events, separate from UI caches.

Every order intent is committed before its network request. SQLite is used locally;
the Vercel adapter uses the existing Postgres connection. Errors never become empty
state. A renewable account lease serializes symbols, processes and cron ticks.
"""
import contextlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path


class RuntimeStore:
    def __init__(self, account, path=None, pg=None):
        self.account = account
        self.pg = pg
        self.owner = None
        if pg is None:
            path = Path(path or os.environ.get('BOT_RUNTIME_DB', Path(__file__).parent / 'data' / 'runtime.db'))
            path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(path, timeout=15)
            self.connection.execute('PRAGMA journal_mode=WAL')
            self.connection.execute('PRAGMA synchronous=FULL')
            self.prefix = ''
            self.placeholder = '?'
        else:
            self.connection = pg
            self.prefix = 'bybit_bot.'
            self.placeholder = '%s'
        with self.transaction() as cur:
            cur.execute(f'CREATE TABLE IF NOT EXISTS {self.prefix}runtime_state (account TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            cur.execute(f'CREATE TABLE IF NOT EXISTS {self.prefix}audit_events (event_id TEXT PRIMARY KEY, account TEXT NOT NULL, ts_ms BIGINT NOT NULL, payload TEXT NOT NULL)')
            cur.execute(f'CREATE TABLE IF NOT EXISTS {self.prefix}execution_leases (account TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_ms BIGINT NOT NULL)')

    @contextlib.contextmanager
    def transaction(self):
        cur = self.connection.cursor()
        try:
            cur.execute('BEGIN IMMEDIATE' if self.pg is None else 'BEGIN')
            yield cur
            cur.execute('COMMIT')
        except Exception:
            cur.execute('ROLLBACK')
            raise
        finally:
            cur.close()

    def sql(self, text):
        return text.replace('?', self.placeholder)

    def acquire(self, ttl_ms=600000):
        owner = uuid.uuid4().hex
        now = int(time.time()*1000)
        with self.transaction() as c:
            c.execute(self.sql(f'INSERT INTO {self.prefix}execution_leases(account,owner,expires_ms) VALUES(?,?,?) ON CONFLICT(account) DO UPDATE SET owner=excluded.owner, expires_ms=excluded.expires_ms WHERE {self.prefix}execution_leases.expires_ms < ?'), (self.account, owner, now+ttl_ms, now))
            c.execute(self.sql(f'SELECT owner FROM {self.prefix}execution_leases WHERE account=?'), (self.account,))
            if c.fetchone()[0] != owner:
                return False
        self.owner = owner
        return True

    def _assert_owner(self, cur):
        cur.execute(self.sql(f'SELECT owner,expires_ms FROM {self.prefix}execution_leases WHERE account=?'), (self.account,))
        row = cur.fetchone()
        if not row or row[0] != self.owner or row[1] <= int(time.time()*1000):
            raise RuntimeError('Execution lease lost; refusing state mutation/order submission')

    def renew(self, ttl_ms=600000):
        with self.transaction() as c:
            self._assert_owner(c)
            c.execute(self.sql(f'UPDATE {self.prefix}execution_leases SET expires_ms=? WHERE account=? AND owner=?'), (int(time.time()*1000)+ttl_ms, self.account, self.owner))

    def release(self):
        if self.owner:
            with self.transaction() as c:
                c.execute(self.sql(f'DELETE FROM {self.prefix}execution_leases WHERE account=? AND owner=?'), (self.account,self.owner))
            self.owner = None

    def load(self):
        with self.transaction() as c:
            self._assert_owner(c)
            c.execute(self.sql(f'SELECT payload FROM {self.prefix}runtime_state WHERE account=?'), (self.account,))
            row = c.fetchone()
        state = json.loads(row[0]) if row else {'schema': 1, 'symbols': {}, 'orders': {}, 'risk': {}}
        if state.get('schema') != 1:
            raise RuntimeError('Unsupported runtime state schema; explicit migration required')
        return state

    def save(self, state, events=()):
        payload = json.dumps(state, ensure_ascii=False, allow_nan=False)
        with self.transaction() as c:
            self._assert_owner(c)
            for event in events:
                c.execute(self.sql(f'INSERT INTO {self.prefix}audit_events(event_id,account,ts_ms,payload) VALUES(?,?,?,?) ON CONFLICT(event_id) DO NOTHING'), (self.account+':'+event['event_id'],self.account,int(event['ts_ms']),json.dumps(event,ensure_ascii=False,allow_nan=False)))
            c.execute(self.sql(f'INSERT INTO {self.prefix}runtime_state(account,payload) VALUES(?,?) ON CONFLICT(account) DO UPDATE SET payload=excluded.payload'), (self.account,payload))

    def close(self):
        self.release()
        self.connection.close()
