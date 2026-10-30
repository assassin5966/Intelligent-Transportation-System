"""越线事件持久队列：先落盘，再向支持幂等的后端投递。"""
import asyncio
from contextlib import contextmanager
import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

import httpx

from ..common.config import settings
from ..common.logger import logger


class EventOutbox:
    def __init__(self, path: str, backend_url: str, transport=None):
        self.path = Path(path)
        self.backend_url = backend_url.rstrip('/')
        self._transport = transport
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self._pending = 0
        self._last_error: Optional[str] = None
        self._storage_error: Optional[str] = None
        self._read_error: Optional[str] = None
        self._coverage_errors: dict[str, str] = {}
        self._last_success: Optional[float] = None

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.execute('PRAGMA synchronous=FULL')
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('''CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created REAL NOT NULL,
                device_id TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0
            )''')
            columns = {row[1] for row in conn.execute('PRAGMA table_info(events)')}
            if 'device_id' not in columns:
                conn.execute("ALTER TABLE events ADD COLUMN device_id TEXT NOT NULL DEFAULT ''")
                conn.execute("UPDATE events SET device_id=json_extract(payload,'$.device_id')")
            conn.execute('CREATE INDEX IF NOT EXISTS events_device ON events(device_id)')
            conn.execute('''CREATE TABLE IF NOT EXISTS event_journal (
                event_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, payload TEXT NOT NULL,
                evidence TEXT NOT NULL, created REAL NOT NULL,
                delivered REAL, backend_applied INTEGER
            )''')
            conn.execute('CREATE INDEX IF NOT EXISTS journal_device_created ON event_journal(device_id,created)')
            conn.execute('''CREATE TABLE IF NOT EXISTS coverage_checkpoints (
                device_id TEXT PRIMARY KEY, stream_epoch TEXT NOT NULL,
                state TEXT NOT NULL, observed REAL NOT NULL
            )''')
            conn.execute('''CREATE TABLE IF NOT EXISTS coverage_gaps (
                id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL,
                stream_epoch TEXT NOT NULL, start REAL NOT NULL, end REAL, reason TEXT NOT NULL
            )''')
            conn.execute('CREATE INDEX IF NOT EXISTS coverage_gaps_device ON coverage_gaps(device_id,id)')
            # 平滑升级旧队列；已有未投递事件不能因表结构变更丢失。
            conn.execute("""INSERT OR IGNORE INTO event_journal
                (event_id,device_id,payload,evidence,created)
                SELECT event_id,device_id,payload,'{}',created FROM events""")
            return conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]

    async def start(self):
        if self._task is not None:
            return
        if settings.event_delivery_protocol not in ('legacy', 'journal_v2'):
            raise ValueError('EVENT_DELIVERY_PROTOCOL must be legacy or journal_v2')
        self._pending = await asyncio.to_thread(self._initialize)
        self._task = asyncio.create_task(self._sender(), name='event-outbox')
        logger.info(f'事件持久队列已启动，待投递={self._pending}')

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _count(self):
        with self._connect() as conn:
            return conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]

    async def snapshot(self):
        oldest = None
        try:
            self._pending, oldest = await asyncio.to_thread(self._backlog_status)
        except (sqlite3.Error, OSError) as exc:
            self._read_error = str(exc)
        return {'pending': self._pending,
                'oldest_pending_age_seconds': max(0, time.time() - oldest) if oldest is not None else None,
                'storage_error': self._storage_error or self._read_error,
                'coverage_error': dict(self._coverage_errors) or None,
                'delivery_error': self._last_error, 'last_success_unix': self._last_success,
                'sender_running': self._task is not None and not self._task.done()}

    def _backlog_status(self):
        with self._connect() as conn:
            return conn.execute('SELECT COUNT(*), MIN(created) FROM events').fetchone()

    def _record_coverage(self, device_id, stream_epoch, state, observed):
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            previous = conn.execute('SELECT stream_epoch,state,observed FROM coverage_checkpoints WHERE device_id=?',
                                    (device_id,)).fetchone()
            if previous is not None and observed < previous[2]:
                raise ValueError('coverage timestamp moved backwards or checkpoint arrived out of order')
            opened = conn.execute('SELECT id,start FROM coverage_gaps WHERE device_id=? AND end IS NULL ORDER BY id DESC LIMIT 1',
                                  (device_id,)).fetchone()
            changed_epoch = previous is not None and previous[0] != stream_epoch
            incomplete = state != 'processing'
            if opened is None and (incomplete or changed_epoch):
                start = previous[2] if previous is not None else observed
                # 本地墙钟回退时保守取较小值，区间为监控采样推断，非源 PTS。
                start = min(start, observed)
                reason = state if incomplete else 'stream_epoch_changed_or_restart'
                conn.execute('INSERT INTO coverage_gaps(device_id,stream_epoch,start,reason) VALUES (?,?,?,?)',
                             (device_id, stream_epoch, start, reason))
                opened = (conn.execute('SELECT last_insert_rowid()').fetchone()[0], start)
            if opened is not None and not incomplete:
                conn.execute('UPDATE coverage_gaps SET end=? WHERE id=?', (max(observed, opened[1]), opened[0]))
            conn.execute('''INSERT INTO coverage_checkpoints(device_id,stream_epoch,state,observed) VALUES (?,?,?,?)
                ON CONFLICT(device_id) DO UPDATE SET stream_epoch=excluded.stream_epoch,
                    state=excluded.state,observed=excluded.observed''',
                         (device_id, stream_epoch, state, observed))

    async def record_coverage(self, device_id, stream_epoch, state, observed=None):
        try:
            await asyncio.to_thread(self._record_coverage, device_id, stream_epoch, state,
                                    time.time() if observed is None else observed)
            self._coverage_errors.pop(device_id, None)
        except (sqlite3.Error, OSError, ValueError) as exc:
            self._coverage_errors[device_id] = str(exc)
            logger.error(f'设备 {device_id} 覆盖缺口记录失败: {exc}')

    def _coverage_gaps(self, device_id, limit):
        with self._connect() as conn:
            rows = conn.execute('''SELECT stream_epoch,start,end,reason FROM coverage_gaps
                WHERE device_id=? ORDER BY id DESC LIMIT ?''', (device_id, limit)).fetchall()
        return [{'stream_epoch': row[0], 'start_unix': row[1], 'end_unix': row[2], 'reason': row[3]}
                for row in rows]

    async def coverage_gaps(self, device_id, limit=100):
        return await asyncio.to_thread(self._coverage_gaps, device_id, limit)

    def _insert_many(self, entries):
        with self._connect() as conn:
            inserted = 0
            for event_id, device_id, payload, evidence in entries:
                row = conn.execute('SELECT payload,evidence FROM event_journal WHERE event_id=?', (event_id,)).fetchone()
                if row is not None:
                    if row != (payload, evidence):
                        raise ValueError('outbox event_id reused with different payload or evidence')
                    continue
                created = time.time()
                conn.execute('INSERT INTO event_journal(event_id,device_id,payload,evidence,created) VALUES (?,?,?,?,?)',
                             (event_id, device_id, payload, evidence, created))
                conn.execute('INSERT INTO events(event_id,device_id,payload,created) VALUES (?,?,?,?)',
                             (event_id, device_id, payload, created))
                inserted += 1
            return inserted

    async def enqueue(self, payload: dict):
        await self.enqueue_many([payload])

    async def enqueue_many(self, payloads: list[dict], *, evidence: Optional[list[dict]] = None):
        """同帧事件一次提交；存储故障时暂停该路计数并持续报警。"""
        entries = []
        if evidence is not None and len(evidence) != len(payloads):
            raise ValueError('evidence must correspond to payloads')
        for index, payload in enumerate(payloads):
            event_id, device_id = payload.get('event_id'), payload.get('device_id')
            if not event_id or not device_id:
                raise ValueError('event_id and device_id required')
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            metadata = evidence[index] if evidence is not None else {}
            encoded_evidence = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
            entries.append((event_id, device_id, encoded, encoded_evidence))
        if not entries:
            return
        while True:
            try:
                inserted = await asyncio.to_thread(self._insert_many, entries)
                self._pending += inserted
                self._storage_error = None
                self._wake.set()
                return
            except (sqlite3.Error, OSError) as exc:
                self._storage_error = str(exc)
                logger.error(f'事件持久化失败，计数暂停、等待存储恢复: {exc}')
                await asyncio.sleep(5)

    def _next(self):
        # 同设备保持入队顺序，避免 Exit 先于重试中的 Enter 导致当前人数错算。
        with self._connect() as conn:
            return conn.execute("""SELECT e.event_id,e.payload,e.attempts,j.evidence FROM events e
                JOIN event_journal j ON j.event_id=e.event_id
                WHERE e.next_attempt<=? AND NOT EXISTS (
                  SELECT 1 FROM events p WHERE p.device_id=e.device_id AND p.rowid<e.rowid)
                ORDER BY e.rowid LIMIT 1""", (time.time(),)).fetchone()

    def _done(self, event_id, applied=None):
        with self._connect() as conn:
            removed = conn.execute('DELETE FROM events WHERE event_id=?', (event_id,)).rowcount
            if removed:
                conn.execute('UPDATE event_journal SET delivered=?,backend_applied=? WHERE event_id=?',
                             (time.time(), None if applied is None else int(applied), event_id))
            return removed

    def _retry(self, event_id, attempts):
        delay = min(60, 2 ** min(attempts, 6))
        with self._connect() as conn:
            conn.execute('UPDATE events SET attempts=?,next_attempt=? WHERE event_id=?',
                         (attempts + 1, time.time() + delay, event_id))

    async def _sender(self):
        async with httpx.AsyncClient(timeout=10, transport=self._transport) as client:
            while True:
                try:
                    row = await asyncio.to_thread(self._next)
                    self._read_error = None
                    if row is None:
                        self._wake.clear()
                        try:
                            await asyncio.wait_for(self._wake.wait(), 1)
                        except asyncio.TimeoutError:
                            pass
                        continue
                    event_id, encoded, attempts, encoded_evidence = row
                    try:
                        capability = await client.get(f'{self.backend_url}/api/events/capabilities')
                        capability.raise_for_status()
                        if capability.json().get('idempotency') != 'permanent-v1':
                            raise RuntimeError('后端未声明原子幂等能力，停止发送并重试检查')
                        v2 = settings.event_delivery_protocol == 'journal_v2'
                        if v2 and capability.json().get('ledger') != 'journal-v2':
                            raise RuntimeError('后端未启用持久账本，停止 v2 投递并重试检查')
                        payload = json.loads(encoded)
                        if v2:
                            payload['evidence'] = json.loads(encoded_evidence)
                        response = await client.post(f"{self.backend_url}/api/events{'/v2' if v2 else ''}", json=payload)
                        response.raise_for_status()
                        ack = response.json()
                        if ack.get('event_id') != event_id or (v2 and ack.get('persisted') is not True) or (not v2 and not isinstance(ack.get('applied'), bool)):
                            raise RuntimeError('后端确认缺少有效 event_id 或持久接收/应用状态')
                        removed = await asyncio.to_thread(self._done, event_id, None if v2 else ack['applied'])
                        self._pending = max(0, self._pending - removed)
                        self._last_error = None
                        self._last_success = time.time()
                    except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                        self._last_error = str(exc)
                        logger.warning(f'事件 {event_id} 投递失败，将重试: {exc}')
                        await asyncio.to_thread(self._retry, event_id, attempts)
                except (sqlite3.Error, OSError) as exc:
                    self._read_error = str(exc)
                    logger.error(f'事件队列存储异常: {exc}')
                    await asyncio.sleep(5)


event_outbox = EventOutbox(settings.event_outbox_path, settings.backend_url)
