import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

import httpx

from .config import settings

logger = logging.getLogger(__name__)

GPTUNNEL_MEDIA_BASE = "https://gptunnel.ru/v1/media"
POLL_INTERVAL = 2.0
POLL_MAX_WAIT = 120.0


async def generate_image(
    prompt: str,
    *,
    aspect_ratio: str = "1:1",
    images: Optional[list[str]] = None,
    output_path: Optional[Path] = None,
    timeout: float = 130.0,
) -> Optional[Path]:
    if not settings.gptunnel_api_key:
        logger.warning("gptunnel_api_key not set, skipping image generation")
        return None

    headers = {
        "Authorization": settings.gptunnel_api_key,
        "Content-Type": "application/json",
    }
    body: dict = {
        "model": settings.gptunnel_image_model,
        "prompt": prompt,
        "ar": aspect_ratio,
    }
    if images:
        body["images"] = images

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{GPTUNNEL_MEDIA_BASE}/create",
            headers=headers,
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            logger.warning("gptunnel create failed: %s", data)
            return None
        task_id = data.get("id")
        if not task_id:
            logger.warning("gptunnel create: no task id in %s", data)
            return None

        logger.info("gptunnel task_id=%s polling for result", task_id)
        elapsed = 0.0
        while elapsed < POLL_MAX_WAIT:
            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL
            result_resp = await client.post(
                f"{GPTUNNEL_MEDIA_BASE}/result",
                headers=headers,
                json={"task_id": task_id},
            )
            result_resp.raise_for_status()
            result = result_resp.json()
            if result.get("code") != 0:
                logger.warning("gptunnel result failed: %s", result)
                return None
            status = result.get("status")
            if status == "done":
                url = result.get("url")
                if not url:
                    logger.warning("gptunnel result done but no url: %s", result)
                    return None
                return await _download_image(client, url, output_path)
            if status in ("failed", "error"):
                logger.warning("gptunnel task failed: %s", result)
                return None

        logger.warning("gptunnel task timed out task_id=%s", task_id)
        return None


async def _download_image(
    client: httpx.AsyncClient,
    url: str,
    output_path: Optional[Path] = None,
) -> Optional[Path]:
    r = await client.get(url)
    r.raise_for_status()
    data = r.content
    if not data:
        return None
    if output_path is None:
        suffix = ".webp" if ".webp" in url else ".png"
        fd, path = tempfile.mkstemp(suffix=suffix, prefix="ad_")
        os.close(fd)
        output_path = Path(path)
    output_path.write_bytes(data)
    logger.info("saved image to %s (%s bytes)", output_path, len(data))
    return output_path
