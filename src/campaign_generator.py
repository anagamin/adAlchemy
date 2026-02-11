import json
import logging
from pathlib import Path
from typing import Any, Optional

from .config import settings
from .image_client import generate_image
from .llm_client import chat_completion, extract_json_from_text
from .models import AdVariant, CampaignDraft, GroupAnalysis
from .prompts import (
    SYSTEM_ADS,
    SYSTEM_ANALYSIS,
    SYSTEM_IMAGE_PROMPT,
    build_user_ads,
    build_user_analysis,
    build_user_image_prompt,
)

logger = logging.getLogger(__name__)


def _top_posts_for_prompt(analysis: GroupAnalysis, limit: int = 15) -> list[dict[str, Any]]:
    return [
        {
            "text": p.text,
            "engagement": p.engagement,
            "likes": p.likes,
            "comments": p.comments,
            "reposts": p.reposts,
            "views": p.views,
        }
        for p in analysis.top_posts_by_engagement[:limit]
    ]


def _segment_description(analysis_result: dict, segment_name: str) -> str:
    for seg in analysis_result.get("audience_segments", []):
        if seg.get("segment_name") == segment_name:
            return seg.get("description", segment_name)
    return segment_name


async def _step1_analysis(analysis: GroupAnalysis) -> dict[str, Any]:
    logger.info("campaign: step1_analysis start group=%s", analysis.group.name)
    top = _top_posts_for_prompt(analysis)
    user = build_user_analysis(
        analysis.group.name,
        analysis.group.description,
        analysis.group.members_count,
        top,
    )
    raw = await chat_completion(
        [{"role": "system", "content": SYSTEM_ANALYSIS}, {"role": "user", "content": user}],
        json_mode=True,
    )
    try:
        out = extract_json_from_text(raw)
    except Exception as e:
        logger.warning("campaign: step1 extract_json failed: %s | raw_preview=%s", e, (raw or "")[:800])
        raise
    if not out.get("vk_campaign"):
        out["vk_campaign"] = {
            "campaign_name": out.get("project_summary", "Кампания")[:80] or "Кампания",
            "budget_daily_rub": 500,
            "budget_total_rub": 10000,
            "link_url": "https://vk.com",
            "bid_type": "cpc",
            "bid_rub": 15,
            "age_from": 18,
            "age_to": 55,
            "country": "1",
            "region_ids": "1,77",
            "interest_ids": "",
        }
    logger.info("campaign: step1_analysis done")
    return out


async def _step2_ads(analysis_result: dict) -> list[dict[str, Any]]:
    logger.info("campaign: step2_ads start")
    analysis_json = json.dumps(analysis_result, ensure_ascii=False, indent=2)
    user = build_user_ads(analysis_json)
    raw = await chat_completion(
        [{"role": "system", "content": SYSTEM_ADS}, {"role": "user", "content": user}],
        json_mode=True,
    )
    try:
        data = extract_json_from_text(raw)
    except Exception as e:
        logger.warning("campaign: step2 extract_json failed: %s | raw_preview=%s", e, (raw or "")[:800])
        raise
    ads = data.get("ads") or []
    if not isinstance(ads, list):
        ads = [ads] if ads else []
    logger.info("campaign: step2_ads done ads_count=%s", len(ads))
    return ads


async def _step3_image_prompt(
    headline: str,
    visual_concept: str,
    segment_description: str,
) -> str:
    user = build_user_image_prompt(headline, visual_concept, segment_description)
    raw = await chat_completion(
        [{"role": "system", "content": SYSTEM_IMAGE_PROMPT}, {"role": "user", "content": user}],
        json_mode=False,
    )
    return raw.strip()


async def generate_campaign(analysis: GroupAnalysis, image_path: Optional[Path] = None) -> CampaignDraft:
    logger.info("campaign: generate_campaign start")
    analysis_result = await _step1_analysis(analysis)
    ads_raw = await _step2_ads(analysis_result)
    keywords = analysis_result.get("keywords") or []

    ad_variants: list[AdVariant] = []
    for a in ads_raw:
        segment_name = a.get("segment_name") or "Аудитория"
        headline = a.get("headline") or ""
        body_text = a.get("body_text") or ""
        cta = a.get("cta") or ""
        visual_concept = a.get("visual_concept") or ""
        image_prompt_short = a.get("image_prompt_short") or ""
        seg_desc = _segment_description(analysis_result, segment_name)
        image_prompt = await _step3_image_prompt(headline, visual_concept, seg_desc)
        ad_variants.append(
            AdVariant(
                segment_name=segment_name,
                headline=headline,
                body_text=body_text,
                cta=cta,
                visual_concept=visual_concept,
                image_prompt_short=image_prompt_short,
                image_prompt=image_prompt,
            )
        )

    if settings.gptunnel_api_key:
        for i, ad in enumerate(ad_variants):
            if not ad.image_prompt:
                continue
            try:
                path = await generate_image(
                    ad.image_prompt,
                    aspect_ratio="1:1",
                )
                if path and path.exists():
                    ad.image_path = str(path)
                    logger.info("campaign: image for ad %s saved to %s", i + 1, ad.image_path)
            except Exception as e:
                logger.warning(
                    "campaign: image generation for ad %s failed: %s %s",
                    i + 1,
                    type(e).__name__,
                    e or "(no message)",
                )

    logger.info("campaign: generate_campaign done ads=%s", len(ad_variants))
    return CampaignDraft(
        analysis_result=analysis_result,
        ads=ad_variants,
        keywords=keywords,
    )
