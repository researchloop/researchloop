"""Artifact upload and orchestrator webhooks.

Uses ``httpx`` for HTTP calls since this runs on HPC nodes where
``aiohttp`` may not be available.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Files we always look for in the sprint directory.
_KEY_FILENAMES = {"report.md", "summary.txt"}

# Extensions we scan for as additional artifacts.
_ARTIFACT_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}

# Generous timeout for large uploads from HPC → orchestrator.
_UPLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=120.0, pool=10.0)
_WEBHOOK_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)


def _auth_headers(shared_secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {shared_secret}"}


async def upload_artifacts(
    sprint_dir: str,
    orchestrator_url: str,
    shared_secret: str,
    sprint_id: str,
) -> list[str]:
    """Scan *sprint_dir* for key artifacts and upload them to the orchestrator.

    Returns a list of successfully uploaded filenames.
    """
    sprint_path = Path(sprint_dir)
    files_to_upload: list[Path] = []

    # Collect known filenames.
    for name in _KEY_FILENAMES:
        candidate = sprint_path / name
        if candidate.is_file():
            files_to_upload.append(candidate)

    # Scan for image / PDF artifacts anywhere in the sprint directory (1 level deep).
    for child in sprint_path.iterdir():
        if child.is_file() and child.suffix.lower() in _ARTIFACT_EXTENSIONS:
            if child not in files_to_upload:
                files_to_upload.append(child)

    # Also check results/ subdirectory.
    results_dir = sprint_path / "results"
    if results_dir.is_dir():
        for child in results_dir.iterdir():
            if child.is_file() and child.suffix.lower() in _ARTIFACT_EXTENSIONS:
                files_to_upload.append(child)

    if not files_to_upload:
        logger.info("No artifacts found to upload in %s", sprint_dir)
        return []

    url = f"{orchestrator_url.rstrip('/')}/api/artifacts/{sprint_id}"
    uploaded: list[str] = []

    async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT) as client:
        for file_path in files_to_upload:
            mime_type = (
                mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
            )
            try:
                with open(file_path, "rb") as f:
                    resp = await client.post(
                        url,
                        headers=_auth_headers(shared_secret),
                        files={"file": (file_path.name, f, mime_type)},
                    )
                resp.raise_for_status()
                uploaded.append(file_path.name)
                logger.info("Uploaded artifact: %s", file_path.name)
            except httpx.HTTPError:
                logger.exception("Failed to upload artifact: %s", file_path.name)

    return uploaded


async def send_webhook(
    orchestrator_url: str,
    shared_secret: str,
    sprint_id: str,
    status: str,
    summary: str | None = None,
    error: str | None = None,
) -> None:
    """Notify the orchestrator that a sprint has completed (or failed).

    POSTs to ``/api/webhook/sprint-complete``.
    """
    url = f"{orchestrator_url.rstrip('/')}/api/webhook/sprint-complete"
    payload = {
        "sprint_id": sprint_id,
        "status": status,
        "summary": summary,
        "error": error,
    }

    async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT) as client:
        resp = await client.post(
            url,
            headers={
                **_auth_headers(shared_secret),
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()

    logger.info("Sent sprint-complete webhook: sprint=%s status=%s", sprint_id, status)


async def send_heartbeat(
    orchestrator_url: str,
    shared_secret: str,
    sprint_id: str,
    status: str,
    step: int,
) -> None:
    """Send a heartbeat ping to the orchestrator.

    POSTs to ``/api/webhook/heartbeat``.
    """
    url = f"{orchestrator_url.rstrip('/')}/api/webhook/heartbeat"
    payload = {
        "sprint_id": sprint_id,
        "status": status,
        "step": step,
    }

    async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT) as client:
        resp = await client.post(
            url,
            headers={
                **_auth_headers(shared_secret),
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
