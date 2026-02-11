"""
Builds VK Ads API request payloads from CampaignDraft.
These are the exact request bodies that would be sent to VK Ads API.
"""

import json
import logging
from typing import Any

from .models import AdVariant, CampaignDraft

logger = logging.getLogger(__name__)

CAMPAIGN_TYPE_DEFAULT = 1
AD_FORMAT_COMMUNITY_POST = 9


def _get_vk_campaign(draft: CampaignDraft) -> dict[str, Any]:
    return draft.analysis_result.get("vk_campaign") or {}


def _targeting_for_segment(segment: dict, vk: dict) -> dict[str, Any]:
    age_range = (segment.get("age_range") or "").strip()
    age_from = vk.get("age_from", 18)
    age_to = vk.get("age_to", 55)
    if age_range and "-" in age_range:
        parts = age_range.replace(" ", "").split("-")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            age_from = int(parts[0])
            age_to = int(parts[1])
    gender = (segment.get("gender") or "all").lower()
    sex = 0
    if gender == "male":
        sex = 1
    elif gender == "female":
        sex = 2

    targeting: dict[str, Any] = {
        "age_from": age_from,
        "age_to": age_to,
        "sex": sex,
        "country": vk.get("country") or "1",
    }
    if vk.get("region_ids"):
        targeting["regions"] = vk.get("region_ids")
    if vk.get("interest_ids"):
        targeting["interest_ids"] = vk.get("interest_ids")
    return targeting


def build_vk_ads_requests(
    draft: CampaignDraft,
    account_id: str = "0",
) -> list[dict[str, Any]]:
    """
    Returns a list of request payloads as they would be sent to VK Ads API.
    Each item: {"method": "ads.createCampaigns", "params": {...}}
    """
    vk = _get_vk_campaign(draft)
    campaign_name = vk.get("campaign_name") or draft.analysis_result.get("project_summary", "Кампания")[:100]
    day_limit = int(vk.get("budget_daily_rub") or 500) * 100
    all_limit = int(vk.get("budget_total_rub") or 0) * 100
    link_url = vk.get("link_url") or "https://vk.com"
    bid_type = vk.get("bid_type") or "cpc"
    bid_rub = float(vk.get("bid_rub") or 15)
    bid = int(bid_rub * 100)

    requests_out: list[dict[str, Any]] = []

    campaign_data = [
        {
            "name": campaign_name,
            "type": CAMPAIGN_TYPE_DEFAULT,
            "day_limit": str(day_limit) if day_limit else "0",
            "all_limit": str(all_limit) if all_limit else "0",
        }
    ]
    requests_out.append({
        "method": "ads.createCampaigns",
        "params": {
            "account_id": account_id,
            "data": json.dumps(campaign_data, ensure_ascii=False),
        },
    })

    segments = draft.analysis_result.get("audience_segments") or []
    if not segments and draft.ads:
        segments = [{"segment_name": ad.segment_name, "description": ""} for ad in draft.ads]

    ad_groups_data = []
    for i, seg in enumerate(segments):
        name = seg.get("segment_name") or f"Группа {i + 1}"
        targeting = _targeting_for_segment(seg, vk)
        ad_groups_data.append({
            "name": name[:100],
            "campaign_id": "{{campaign_id}}",
            "day_limit": str(day_limit) if day_limit else "0",
            "bid": str(bid),
            "targeting": json.dumps(targeting, ensure_ascii=False),
        })

    requests_out.append({
        "method": "ads.createAdGroups",
        "params": {
            "account_id": account_id,
            "campaign_id": "{{campaign_id}}",
            "data": json.dumps(ad_groups_data, ensure_ascii=False),
        },
    })

    ads_data = []
    for i, ad in enumerate(draft.ads):
        ad_format = AD_FORMAT_COMMUNITY_POST
        group_placeholder = f"{{{{ad_group_id_{i}}}}}" if len(draft.ads) > 1 else "{{ad_group_id}}"
        ads_data.append({
            "campaign_id": "{{campaign_id}}",
            "ad_group_id": group_placeholder,
            "name": (ad.headline or ad.segment_name or f"Объявление {i + 1}")[:100],
            "link_url": link_url,
            "title": (ad.headline or "")[:80],
            "description": (ad.body_text or "")[:800],
            "ad_format": str(ad_format),
        })
    if not ads_data:
        ads_data = [{
            "campaign_id": "{{campaign_id}}",
            "ad_group_id": "{{ad_group_id}}",
            "name": campaign_name[:100],
            "link_url": link_url,
            "title": campaign_name[:80],
            "description": "",
            "ad_format": str(AD_FORMAT_COMMUNITY_POST),
        }]

    requests_out.append({
        "method": "ads.createAds",
        "params": {
            "account_id": account_id,
            "data": json.dumps(ads_data, ensure_ascii=False),
        },
    })

    return requests_out


def build_vk_ads_requests_with_placeholders_resolved(
    draft: CampaignDraft,
    account_id: str = "0",
    campaign_id: str = "",
    ad_group_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Same as build_vk_ads_requests but with campaign_id and ad_group_id placeholders
    replaced by real values (for documentation / preview after campaign and groups are created).
    """
    raw = build_vk_ads_requests(draft, account_id)
    if not campaign_id:
        return raw
    ad_group_ids = ad_group_ids or []
    result = []
    for r in raw:
        params = dict(r["params"])
        if "data" in params:
            data_str = params["data"]
            data_str = data_str.replace("{{campaign_id}}", campaign_id)
            for i, gid in enumerate(ad_group_ids):
                data_str = data_str.replace("{{ad_group_id}}", gid, 1)
            params["data"] = data_str
        if "campaign_id" in params and params["campaign_id"] == "{{campaign_id}}":
            params["campaign_id"] = campaign_id
        result.append({"method": r["method"], "params": params})
    return result
