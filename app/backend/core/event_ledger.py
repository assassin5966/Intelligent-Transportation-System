"""可选的 MySQL 事件账本：持久接收与待物化任务保存在同一行。"""
import asyncio
import hashlib
import json
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import aiomysql
import pymysql

from ...common import mysql_client
from ...common.config import settings
from ...common.logger import logger
from . import realtime

_DDL = """CREATE TABLE IF NOT EXISTS traffic_event_ledger (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    event_id VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    device_id VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    occurred_at VARCHAR(64) NOT NULL,
    payload_hash CHAR(64) NOT NULL,
    evidence LONGTEXT NOT NULL,
    received_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    materialized_at DATETIME(6) NULL,
    attempts INT UNSIGNED NOT NULL DEFAULT 0,
    next_attempt DOUBLE NOT NULL DEFAULT 0,
    last_error VARCHAR(512) NULL,
    PRIMARY KEY (id), UNIQUE KEY event_id_unique (event_id),
    KEY pending_order (materialized_at, id), KEY device_order (device_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""


class LedgerConflict(ValueError):
    pass


def _canonical(event: dict, evidence: dict) -> tuple[str, str]:
    encoded_evidence = json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                                  separators=(',', ':'), allow_nan=False)
    body = json.dumps({'event': event, 'evidence': evidence}, ensure_ascii=False,
                      sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(body.encode()).hexdigest(), encoded_evidence


class EventLedger:
    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._ready = False
        self._ready_lock = asyncio.Lock()
        self._last_error: Optional[str] = None
        self._lock_held = False

    async def ensure_ready(self):
        if self._ready:
            return
        async with self._ready_lock:
            if self._ready:
                return
            pool = await mysql_client.get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(_DDL)
            self._ready = True

    async def persist(self, event: dict, evidence: dict) -> bool:
        """返回是否首次持久接收；同 ID 不同内容冲突，不写 Redis。"""
        await self.ensure_ready()
        digest, evidence_json = _canonical(event, evidence)
        pool = await mysql_client.get_pool()
        async with pool.acquire() as conn:
            lock_name = 'dt-evdev-' + hashlib.sha256(
                (settings.mysql_database + '\0' + event['device_id']).encode()).hexdigest()[:48]
            async with conn.cursor() as cur:
                await cur.execute('SELECT GET_LOCK(%s, 5)', (lock_name,))
                if (await cur.fetchone())[0] != 1:
                    raise RuntimeError('device event journal lock unavailable')
            try:
                await conn.begin()
                async with conn.cursor() as cur:
                    await cur.execute("""INSERT INTO traffic_event_ledger
                        (event_id,device_id,event_type,occurred_at,payload_hash,evidence)
                        VALUES (%s,%s,%s,%s,%s,%s)""",
                        (event['event_id'], event['device_id'], event['event_type'],
                         event['occurred_at'], digest, evidence_json))
                await conn.commit()
                return True
            except pymysql.IntegrityError as exc:
                await conn.rollback()
                if exc.args[0] != 1062:
                    raise
                async with conn.cursor() as cur:
                    await cur.execute('SELECT payload_hash FROM traffic_event_ledger WHERE event_id=%s',
                                      (event['event_id'],))
                    row = await cur.fetchone()
                if not row or row[0] != digest:
                    raise LedgerConflict('event_id already used for different content') from exc
                return False
            except BaseException:
                await conn.rollback()
                raise
            finally:
                try:
                    async with conn.cursor() as cur:
                        await cur.execute('SELECT RELEASE_LOCK(%s)', (lock_name,))
                except Exception:
                    conn.close()  # do not return a connection holding an unknown lock to the pool

    async def _next(self, conn):
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("""SELECT e.id,e.event_id,e.device_id,e.event_type,e.occurred_at,e.attempts
                FROM traffic_event_ledger e WHERE e.materialized_at IS NULL AND e.next_attempt<=%s
                AND NOT EXISTS (SELECT 1 FROM traffic_event_ledger p
                    WHERE p.device_id=e.device_id AND p.id<e.id AND p.materialized_at IS NULL)
                ORDER BY e.id LIMIT 1""", (time.time(),))
            return await cur.fetchone()

    async def _materialize_one(self, conn, row):
        try:
            event_time = datetime.fromisoformat(row['occurred_at'])
            if event_time.tzinfo is None or event_time.utcoffset() is None:
                raise ValueError('v2 event timestamp has no timezone offset')
            await realtime.apply_event(row['event_type'], row['device_id'],
                                       event_time.astimezone(ZoneInfo(settings.business_timezone)), row['event_id'])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._last_error = f'{type(exc).__name__}: {exc}'
            delay = min(60, 2 ** min(int(row['attempts']), 6))
            async with conn.cursor() as cur:
                await cur.execute('UPDATE traffic_event_ledger SET attempts=attempts+1,next_attempt=%s,last_error=%s WHERE id=%s',
                                  (time.time() + delay, self._last_error[:512], row['id']))
            return False
        async with conn.cursor() as cur:
            await cur.execute('UPDATE traffic_event_ledger SET materialized_at=NOW(6),last_error=NULL WHERE id=%s AND materialized_at IS NULL',
                              (row['id'],))
        self._last_error = None
        return True

    async def _run(self):
        # 持有 MySQL 会话级锁，避免多后端同时把同一设备事件乱序物化。
        lock_name = 'dt-event-' + hashlib.sha256(settings.mysql_database.encode()).hexdigest()[:24]
        while True:
            try:
                await self.ensure_ready()
                pool = await mysql_client.get_pool()
                async with pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute('SELECT GET_LOCK(%s, 0)', (lock_name,))
                        acquired = (await cur.fetchone())[0] == 1
                    if not acquired:
                        await asyncio.sleep(2)
                        continue
                    self._lock_held = True
                    try:
                        while True:
                            row = await self._next(conn)
                            if row is None:
                                await asyncio.sleep(0.5)
                            else:
                                await self._materialize_one(conn, row)
                    finally:
                        self._lock_held = False
                        try:
                            async with conn.cursor() as cur:
                                await cur.execute('SELECT RELEASE_LOCK(%s)', (lock_name,))
                        except Exception:
                            conn.close()  # never return a possibly lock-holding session to the pool
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._ready = False
                self._last_error = f'{type(exc).__name__}: {exc}'
                logger.error(f'事件账本物化失败，将重试: {self._last_error}')
                await asyncio.sleep(2)

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name='event-ledger-materializer')

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def snapshot(self):
        result = {'ready': self._ready, 'worker_running': self._task is not None and not self._task.done(),
                  'materializer_lock_held': self._lock_held, 'last_error': self._last_error,
                  'pending': None, 'oldest_pending_age_seconds': None}
        if not self._ready:
            return result
        try:
            pool = await mysql_client.get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute('SELECT COUNT(*),TIMESTAMPDIFF(SECOND,MIN(received_at),NOW()) FROM traffic_event_ledger WHERE materialized_at IS NULL')
                    pending, oldest = await cur.fetchone()
            result.update(pending=pending, oldest_pending_age_seconds=oldest)
        except Exception as exc:  # noqa: BLE001
            result['last_error'] = f'{type(exc).__name__}: {exc}'
        return result


event_ledger = EventLedger()
