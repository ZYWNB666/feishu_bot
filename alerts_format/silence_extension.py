"""Helpers for extending existing Alertmanager-compatible silences."""

import logging
from datetime import datetime, timedelta, timezone

import requests

from config.constants import SILENCE_API_TIMEOUT

logger = logging.getLogger(__name__)


def _parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone()
    except (TypeError, ValueError):
        return None


def extend_existing_silences(
    silence_base_url: str,
    silence_ids: list,
    duration_hours: int,
    headers: dict | None = None,
    backend: str = "Alertmanager",
) -> dict | None:
    """Extend existing silences from their current end time.

    Returns None when there are no IDs. A 404 marks stale IDs so callers can
    create a fresh silence; other failures are returned as errors.
    """
    if not silence_ids:
        return None

    headers = dict(headers or {})
    headers.setdefault("Content-Type", "application/json")
    now = datetime.now().astimezone()
    extension = timedelta(hours=int(duration_hours))
    target_ends = []
    updated_silence_ids = []
    silence_item_base_url = silence_base_url.rstrip("/")
    silence_collection_url = f"{silence_item_base_url}s"

    for silence_id in silence_ids:
        url = f"{silence_item_base_url}/{silence_id}"
        try:
            response = requests.get(url, headers=headers, timeout=SILENCE_API_TIMEOUT)
            if response.status_code == 404:
                return {
                    "success": False,
                    "not_found": True,
                    "message": f"Existing {backend} silence was not found",
                }
            response.raise_for_status()
            silence = response.json()
            if not isinstance(silence, dict):
                return {"success": False, "message": "Invalid silence response"}

            current_end = _parse_datetime(silence.get("endsAt"))
            target_end = max(current_end or now, now) + extension
            payload = {
                key: silence[key]
                for key in (
                    "matchers",
                    "startsAt",
                    "createdBy",
                    "comment",
                    "annotations",
                )
                if key in silence
            }
            # Alertmanager API v2 updates an existing silence by POSTing to
            # /silences with the existing ID in the request body. The
            # /silence/{id} resource only supports GET and DELETE.
            payload["id"] = silence_id
            payload.setdefault("startsAt", now.isoformat())
            payload["endsAt"] = target_end.isoformat()

            update_response = requests.post(
                silence_collection_url,
                headers=headers,
                json=payload,
                timeout=SILENCE_API_TIMEOUT,
            )
            if update_response.status_code not in (200, 201, 202):
                logger.error(
                    "%s silence extension failed: silence_id=%s status=%s body=%s",
                    backend, silence_id, update_response.status_code,
                    update_response.text[:500],
                )
                return {
                    "success": False,
                    "not_found": update_response.status_code == 404,
                    "message": f"Failed to extend {backend} silence",
                }

            result = update_response.json()
            returned_silence_id = (
                result.get("silenceID") or result.get("id")
                if isinstance(result, dict)
                else None
            )
            if returned_silence_id and str(returned_silence_id) != str(silence_id):
                logger.error(
                    "%s silence update returned a different ID: expected=%s actual=%s",
                    backend, silence_id, returned_silence_id,
                )
                return {
                    "success": False,
                    "message": f"{backend} silence update returned a different ID",
                }

            target_ends.append(target_end)
            updated_silence_ids.append(str(silence_id))
            logger.info(
                "%s silence extended: silence_id=%s ends_at=%s",
                backend, silence_id, target_end.isoformat(),
            )
        except Exception as exc:
            logger.error(
                "%s silence extension error: silence_id=%s error=%s",
                backend, silence_id, exc,
            )
            return {
                "success": False,
                "message": f"Failed to extend {backend} silence: {exc}",
            }

    remaining_seconds = max(
        0, int((max(target_ends) - now).total_seconds())
    )
    return {
        "success": True,
        "silence_ids": updated_silence_ids,
        "duration_seconds": remaining_seconds,
        "message": f"Extended {len(silence_ids)} {backend} silence rules",
    }
