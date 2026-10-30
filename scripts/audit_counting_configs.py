#!/usr/bin/env python3
"""只读扫描 Redis 设备计数配置，输出未配置、无效及多段 ROI 清单。"""
import argparse
import json
import os
from collections import Counter

import redis

from app.common.counting_config import effective_line_intervals, parse_counting_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--redis-prefix', default=os.environ.get('REDIS_PREFIX', 'sc'))
    args = parser.parse_args()
    if 'REDIS_URL' not in os.environ:
        parser.error('请通过 REDIS_URL 环境变量指定 Redis')
    client = redis.Redis.from_url(os.environ['REDIS_URL'], decode_responses=True)
    rows = []
    try:
        for key in client.scan_iter(f'{args.redis_prefix}:device:*'):
            if client.type(key) != 'hash':
                continue
            data = client.hgetall(key)
            if not data.get('id'):
                continue
            result = {'device_id': data['id'], 'name': data.get('name', ''),
                      'gb_device_id': data.get('gb_device_id', ''),
                      'gb_channel_id': data.get('gb_channel_id', ''),
                      'status': data.get('status', '')}
            configured = bool((data.get('line_coords') or '').strip())
            try:
                line, _anchor, roi = parse_counting_config(
                    data.get('line_coords'), data.get('anchor_coords'), data.get('roi_coords'),
                    require_line=configured)
                result['config_state'] = 'valid' if configured else 'unconfigured'
                if configured and roi is not None:
                    result['effective_line_segments'] = len(effective_line_intervals(line, roi))
            except ValueError as exc:
                result.update(config_state='invalid', reason=str(exc))
            rows.append(result)
    finally:
        client.close()
    rows.sort(key=lambda item: item['device_id'])
    counts = Counter(row['config_state'] for row in rows)
    print(json.dumps({'counts': dict(counts), 'devices': rows}, ensure_ascii=False, indent=2))
    return 1 if counts['invalid'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
