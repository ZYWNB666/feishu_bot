# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Start the service
python main.py

# Test sending messages (edit CHAT_ID first)
python example_send.py

# Verify config
python config/config.py
```

No test runner, linter, or build tooling is configured.

## Architecture

**Flask monolith** — all HTTP routes live in `main.py`. Each route delegates to a handler module:

| Route | Handler module |
|---|---|
| `POST /webhook/event` | `feishu_utils/event_handler.py` → `feishu_event()` |
| `POST /api/card_callback` | `feishu_utils/callback_handler.py` → `process_card_callback()` |
| `POST /api/v1/alerts` | `feishu_utils/alert_handler.py` → `process_alert_request()` |
| `POST /api/gitlab-pipeline-status` | `gitlab_utils/pipeline_msg_format.py` → `json_processing()` |
| `POST /api/send_message`, `/api/send_text` | `main.py` directly (thin wrappers around `feishu_client.send()`) |
| Alert rules CRUD (`/api/alert_rules`) | `main.py` directly (raw SQL against `alert_config` table) |

**Two MySQL databases** (can be the same or separate instances):
- **Config DB** (`MYSQL_HOST`/`MYSQL_DATABASE`) — `alert_config` table (routing rules, `label_rules` for regex matching)
- **Alert DB** (same config keys, no separate env vars currently — `get_alert_db_config` is aliased to `get_config_db_config`)

## Key design patterns

**Async event processing**: The Feishu webhook callback (`/webhook/event`) immediately returns HTTP 200, then dispatches all event work to a daemon thread via `_process_event_async_wrapper()`. This prevents Feishu from timing out and retrying events. The same pattern is used for card callbacks — silence operations (`handle_silence_action`, `handle_cancel_silence_action`) run in background threads.

**In-memory deduplication**: Both `event_handler` and `callback_handler` maintain in-memory caches (`_event_cache` with 1-hour TTL, `_callback_cache` with 5-second TTL) to prevent processing duplicate events/callbacks.

**Alert routing priority**: When an alert arrives, `_find_alert_configs()` tries label-rule matching first (regex on `label_rules` JSON), then falls back to exact `alert_id` matching if no label-rule matches were found.

**Config loading**: `config/config.py` loads environment variables at module import time (class-level attributes). `load_dotenv(find_dotenv())` searches for a `.env` file. The `validate()` method is called at startup and will `sys.exit(1)` if required vars are missing.

**Feishu API client**: `FeishuApiClient` auto-fetches `tenant_access_token` before each `send()` or `reply_message()` call. Not cached across calls.

## Modules

- **`feishu_utils/`** — Core Feishu integration: API client, event dispatching, alert-to-card formatting, card callback handling (silence buttons), welcome message formatting
- **`alerts_format/`** — Alertmanager data extraction, label filtering (Kubernetes prefixes stripped), DB save/lookup for alert routing
- **`gitlab_utils/`** — GitLab webhook handler for Pipeline and Push events, formats them into Feishu card messages
- **`jira_utils/`** — Jira session-auth client that extracts CSRF tokens from page HTML to call invite-user endpoints
- **`config/`** — Single Config class reading all settings from env vars

## Database schema

Two tables (see `init.sql`):
- **`alert_config`** — Routing rules: maps alerts (by `alert_id` or `label_rules` regex) to `group_id` + `users` + `alertmanager_url`. `label_rules` is a JSON column where keys and values are regex patterns matched against alert labels.
- **`alert_data`** — Alert records: `id` (random 20-char alphanumeric, exposed as "MAID" in cards), `alertlabels` (JSON of matchers), `project`, `alerttime`, `silenceid` (JSON array of Alertmanager silence IDs)

## Card interactions

The silence feature uses Feishu interactive card buttons:
- Button `value` contains a JSON string with `action` ("silence" or "cancel_silence"), `maid`, and `duration` (in seconds)
- Card callback parsing handles double-JSON-encoding (Feishu may double-escape the value string)
- The `ma/ma.py` module calls Alertmanager's `/api/v2/silences` API directly
