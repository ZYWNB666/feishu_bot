import os
import json
import requests
import sys
from typing import Dict, Any, Tuple, Set

# === input params start
app_id = os.getenv("APP_ID")        # app_id, required, 应用 ID
# 应用唯一标识，创建应用后获得。有关app_id 的详细介绍。请参考通用参数https://open.feishu.cn/document/ukTMukTMukTM/uYTM5UjL2ETO14iNxkTN/terminology。
app_secret = os.getenv("APP_SECRET")  # app_secret, required, 应用密钥
# 应用秘钥，创建应用后获得。有关 app_secret 的详细介绍，请参考https://open.feishu.cn/document/ukTMukTMukTM/uYTM5UjL2ETO14iNxkTN/terminology。
# === input params end

def get_tenant_access_token(app_id: str, app_secret: str) -> Tuple[str, Exception]:
    """获取 tenant_access_token

    Args:
        app_id: 应用ID
        app_secret: 应用密钥

    Returns:
        Tuple[str, Exception]: (access_token, error)
    """
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    payload = {
        "app_id": app_id,
        "app_secret": app_secret
    }
    headers = {
        "Content-Type": "application/json; charset=utf-8"
    }
    try:
        print(f"POST: {url}")
        print(f"Request body: {json.dumps(payload)}")
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()

        result = response.json()
        print(f"Response: {json.dumps(result)}")

        if result.get("code", 0) != 0:
            print(f"ERROR: failed to get tenant_access_token: {result.get('msg', 'unknown error')}", file=sys.stderr)
            return "", Exception(f"failed to get tenant_access_token: {response.text}")

        return result["tenant_access_token"], None

    except Exception as e:
        print(f"ERROR: getting tenant_access_token: {e}", file=sys.stderr)
        if hasattr(e, 'response') and e.response is not None:
            print(f"ERROR: Response body: {e.response.text}", file=sys.stderr)
        return "", e

def get_all_department_ids(tenant_access_token: str) -> Tuple[Set[str], Exception]:
    """获取所有部门的 open_department_id

    Args:
        tenant_access_token: 租户访问令牌

    Returns:
        Tuple[Set[str], Exception]: (部门ID集合, 错误)
    """
    department_ids = set()
    url = "https://open.feishu.cn/open-apis/contact/v3/departments/0/children"
    headers = {
        "Authorization": f"Bearer {tenant_access_token}",
        "Content-Type": "application/json; charset=utf-8"
    }
    
    params = {
        "department_id_type": "open_department_id",
        "user_id_type": "open_id",
        "fetch_child": True,
        "page_size": 50
    }
    
    page_token = ""
    try:
        while True:
            if page_token:
                params["page_token"] = page_token
            
            print(f"GET: {url}")
            print(f"Params: {json.dumps(params)}")
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            
            result = response.json()
            print(f"Response: {json.dumps(result)}")
            
            if result.get("code", 0) != 0:
                return department_ids, Exception(f"failed to get departments: {result.get('msg', 'unknown error')}")
            
            data = result.get("data", {})
            items = data.get("items", [])
            
            for item in items:
                open_department_id = item.get("open_department_id")
                if open_department_id:
                    department_ids.add(open_department_id)
                    print(f"Found department: {open_department_id}")
            
            if not data.get("has_more", False):
                break
            page_token = data.get("page_token", "")
            
        return department_ids, None
        
    except Exception as e:
        print(f"ERROR: getting department IDs: {e}", file=sys.stderr)
        return department_ids, e

def get_users_by_department(tenant_access_token: str, department_id: str) -> Tuple[list, Exception]:
    """获取指定部门的所有用户信息

    Args:
        tenant_access_token: 租户访问令牌
        department_id: 部门ID

    Returns:
        Tuple[list, Exception]: (用户列表, 错误)
    """
    users = []
    url = "https://open.feishu.cn/open-apis/contact/v3/users/find_by_department"
    headers = {
        "Authorization": f"Bearer {tenant_access_token}",
        "Content-Type": "application/json; charset=utf-8"
    }
    
    params = {
        "department_id": department_id,
        "department_id_type": "open_department_id",
        "user_id_type": "open_id",
        "page_size": 50
    }
    
    page_token = ""
    try:
        while True:
            if page_token:
                params["page_token"] = page_token
            
            print(f"GET: {url}")
            print(f"Params: {json.dumps(params)}")
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            
            result = response.json()
            print(f"Response: {json.dumps(result)}")
            
            if result.get("code", 0) != 0:
                return users, Exception(f"failed to get users for department {department_id}: {result.get('msg', 'unknown error')}")
            
            data = result.get("data", {})
            items = data.get("items", [])
            
            for item in items:
                # 过滤已离职用户
                status = item.get("status", {})
                if status.get("is_resigned", False):
                    continue
                
                open_id = item.get("open_id")
                name = item.get("name")
                if open_id and name:
                    users.append({
                        "open_id": open_id,
                        "name": name
                    })
                    print(f"Found user: open_id={open_id}, name={name}")
            
            if not data.get("has_more", False):
                break
            page_token = data.get("page_token", "")
            
        return users, None
        
    except Exception as e:
        print(f"ERROR: getting users for department {department_id}: {e}", file=sys.stderr)
        return users, e

if __name__ == "__main__":
    # 获取 tenant_access_token
    tenant_access_token, err = get_tenant_access_token(app_id, app_secret)
    if err:
        print(f"ERROR: getting tenant_access_token: {err}", file=sys.stderr)
        exit(1)
    
    # 获取所有部门ID
    department_ids, err = get_all_department_ids(tenant_access_token)
    if err:
        print(f"ERROR: getting department IDs: {err}", file=sys.stderr)
        exit(1)
    
    # 添加根部门（ID为0）
    department_ids.add("0")
    print(f"Total departments to process: {len(department_ids)}")
    
    # 存储所有用户，使用字典去重（以open_id为键）
    all_users = {}
    
    # 遍历所有部门获取用户
    for dept_id in department_ids:
        print(f"Processing department: {dept_id}")
        users, err = get_users_by_department(tenant_access_token, dept_id)
        if err:
            print(f"WARNING: Error getting users for department {dept_id}: {err}", file=sys.stderr)
            continue
        
        # 去重处理：如果用户已在字典中，则跳过；否则添加
        for user in users:
            open_id = user["open_id"]
            if open_id not in all_users:
                all_users[open_id] = user
    
    # 按指定格式输出结果
    print("\n=== Final Results ===")
    for user in all_users.values():
        print(f"open_id: {user['open_id']}, name: {user['name']}")
    
    print(f"\nTotal unique users: {len(all_users)}")