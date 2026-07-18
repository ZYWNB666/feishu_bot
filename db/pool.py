#!/usr/bin/env python3
"""
MySQL 连接池模块

替代项目中"每次请求新建/关闭连接"的模式，使用 mysql.connector 的连接池
复用连接，降低高频告警与 CRUD 接口下的连接建立开销。

提供：
- get_connection(): 从池中获取连接（用完调用 .close() 归还池）
- db_cursor(): 上下文管理器，自动获取连接/游标并在退出时归还
- with_db(): 装饰器，向视图函数注入 conn 与 cursor，消除样板代码
"""

import functools
import logging
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Tuple

from mysql.connector import pooling
from mysql.connector.connection import MySQLConnection
from mysql.connector.errors import PoolError

from config import config
from config.constants import MYSQL_POOL_SIZE, MYSQL_POOL_ACQUIRE_TIMEOUT

logger = logging.getLogger(__name__)

_pool: "pooling.MySQLConnectionPool | None" = None
_pool_lock = threading.Lock()


def _build_pool() -> "pooling.MySQLConnectionPool":
    """构建连接池（懒加载，线程安全）。"""
    db_config = config.get_config_db_config()
    # pooling 不支持 pool_size=0
    pool_size = max(1, MYSQL_POOL_SIZE)
    try:
        # 注：mysql-connector-python 8.0.16+ 默认在归还连接时重置会话，
        # 无需显式传 pool_reset_connection（部分版本不支持该参数）。
        return pooling.MySQLConnectionPool(
            pool_name="feishu_bot_pool",
            pool_size=pool_size,
            **db_config,
        )
    except Exception as e:
        logger.error("创建 MySQL 连接池失败: %s", e)
        raise


def get_pool() -> "pooling.MySQLConnectionPool":
    """获取连接池单例（懒加载）。"""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = _build_pool()
                logger.info("✅ MySQL 连接池已创建 (pool_size=%d)", MYSQL_POOL_SIZE)
    return _pool


def get_connection() -> MySQLConnection:
    """从连接池获取一个连接。

    mysql-connector 的连接池在耗尽时默认立即抛出 PoolError。这里按配置等待，
    避免短时并发峰值被误判为数据库故障。

    使用完毕后必须调用 conn.close() 归还连接池（pooled connection 的
    close() 实际是将连接归还池而非真正关闭）。
    """
    pool = get_pool()
    timeout = max(0.0, float(MYSQL_POOL_ACQUIRE_TIMEOUT))
    deadline = time.monotonic() + timeout

    while True:
        try:
            return pool.get_connection()
        except PoolError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.error("MySQL 连接池获取超时 (timeout=%.1fs)", timeout)
                raise PoolError(f"MySQL 连接池获取超时（{timeout:.1f}秒）") from exc
            time.sleep(min(0.05, remaining))


@contextmanager
def db_cursor(dictionary: bool = False) -> Iterator[Tuple[MySQLConnection, object]]:
    """上下文管理器：自动获取连接与游标，退出时归还连接。

    用法:
        with db_cursor(dictionary=True) as (conn, cur):
            cur.execute("SELECT ...")
            rows = cur.fetchall()
            conn.commit()

    Args:
        dictionary: True 时游标返回 dict 行
    Yields:
        (conn, cursor)
    """
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor(dictionary=dictionary)
        yield conn, cursor
    except Exception:
        try:
            conn.rollback()
        except Exception as rollback_error:
            logger.warning("MySQL 事务回滚失败: %s", rollback_error)
        raise
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        try:
            conn.close()  # 归还连接池
        except Exception:
            pass


def with_db(dictionary: bool = False):
    """装饰器：为视图函数注入 conn 与 cursor，并自动归还连接。

    被装饰函数需声明 conn 与 cursor 参数。异常时自动关闭游标与归还连接，
    不会自动 commit/rollback（由调用方在函数体内控制事务）。

    用法:
        @app.route("/api/foo")
        @with_db(dictionary=True)
        def foo(conn, cursor):
            cursor.execute("SELECT 1")
            return jsonify(cursor.fetchone())
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with db_cursor(dictionary=dictionary) as (conn, cursor):
                kwargs["conn"] = conn
                kwargs["cursor"] = cursor
                return fn(*args, **kwargs)
        return wrapper
    return decorator


def close_pool() -> None:
    """显式关闭连接池（通常仅在测试或优雅停机时调用）。"""
    global _pool
    with _pool_lock:
        _pool = None
    logger.info("MySQL 连接池已关闭")
