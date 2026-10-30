#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实时位置数据接入 — Python 示例 (Java 接入示例的等价转换).

对应规范 : 《实时位置数据接入规范 V1.0》(石家庄轻简科技有限公司)
对应 Java : gps-pulsar-demo (Spring Boot + spring-boot-starter-pulsar)

Java 关键片段 (GpsPulsarListener.java)::

    @PulsarListener(
        topics = "persistent://dtzhjg/device/location_realtime_v1",
        subscriptionName = "client_sub_001",
        subscriptionType = SubscriptionType.Shared,
        schemaType = SchemaType.BYTES)
    public void onGps(byte[] payload) {
        String json = new String(payload, StandardCharsets.UTF_8);
        System.out.println("收到 GPS 数据: " + json);
    }

本示例用 Apache Pulsar 官方 Python 客户端 (pulsar-client) 实现等价逻辑：
    - 连接 pulsar://23.45.1.99:6650
    - AuthenticationToken 鉴权 (对应 Java 的 AuthenticationToken + param.token)
    - 订阅同一 Topic, Shared 订阅类型, 以 BYTES 方式接收(即原始 JSON 字节)
    - UTF-8 解码后打印 (生产环境可替换为入库/转发)

运行::

    pip install pulsar-client
    python examples/gps_pulsar_consumer.py
    # 自定义参数:
    python examples/gps_pulsar_consumer.py \
        --service-url pulsar://23.45.1.99:6650 \
        --token <接入Token> \
        --topic persistent://dtzhjg/device/location_realtime_v1 \
        --subscription client_sub_001
"""
from __future__ import annotations

import os

import argparse
import json
import signal
import sys
import time

import pulsar


# ===== 默认值 (来自规范附件 config.txt) =====
DEFAULT_SERVICE_URL = "pulsar://23.45.1.99:6650"
DEFAULT_TOKEN = os.environ.get("PULSAR_TOKEN", "")
DEFAULT_TOPIC = "persistent://dtzhjg/device/location_realtime_v1"
DEFAULT_SUBSCRIPTION = "client_sub_001"

# 规范 1. 数据结构描述 (JSON 字段)
LOCATION_FIELDS = (
    "id, deviceId, deviceType, longitude, latitude, "
    "speed, heading, altitude, positioningTime, extension"
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="实时位置数据 Pulsar 消费示例 (Python)")
    p.add_argument("--service-url", default=DEFAULT_SERVICE_URL, help="Pulsar 服务地址")
    p.add_argument("--token", default=DEFAULT_TOKEN, help="接入 Token (鉴权)")
    p.add_argument("--topic", default=DEFAULT_TOPIC, help="订阅 Topic")
    p.add_argument("--subscription", default=DEFAULT_SUBSCRIPTION, help="订阅名称(接入方标识)")
    p.add_argument("--count", type=int, default=0, help="接收多少条后退出 (0=持续运行)")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # Java: authentication.plugin-class-name = AuthenticationToken + param.token
    # Python 等价: pulsar.AuthenticationToken(token)
    auth = pulsar.AuthenticationToken(args.token) if args.token else None
    client = pulsar.Client(args.service_url, authentication=auth)

    # subscription_type=Shared 对应 Java SubscriptionType.Shared
    # schema 默认 BYTES (对应 Java SchemaType.BYTES) -> receive() 拿到原始字节
    consumer = client.subscribe(
        args.topic,
        args.subscription,
        consumer_type=pulsar.ConsumerType.Shared,
    )

    print(f"[*] 已订阅 topic={args.topic} 订阅={args.subscription} ({args.service_url})")
    print(f"[*] 字段: {LOCATION_FIELDS}")
    print("[*] 等待位置数据... (Ctrl+C 退出)")

    received = 0
    _running = True

    def _stop(signum, _frame):
        nonlocal _running
        _running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        while _running:
            try:
                # 1000ms 超时便于响应 Ctrl+C
                msg = consumer.receive(timeout_millis=1000)
            except pulsar.Timeout:
                continue
            except Exception as e:  # noqa: BLE001
                print(f"[!] 接收异常: {e}", file=sys.stderr)
                time.sleep(1)
                continue

            # 等价 Java: new String(payload, UTF_8)
            payload = msg.data().decode("utf-8")

            # 此处仅打印 (等价 Java onGps 的 System.out.println)。
            # 生产可在此解析 JSON 并入库/转发, 例如:
            #     data = json.loads(payload)
            #     print(data["deviceId"], data["longitude"], data["latitude"])
            print("收到 GPS 数据:", payload)

            consumer.acknowledge(msg)
            received += 1
            if args.count and received >= args.count:
                break
    finally:
        consumer.close()
        client.close()
        print(f"[*] 已退出, 共接收 {received} 条。")


if __name__ == "__main__":
    main()
