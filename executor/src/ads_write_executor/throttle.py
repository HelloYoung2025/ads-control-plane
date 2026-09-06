"""桶感知节流器（评审 ENG-03）：置于单提交 CAS 之前的本地限流。

现实约束（2026-08-28 官方文档）：领星开放平台写接口令牌桶容量 1（按 appId+接口）。
执行器必须先消费本地令牌再进入 CAS，尽量不把请求撞到 Provider 限流上——
撞上限流虽有有界重投兜底，但每次都消耗重投预算。

MVP 为手动补充的计数桶（测试确定性）；生产实现按 Provider 文档速率自动补充。
"""

from __future__ import annotations

import threading
from collections import defaultdict


class LocalThrottle:
    def __init__(self, capacity_per_key: int = 1) -> None:
        self._lock = threading.Lock()
        self._capacity = capacity_per_key
        self._tokens: dict[str, int] = defaultdict(lambda: capacity_per_key)

    def try_acquire(self, key: str) -> bool:
        """非阻塞获取。失败时调用方保持 DISPATCHING 稍后重投——不消耗 Intent。"""
        with self._lock:
            if self._tokens[key] <= 0:
                return False
            self._tokens[key] -= 1
            return True

    def refill(self, key: str, tokens: int = 1) -> None:
        with self._lock:
            self._tokens[key] = min(self._capacity, self._tokens[key] + tokens)
