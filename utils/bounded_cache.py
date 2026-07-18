#!/usr/bin/env python3
"""
带 TTL 与容量上限的线程安全缓存

替代项目中裸 dict + 手动过期清理的去重缓存实现，提供：
- TTL 过期自动清理
- 最大容量上限（LRU 淘汰），防止内存无限增长
- 原子的"检查并标记"操作（mark），用于去重场景，避免 check-then-set 竞态

仅依赖标准库，无需引入第三方包。
"""

import threading
import time
from collections import OrderedDict
from typing import Any, Optional


class BoundedTTLCache:
    """线程安全的 TTL + LRU 缓存。

    Args:
        maxsize: 最大条目数，超出后按 LRU 淘汰最久未访问的条目。
        ttl: 每个条目的存活时间（秒）。
    """

    __slots__ = ("_data", "_lock", "maxsize", "ttl")

    def __init__(self, maxsize: int = 10000, ttl: float = 300.0):
        self._data: "OrderedDict[Any, tuple[float, Any]]" = OrderedDict()
        self._lock = threading.Lock()
        self.maxsize = maxsize
        self.ttl = ttl

    # ── 内部维护 ──
    def _purge_expired(self, now: float) -> None:
        """清理已过期条目。

        条目按插入顺序存储，由于 ttl 恒定，过期时间随插入时间单调递增，
        因此从头部弹出即可，无需遍历全部。
        """
        while self._data:
            exp, _ = next(iter(self._data.values()))
            if exp <= now:
                self._data.popitem(last=False)
            else:
                break

    def _evict_lru(self) -> None:
        """超出容量时按 LRU 淘汰（弹出头部最久未访问条目）。"""
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)

    # ── 公共 API ──
    def __contains__(self, key: Any) -> bool:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return False
            exp, _ = entry
            if exp <= time.time():
                # 已过期，惰性删除
                self._data.pop(key, None)
                return False
            return True

    def get(self, key: Any, default: Any = None) -> Any:
        """获取未过期的值，过期则删除并返回 default。命中时提升为最近使用。"""
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return default
            exp, value = entry
            now = time.time()
            if exp <= now:
                self._data.pop(key, None)
                return default
            # LRU：提升到末尾
            self._data.move_to_end(key)
            return value

    def set(self, key: Any, value: Any = None) -> None:
        """写入条目，刷新 TTL 与 LRU 位置。"""
        with self._lock:
            now = time.time()
            self._purge_expired(now)
            self._data[key] = (now + self.ttl, value)
            self._data.move_to_end(key)
            self._evict_lru()

    __setitem__ = set

    def pop(self, key: Any, default: Any = None) -> Any:
        """删除并返回值，不存在返回 default。"""
        with self._lock:
            entry = self._data.pop(key, None)
            if entry is None:
                return default
            return entry[1]

    def mark(self, key: Any, value: Any = True) -> bool:
        """原子的去重检查并标记。

        - 若 key 已存在且未过期：返回 True（表示重复），不刷新 TTL。
        - 若 key 不存在或已过期：写入并返回 False（表示首次）。

        适用于告警/事件去重场景，替代裸 dict 的 check-then-set。
        """
        with self._lock:
            now = time.time()
            self._purge_expired(now)
            entry = self._data.get(key)
            if entry is not None and entry[0] > now:
                return True
            self._data[key] = (now + self.ttl, value)
            self._data.move_to_end(key)
            self._evict_lru()
            return False

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def purge(self) -> int:
        """主动清理过期条目，返回清理数量。"""
        with self._lock:
            before = len(self._data)
            self._purge_expired(time.time())
            return before - len(self._data)
