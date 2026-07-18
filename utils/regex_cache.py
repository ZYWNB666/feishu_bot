#!/usr/bin/env python3
"""
正则表达式缓存

集中缓存项目中反复编译的正则表达式（label_rules 路由匹配、邮箱校验等），
避免每次请求重新编译。使用 functools.lru_cache 实现自动上限与淘汰。
"""

import re
from functools import lru_cache
from typing import Optional


@lru_cache(maxsize=1024)
def compile_pattern(pattern: str, flags: int = 0) -> Optional[re.Pattern]:
    """编译并缓存正则表达式。

    编译失败时返回 None（调用方需处理降级到精确匹配）。

    Args:
        pattern: 正则表达式字符串
        flags: re 编译标志（如 re.IGNORECASE）
    Returns:
        编译后的 Pattern 对象，或 None（编译失败）
    """
    try:
        return re.compile(pattern, flags)
    except re.error:
        return None


def search(pattern: str, string: str, flags: int = 0) -> Optional[re.Match]:
    """缓存版的 re.search，编译失败返回 None。"""
    pat = compile_pattern(pattern, flags)
    if pat is None:
        return None
    return pat.search(string)


def match(pattern: str, string: str, flags: int = 0) -> Optional[re.Match]:
    """缓存版的 re.match，编译失败返回 None。"""
    pat = compile_pattern(pattern, flags)
    if pat is None:
        return None
    return pat.match(string)


def cache_info() -> dict:
    """返回缓存统计信息（用于调试/监控）。"""
    info = compile_pattern.cache_info()
    return {
        "hits": info.hits,
        "misses": info.misses,
        "maxsize": info.maxsize,
        "currsize": info.currsize,
    }
