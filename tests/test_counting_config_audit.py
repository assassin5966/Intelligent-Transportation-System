"""只读配置扫描只访问明确指定的测试 Redis。"""
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import redis.asyncio as redis_async


@pytest.mark.asyncio
async def test_config_audit_reports_invalid_without_modifying_redis():
    url = os.environ.get('TEST_REDIS_URL')
    if not url:
        pytest.skip('requires isolated TEST_REDIS_URL')
    prefix = 'config_audit_' + uuid4().hex
    client = redis_async.from_url(url, decode_responses=True)
    try:
        await client.hset(f'{prefix}:device:bad', mapping={'id': 'bad', 'line_coords': 'bad', 'stream_url': 'secret-url'})
        await client.hset(f'{prefix}:device:empty', mapping={'id': 'empty', 'line_coords': ''})
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'audit_counting_configs.py'
        env = {**os.environ, 'REDIS_URL': url}
        process = await asyncio.create_subprocess_exec(sys.executable, str(script), '--redis-prefix', prefix,
                                                       env=env, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        assert process.returncode == 1, stderr.decode()
        result = json.loads(stdout)
        assert result['counts'] == {'invalid': 1, 'unconfigured': 1}
        assert 'secret-url' not in stdout.decode()
        assert (await client.hgetall(f'{prefix}:device:bad'))['line_coords'] == 'bad'
    finally:
        keys = [key async for key in client.scan_iter(f'{prefix}:*')]
        if keys:
            await client.delete(*keys)
        await client.aclose()
