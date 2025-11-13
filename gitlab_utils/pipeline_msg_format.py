import logging
import json

logger = logging.getLogger(__name__)

def json_processing(group_id, data, feishu_client):
    """
    Args:
        group_id: 群组ID
        data: 数据
        feishu_client: 飞书客户端实例
    
    Returns:
        tuple: (result, status_code)
    """
    try:
        if not data:
            return {"code": 400, "msg": "No data provided"}, 400
        webhook_type = data.get("object_kind")
        pipeline_status = data.get("object_attributes", {}).get("status")
        logger.info(f"Received GitLab webhook type: {webhook_type}")
        logger.debug(f"GitLab webhook data: {json.dumps(data, indent=2)}")
        
        if webhook_type == "pipeline" and (pipeline_status == "success" or pipeline_status == "failed"):
            # 初始化变量
            failed_jobs = []
            all_jobs = []
            
            pipeline_id = data.get("object_attributes", {}).get("id")
            pipeline_created_at = data.get("object_attributes", {}).get("created_at")
            pipeline_finished_at = data.get("object_attributes", {}).get("finished_at")
            pipeline_stages = data.get("object_attributes", {}).get("stages")
            commit_diff_url = data.get("commit", {}).get("url")
            commit_user_email = data.get("commit").get("author").get("email")
            commit_user_name = data.get("commit").get("author").get("name")
            commit_title = data.get("commit").get("title")
            commit_message = data.get("commit").get("message")
            project_url = data.get("project", {}).get("web_url")
            project_name = data.get("project", {}).get("name")
            project_id = data.get("project", {}).get("id")
            project_namespace = data.get("project", {}).get("namespace")
            project_path = data.get("project", {}).get("path")
            project_path_with_namespace = data.get("project", {}).get("path_with_namespace")
            project_default_branch = data.get("project", {}).get("default_branch")
            project_avatar_url = data.get("project", {}).get("avatar_url")
            project_web_url = data.get("project", {}).get("web_url")
            project_visibility_level = data.get("project", {}).get("visibility_level")
            
            # 收集所有 job 信息，特别是失败的 job
            if pipeline_stages and data.get("builds"):
                for build in data.get("builds"):
                    job_info = {
                        "stage": build.get("stage"),
                        "name": build.get("name"),
                        "status": build.get("status"),
                        "allow_failure": build.get("allow_failure", False)
                    }
                    all_jobs.append(job_info)
                    
                    # 收集失败的 job（排除允许失败的）
                    if job_info["status"] == "failed" and not job_info["allow_failure"]:
                        failed_jobs.append(job_info)
        
            # 构造成功消息卡片内容
            success_card_content = f"**项目名称:** {project_name}\n"
            success_card_content += f"**流水线ID:** {pipeline_id}\n"
            success_card_content += f"**提交信息:** {commit_message}\n"
            success_card_content += f"**提交人:** {commit_user_email}\n"
            success_card_content += f"**代码diff:** [查看]({commit_diff_url})\n"
            success_card_content += f"**完成时间:** {pipeline_finished_at}\n"
            
            # 显示所有 job 状态
            if all_jobs:
                success_card_content += f"\n**✅ 所有步骤 ({len(all_jobs)}):**\n"
                for job in all_jobs:
                    status_icon = "✅" if job["status"] == "success" else "⚠️"
                    success_card_content += f"{status_icon} **[{job['stage']}]** {job['name']}\n"

            # 构造失败消息卡片内容
            failed_card_content = f"**项目名称:** {project_name}\n"
            failed_card_content += f"**流水线ID:** {pipeline_id}\n"
            failed_card_content += f"**提交信息:** {commit_message}\n"
            failed_card_content += f"**提交人:** {commit_user_email}\n"
            failed_card_content += f"**代码diff:** [查看]({commit_diff_url})\n\n"
            
            # 添加失败的 job 详情
            if failed_jobs:
                failed_card_content += f"**❌ 失败的步骤 ({len(failed_jobs)}):**\n"
                for job in failed_jobs:
                    failed_card_content += f"- **[{job['stage']}]** {job['name']} (状态: {job['status']})\n"
            else:
                failed_card_content += "**状态:** Pipeline 失败，但未找到具体失败的 job\n"
            
            failed_card_content += f"\n**完成时间:** {pipeline_finished_at}\n"
            if pipeline_status == "success":
                # 构建消息卡片
                card_data = {
                    "config": {
                        "wide_screen_mode": True
                    },
                    "header": {
                        "title": {
                            "tag": "plain_text",
                            "content": "Gitlab Pipeline Success"
                        },
                        "template": "blue"
                    },
                    "elements": [
                        {
                            "tag": "div",
                            "text": {
                                "tag": "lark_md",
                                "content": success_card_content
                            }
                        }
                    ]
                }
                feishu_client.send("chat_id", group_id, "interactive", json.dumps(card_data))
                logger.info(f"Gitlab pipeline success: {pipeline_id}")
                return {"code": 0, "msg": "success"}, 200
            elif pipeline_status == "failed":
                # 构建消息卡片
                card_data = {
                    "config": {
                        "wide_screen_mode": True
                    },
                    "header": {
                        "title": {
                            "tag": "plain_text",
                            "content": "Gitlab Pipeline Failed"
                        },
                        "template": "red"
                    },
                    "elements": [
                        {
                            "tag": "div",
                            "text": {
                                "tag": "lark_md",
                                "content": failed_card_content
                            }
                        }
                    ]
                }
                feishu_client.send("chat_id", group_id, "interactive", json.dumps(card_data))
                logger.info(f"Gitlab pipeline failed: {pipeline_id}")
                return {"code": 0, "msg": "success"}, 200
            else:
                # 其他状态（running, pending, skipped等）暂不处理
                logger.info(f"Gitlab pipeline status '{pipeline_status}' received, ignored (pipeline_id: {pipeline_id})")
                return {"code": 0, "msg": f"Pipeline status '{pipeline_status}' ignored"}, 200
        
        elif webhook_type == "push":
            push_ref = data.get("ref")
            push_username = data.get("user_name")
            push_project_name = data.get("project").get("name")
            push_project_url = data.get("project").get("web_url")
            push_project_namespace = data.get("project").get("namespace")
            push_commit_id = data.get("commits")[0].get("id")
            push_commit_message = data.get("commits")[0].get("message")
            push_commit_diff_url = data.get("commits")[0].get("url")
            push_file_add = data.get("commits")[0].get("added")
            push_file_removed = data.get("commits")[0].get("removed")
            push_file_modified = data.get("commits")[0].get("modified")
            push_finished_at = data.get("commits")[0].get("timestamp")
            
            # 判断文件新增、删除、修改的列表是否为空，并格式化显示
            if not push_file_add:
                file_add_display = "无"
            else:
                file_add_display = ", ".join(push_file_add)
            
            if not push_file_removed:
                file_removed_display = "无"
            else:
                file_removed_display = ", ".join(push_file_removed)
            
            if not push_file_modified:
                file_modified_display = "无"
            else:
                file_modified_display = ", ".join(push_file_modified)
            
            # 处理提交信息：去除首尾空白，保留内部换行
            commit_message_display = push_commit_message.strip()

            push_card_content = f"**项目:** [{push_project_name}]({push_project_url})\n"
            push_card_content += f"**分支:** {push_ref.replace('refs/heads/', '')}\n"
            push_card_content += f"**提交人:** {push_username}\n"
            push_card_content += f"**时间:** {push_finished_at}\n\n"
            push_card_content += f"📝 **提交信息:**\n> {commit_message_display.replace(chr(10), chr(10) + '> ')}\n\n"
            push_card_content += "**变更文件:**\n"
            if file_add_display != "无":
                push_card_content += f"  🟢 新增: {file_add_display}\n"
            if file_modified_display != "无":
                push_card_content += f"  🔵 修改: {file_modified_display}\n"
            if file_removed_display != "无":
                push_card_content += f"  🔴 删除: {file_removed_display}\n"
            if file_add_display == "无" and file_modified_display == "无" and file_removed_display == "无":
                push_card_content += "  无文件变更\n"
            push_card_content += f"\n[查看详情]({push_commit_diff_url}) | Commit ID: `{push_commit_id[:8]}`"

            # 构建消息卡片
            card_data = {
                "config": {
                    "wide_screen_mode": True
                },
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": "Gitlab Push"
                    },
                    "template": "blue"
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": push_card_content
                        }
                    }
                ]
            }
            feishu_client.send("chat_id", group_id, "interactive", json.dumps(card_data))
            logger.info(f"Gitlab push: {push_commit_id}")
            return {"code": 0, "msg": "success"}, 200

        else:
            logger.info(f"Unsupported webhook type: {webhook_type}")
            return {"code": 200, "msg": f"Unsupported webhook type: {webhook_type}"}, 200



    except Exception as e:
        logger.error(f"Gitlab pipeline status failed: {e}", exc_info=True)
        return {"code": 500, "msg": str(e)}, 500


