
# 获取排班信息

```bash
curl --request POST \
  --url 'https://api.flashcat.cloud/schedule/info?app_key=**' \
  --header 'Content-Type: application/json' \
  --data '
{
  "schedule_id": 5511541227763, # 一个固定的ID
  "start": 1778468400, # 开始时间为每天的 11:00 unix时间戳
  "end": 1778554800  # 结束时间为次日的11:00 unix时间戳
}
'
```
响应示例：

```json
{
    "request_id": "0a65ea0a6a01631331610001ecfe82b0",
    "data": {
        "id": 5511541227763,
        "name": "引擎组值班",
        "account_id": 4503237861895,
        "group_id": 4503245179895,
        "disabled": 0,
        "create_at": 1760679209,
        "create_by": 4503237861895,
        "update_at": 1776910839,
        "update_by": 4503237861895,
        "layers": [
            {
                "account_id": 4503237861895,
                "name": "规则1",
                "schedule_id": 5511541227763,
                "hidden": 0,
                "mode": 0,
                "weight": 0,
                "groups": [
                    {
                        "group_name": "A",
                        "name": "A",
                        "members": [
                            {
                                "role_id": 0,
                                "person_ids": [
                                    5415270052895,
                                    5419373642895,
                                    6331045902895
                                ]
                            }
                        ],
                        "start": 0,
                        "end": 0
                    },
                    {
                        "group_name": "B",
                        "name": "B",
                        "members": [
                            {
                                "role_id": 0,
                                "person_ids": [
                                    5413612482895,
                                    5419327032895,
                                    6331039322895
                                ]
                            }
                        ],
                        "start": 0,
                        "end": 0
                    },
                    {
                        "group_name": "C",
                        "name": "C",
                        "members": [
                            {
                                "role_id": 0,
                                "person_ids": [
                                    5419314842895,
                                    5715041952895,
                                    6331044692895
                                ]
                            }
                        ],
                        "start": 0,
                        "end": 0
                    },
                    {
                        "group_name": "D",
                        "name": "D",
                        "members": [
                            {
                                "role_id": 0,
                                "person_ids": [
                                    5415289432895,
                                    5993261322895
                                ]
                            }
                        ],
                        "start": 0,
                        "end": 0
                    },
                    {
                        "group_name": "E",
                        "name": "E",
                        "members": [
                            {
                                "role_id": 0,
                                "person_ids": [
                                    4503237861895,
                                    5803962592895,
                                    6330178222895
                                ]
                            }
                        ],
                        "start": 0,
                        "end": 0
                    }
                ],
                "rotation_duration": 86400,
                "handoff_time": 0,
                "enable_time": 1766372400,
                "expire_time": 0,
                "restrict_mode": 0,
                "restrict_start": 0,
                "restrict_end": 0,
                "restrict_periods": [

                ],
                "day_mask": {
                    "repeat": null
                },
                "create_at": 1776910839,
                "create_by": 4503237861895,
                "update_at": 1776910839,
                "update_by": 4503237861895,
                "layer_name": "规则1",
                "fair_rotation": false,
                "layer_start": 1766372400,
                "layer_end": null,
                "rotation_unit": "day",
                "rotation_value": 1,
                "mask_continuous_enabled": false
            }
        ],
        "schedule_layers": [
            {
                "layer_name": "规则1",
                "name": "规则1",
                "mode": 0,
                "schedules": [
                    {
                        "start": 1778468400,
                        "end": 1778554800,
                        "group": {
                            "group_name": "A",
                            "name": "A",
                            "members": [
                                {
                                    "role_id": 0,
                                    "person_ids": [
                                        5415270052895,
                                        5419373642895,
                                        6331045902895
                                    ]
                                }
                            ],
                            "start": 1778468400,
                            "end": 1778515200
                        },
                        "index": 0
                    }
                ]
            }
        ],
        "final_schedule": {
            "layer_name": "",
            "name": "",
            "mode": 0,
            "schedules": [
                {
                    "start": 1778468400,
                    "end": 1778554800,
                    "group": {
                        "group_name": "A",
                        "name": "A",
                        "members": [
                            {
                                "role_id": 0,
                                "person_ids": [
                                    5415270052895,
                                    5419373642895,
                                    6331045902895
                                ]
                            }
                        ],
                        "start": 1778468400,
                        "end": 1778515200
                    },
                    "index": 0
                }
            ]
        },
        "notify": {
            "fixed_time": null,
            "by": null,
            "webhooks": null
        },
        "schedule_id": 5511541227763,
        "schedule_name": "引擎组值班",
        "team_id": 4503245179895,
        "description": "",
        "layer_schedules": [
            {
                "layer_name": "规则1",
                "name": "规则1",
                "mode": 0,
                "schedules": [
                    {
                        "start": 1778468400,
                        "end": 1778554800,
                        "group": {
                            "group_name": "A",
                            "name": "A",
                            "members": [
                                {
                                    "role_id": 0,
                                    "person_ids": [
                                        5415270052895,
                                        5419373642895,
                                        6331045902895
                                    ]
                                }
                            ],
                            "start": 1778468400,
                            "end": 1778515200
                        },
                        "index": 0
                    }
                ]
            }
        ],
        "status": 0,
        "cur_oncall": {
            "start": 1778475780,
            "end": 1778554800,
            "group": {
                "group_name": "A",
                "name": "A",
                "members": [
                    {
                        "role_id": 0,
                        "person_ids": [
                            5415270052895,
                            5419373642895,
                            6331045902895
                        ]
                    }
                ],
                "start": 1778475780,
                "end": 1778515200
            },
            "update_at": 0,
            "weight": 0,
            "index": 0
        },
        "next_oncall": {
            "start": 1778554800,
            "end": 1778641200,
            "group": {
                "group_name": "B",
                "name": "B",
                "members": [
                    {
                        "role_id": 0,
                        "person_ids": [
                            5413612482895,
                            5419327032895,
                            6331039322895
                        ]
                    }
                ],
                "start": 1778554800,
                "end": 1778601600
            },
            "update_at": 0,
            "weight": 0,
            "index": 0
        }
    }
}
```
cur_oncall 中的 `group.members` 列表即为当前值班人员的 `person_id` 列表。下一轮值班人员信息在 `next_oncall.group.members` 中。


## 获取用户名

```bash
curl --request POST \
  --url 'https://api.flashcat.cloud/person/infos?app_key=**' \
  --header 'Content-Type: application/json' \
  --data '
{
  "person_ids": [
    5415270052895,5419373642895,6331045902895
  ]
}
'
```
响应示例：

```json
{
    "request_id": "0706d73ff54b2b3f798e90051daa677a",
    "data": {
        "items": [
            {
                "account_id": 4503237861895,
                "person_id": 5415270052895,
                "person_name": "张三",
                "locale": "zh-CN",
                "time_zone": "Asia/Shanghai",
                "phone_verified": false,
                "email_verified": false,
                "as": "member",
                "status": "enabled"
            },
            {
                "account_id": 4503237861895,
                "person_id": 5419373642895,
                "person_name": "李四",
                "locale": "zh-CN",
                "time_zone": "Asia/Shanghai",
                "email": "xbwang@magikcompute.ai",
                "phone_verified": false,
                "email_verified": true,
                "as": "member",
                "status": "enabled"
            },
            {
                "account_id": 4503237861895,
                "person_id": 6331045902895,
                "person_name": "王五",
                "locale": "zh-CN",
                "time_zone": "Asia/Shanghai",
                "phone_verified": false,
                "email_verified": false,
                "as": "member",
                "status": "enabled"
            }
        ]
    }
}
```
响应中的 `person_name` 字段即为对应 `person_id` 的用户名。