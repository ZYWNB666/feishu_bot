#!/usr/bin/env python3
"""启动时将飞书通讯录的姓名/open_id 全量同步到本地数据库。"""

from dataclasses import asdict, dataclass
from typing import Dict, Iterable, List

from db.pool import db_cursor
from feishu_utils.feishu_api import FeishuApiClient

DEPARTMENT_CHILDREN_URI = "/open-apis/contact/v3/departments/0/children"
USERS_BY_DEPARTMENT_URI = "/open-apis/contact/v3/users/find_by_department"
PAGE_SIZE = 50


class FeishuUserSyncError(RuntimeError):
    """飞书用户数据不完整或无法安全写入时抛出。"""


@dataclass(frozen=True)
class UserSyncResult:
    fetched: int
    added: int
    updated: int
    deleted: int
    changed: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _get_all_pages(
    client: FeishuApiClient,
    uri: str,
    params: dict,
) -> List[dict]:
    """完整读取一个飞书分页接口；任意一页失败时由调用方中止同步。"""
    items: List[dict] = []
    page_token = ""

    while True:
        page_params = dict(params)
        if page_token:
            page_params["page_token"] = page_token

        response = client.get_json(uri, params=page_params)
        data = response.get("data")
        if not isinstance(data, dict):
            raise FeishuUserSyncError(f"飞书接口返回中缺少有效 data: {uri}")
        page_items = data.get("items", [])
        if not isinstance(page_items, list):
            raise FeishuUserSyncError(f"飞书接口返回了非法 items: {uri}")
        items.extend(page_items)

        if not data.get("has_more", False):
            return items

        next_page_token = data.get("page_token") or ""
        if not next_page_token or next_page_token == page_token:
            raise FeishuUserSyncError(f"飞书接口分页缺少有效 page_token: {uri}")
        page_token = next_page_token


def fetch_all_feishu_users(client: FeishuApiClient) -> List[Dict[str, str]]:
    """从根部门和全部子部门读取用户，并按 open_id 去重。"""
    departments = _get_all_pages(
        client,
        DEPARTMENT_CHILDREN_URI,
        {"fetch_child": "true", "page_size": PAGE_SIZE},
    )

    # 子部门接口不包含根部门本身；加入 0，避免遗漏直属根部门的用户。
    department_ids = ["0"]
    seen_department_ids = {"0"}
    for department in departments:
        department_id = (department.get("open_department_id") or "").strip()
        if department_id and department_id not in seen_department_ids:
            seen_department_ids.add(department_id)
            department_ids.append(department_id)

    users_by_open_id: Dict[str, Dict[str, str]] = {}
    for department_id in department_ids:
        users = _get_all_pages(
            client,
            USERS_BY_DEPARTMENT_URI,
            {
                "department_id": department_id,
                "department_id_type": "open_department_id",
                "user_id_type": "open_id",
                "page_size": PAGE_SIZE,
            },
        )
        for user in users:
            open_id = (user.get("open_id") or "").strip()
            name = (user.get("name") or "").strip()
            if not open_id or not name:
                raise FeishuUserSyncError(f"飞书用户缺少姓名或 open_id: {user}")
            users_by_open_id[open_id] = {"name": name, "open_id": open_id}

    result = sorted(users_by_open_id.values(), key=lambda user: (user["name"], user["open_id"]))
    if not result:
        raise FeishuUserSyncError("飞书通讯录未返回任何用户，拒绝清空 feishu_users 表")
    _validate_users(result)
    return result


def _validate_users(users: Iterable[Dict[str, str]]) -> None:
    """确保远端结果符合 feishu_users 表的唯一键和字段长度约束。"""
    names: Dict[str, str] = {}
    open_ids = set()
    for user in users:
        name = user["name"]
        open_id = user["open_id"]
        if len(name) > 64 or len(open_id) > 64:
            raise FeishuUserSyncError(f"飞书用户字段超过数据库长度限制: name={name!r}")
        previous_open_id = names.get(name)
        if previous_open_id and previous_open_id != open_id:
            raise FeishuUserSyncError(
                f"存在同名飞书用户，无法写入姓名唯一的 feishu_users 表: {name!r}"
            )
        if open_id in open_ids:
            raise FeishuUserSyncError(f"存在重复 open_id: {open_id}")
        names[name] = open_id
        open_ids.add(open_id)


def sync_feishu_users(client: FeishuApiClient) -> UserSyncResult:
    """比较并以单事务全量覆盖 feishu_users 表。"""
    remote_users = fetch_all_feishu_users(client)
    remote_mapping = {user["name"]: user["open_id"] for user in remote_users}

    with db_cursor(dictionary=True) as (conn, cursor):
        cursor.execute("SELECT name, open_id FROM feishu_users")
        existing_rows = cursor.fetchall()
        existing_mapping = {row["name"]: row["open_id"] for row in existing_rows}

        added = len(remote_mapping.keys() - existing_mapping.keys())
        deleted = len(existing_mapping.keys() - remote_mapping.keys())
        updated = sum(
            existing_mapping[name] != remote_mapping[name]
            for name in remote_mapping.keys() & existing_mapping.keys()
        )
        changed = existing_mapping != remote_mapping

        if changed:
            # 不使用 TRUNCATE，确保后续 INSERT 失败时 DELETE 可以被事务回滚。
            cursor.execute("DELETE FROM feishu_users")
            cursor.executemany(
                "INSERT INTO feishu_users (name, open_id) VALUES (%s, %s)",
                [(user["name"], user["open_id"]) for user in remote_users],
            )
            conn.commit()

    return UserSyncResult(
        fetched=len(remote_users),
        added=added,
        updated=updated,
        deleted=deleted,
        changed=changed,
    )
