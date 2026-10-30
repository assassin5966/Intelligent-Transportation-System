#!/usr/bin/env python3
"""只读对账本地 SQLite 与 MySQL v2 账本及 Redis 幂等记录。

数据库连接通过 MYSQL_HOST/MYSQL_PORT/MYSQL_USER/MYSQL_PASSWORD/MYSQL_DATABASE
和 REDIS_URL 环境变量提供；绝不修改账本或对差异自动补投。
"""
import argparse
import hashlib
import json
import os
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pymysql
import redis


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sqlite', required=True, help='AI 侧 event-outbox SQLite 路径')
    parser.add_argument('--device', help='仅对账指定设备')
    parser.add_argument('--since-received', help='配合 --device，从 MySQL 接收时间起检查后端独有事件，例如 2026-10-28 00:00:00')
    parser.add_argument('--redis-prefix', default=os.environ.get('REDIS_PREFIX', 'sc'))
    parser.add_argument('--sample-limit', type=int, default=20)
    args = parser.parse_args()
    if 'REDIS_URL' not in os.environ:
        parser.error('请通过 REDIS_URL 环境变量指定 Redis')
    if args.sample_limit < 0:
        parser.error('--sample-limit must be nonnegative')
    if args.since_received and not args.device:
        parser.error('--since-received requires --device')
    if args.since_received:
        try:
            datetime.fromisoformat(args.since_received)
        except ValueError:
            parser.error('--since-received must be an ISO datetime')

    db_uri = Path(args.sqlite).resolve().as_uri() + '?mode=ro'
    local = sqlite3.connect(db_uri, uri=True)
    mysql = pymysql.connect(host=os.environ.get('MYSQL_HOST', 'mysql'),
                            port=int(os.environ.get('MYSQL_PORT', '3306')),
                            user=os.environ.get('MYSQL_USER', 'root'),
                            password=os.environ.get('MYSQL_PASSWORD', ''),
                            database=os.environ.get('MYSQL_DATABASE', 'dt_stats'),
                            charset='utf8mb4', autocommit=False,
                            cursorclass=pymysql.cursors.DictCursor)
    cache = redis.Redis.from_url(os.environ['REDIS_URL'], decode_responses=True)
    counts, samples = Counter(), {}

    def add(name, event_id):
        counts[name] += 1
        if len(samples.setdefault(name, [])) < args.sample_limit:
            samples[name].append(event_id)

    try:
        local.execute('BEGIN')
        local_watermark = local.execute('SELECT COALESCE(MAX(rowid),0) FROM event_journal').fetchone()[0]
        with mysql.cursor() as cur:
            cur.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
            cur.execute('SELECT COALESCE(MAX(id),0) AS watermark FROM traffic_event_ledger')
            mysql_watermark = int(cur.fetchone()['watermark'])
            query = 'SELECT event_id,payload,evidence,delivered,backend_applied FROM event_journal WHERE rowid<=?'
            values = [local_watermark]
            if args.device:
                query += ' AND device_id=?'
                values.append(args.device)
            rows = local.execute(query + ' ORDER BY rowid', values)
            while batch := rows.fetchmany(500):
                ids = [row[0] for row in batch]
                marks = ','.join(['%s'] * len(ids))
                cur.execute(f'SELECT event_id,payload_hash,materialized_at FROM traffic_event_ledger WHERE id<=%s AND event_id IN ({marks})',
                            (mysql_watermark, *ids))
                remote = {row['event_id']: row for row in cur.fetchall()}
                redis_digests = cache.hmget(f'{args.redis_prefix}:event:dedup', ids)
                for (event_id, payload_json, evidence_json, delivered, applied), redis_digest in zip(batch, redis_digests):
                    payload, evidence = json.loads(payload_json), json.loads(evidence_json)
                    payload['occurred_at'] = datetime.fromisoformat(payload['occurred_at']).isoformat()
                    mysql_row = remote.get(event_id)
                    if mysql_row is None:
                        add('ledger_missing_pending' if delivered is None else
                            'ledger_missing_delivered_v2' if applied is None else 'legacy_delivered_without_ledger', event_id)
                    else:
                        expected = digest({'event': payload, 'evidence': evidence})
                        add('ledger_match' if expected == mysql_row['payload_hash'] else 'ledger_payload_conflict', event_id)
                        if delivered is None:
                            add('ledger_received_local_pending', event_id)
                    event_time = datetime.fromisoformat(payload['occurred_at'])
                    redis_time = (event_time.astimezone(ZoneInfo(os.environ.get('BUSINESS_TIMEZONE', 'Asia/Shanghai'))).isoformat()
                                  if mysql_row and event_time.tzinfo is not None else event_time.isoformat())
                    identity = json.dumps([payload['device_id'], payload['event_type'], redis_time],
                                          ensure_ascii=False, separators=(',', ':'))
                    expected_redis = hashlib.sha256(identity.encode()).hexdigest()
                    if redis_digest is not None and redis_digest != expected_redis:
                        add('redis_payload_conflict', event_id)
                    elif mysql_row and mysql_row['materialized_at'] is not None and redis_digest is None:
                        add('materialized_missing_redis', event_id)
                    elif mysql_row and mysql_row['materialized_at'] is None and redis_digest is not None:
                        add('redis_applied_ledger_pending', event_id)
                    elif redis_digest is not None:
                        add('redis_match', event_id)
            if args.device:
                query = 'SELECT event_id FROM traffic_event_ledger WHERE id<=%s AND device_id=%s'
                mysql_args = [mysql_watermark, args.device]
                if args.since_received:
                    query += ' AND received_at>=%s'
                    mysql_args.append(args.since_received)
                cur.execute(query + ' ORDER BY id', mysql_args)
                while batch := cur.fetchmany(500):
                    ids = [item['event_id'] for item in batch]
                    marks = ','.join(['?'] * len(ids))
                    matched = {row[0] for row in local.execute(
                        f'SELECT event_id FROM event_journal WHERE rowid<=? AND event_id IN ({marks})',
                        (local_watermark, *ids))}
                    for event_id in ids:
                        if event_id not in matched:
                            add('backend_only_candidate', event_id)
        result = {'sqlite_watermark': local_watermark, 'mysql_watermark': mysql_watermark,
                  'counts': dict(counts), 'sample_event_ids': samples,
                  'scope': 'both_directions_for_device' if args.device else 'local_to_backend_only',
                  'limits': ['水位不是同时刻快照；对差异复查', '历史 legacy 事件和后端独有事件不代表丢失',
                             '幂等记录存在不证明统计桶完整，需另行对账小时汇总和视频真值']}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        issues = ('ledger_missing_delivered_v2', 'ledger_payload_conflict', 'redis_payload_conflict', 'materialized_missing_redis')
        return 1 if any(counts[key] for key in issues) else 0
    finally:
        local.close()
        mysql.rollback()
        mysql.close()
        cache.close()


if __name__ == '__main__':
    raise SystemExit(main())
