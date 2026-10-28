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
            return conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]

    async def start(self):
        if self._task is not None:
            return
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
                'delivery_error': self._last_error, 'last_success_unix': self._last_success,
                'sender_running': self._task is not None and not self._task.done()}

    def _backlog_status(self):
        with self._connect() as conn:
            return conn.execute('SELECT COUNT(*), MIN(created) FROM events').fetchone()

    def _insert_many(self, entries):
        with self._connect() as conn:
            inserted = 0
            for event_id, device_id, payload in entries:
                row = conn.execute('SELECT payload FROM events WHERE event_id=?', (event_id,)).fetchone()
                if row is not None:
                    if row[0] != payload:
                        raise ValueError('outbox event_id reused with a different payload')
                    continue
                conn.execute('INSERT INTO events(event_id,device_id,payload,created) VALUES (?,?,?,?)',
                             (event_id, device_id, payload, time.time()))
                inserted += 1
            return inserted

    async def enqueue(self, payload: dict):
        await self.enqueue_many([payload])

    async def enqueue_many(self, payloads: list[dict]):
        """同帧事件一次提交；存储故障时暂停该路计数并持续报警。"""
        entries = []
        for payload in payloads:
            event_id, device_id = payload.get('event_id'), payload.get('device_id')
            if not event_id or not device_id:
                raise ValueError('event_id and device_id required')
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            entries.append((event_id, device_id, encoded))
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
            return conn.execute("""SELECT e.event_id,e.payload,e.attempts FROM events e
                WHERE e.next_attempt<=? AND NOT EXISTS (
                  SELECT 1 FROM events p WHERE p.device_id=e.device_id AND p.rowid<e.rowid)
                ORDER BY e.rowid LIMIT 1""", (time.time(),)).fetchone()

    def _done(self, event_id):
        with self._connect() as conn:
            return conn.execute('DELETE FROM events WHERE event_id=?', (event_id,)).rowcount

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
                    event_id, encoded, attempts = row
                    try:
                        capability = await client.get(f'{self.backend_url}/api/events/capabilities')
                        capability.raise_for_status()
                        if capability.json().get('idempotency') != 'permanent-v1':
                            raise RuntimeError('后端未声明原子幂等能力，停止发送并重试检查')
                        response = await client.post(f'{self.backend_url}/api/events', json=json.loads(encoded))
                        response.raise_for_status()
                        ack = response.json()
                        if ack.get('event_id') != event_id or not isinstance(ack.get('applied'), bool):
                            raise RuntimeError('后端确认缺少有效 event_id/applied')
                        removed = await asyncio.to_thread(self._done, event_id)
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
