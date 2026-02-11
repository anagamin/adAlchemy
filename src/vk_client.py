import logging
import re
from typing import Optional

import vk_api

from .config import settings
from .models import GroupAnalysis, GroupInfo, PostStats

logger = logging.getLogger(__name__)


def _parse_group_id_or_screen_name(link: str) -> Optional[str]:
    link = link.strip().rstrip("/")
    patterns = [
        r"(?:https?://)?(?:www\.)?vk\.com/(?:club|public|event)?(\d+)",
        r"(?:https?://)?(?:www\.)?vk\.com/([a-zA-Z0-9_.]+)",
        r"(?:https?://)?(?:m\.)?vk\.com/(?:club|public)?(\d+)",
        r"(?:https?://)?(?:m\.)?vk\.com/([a-zA-Z0-9_.]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, link, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _engagement(likes: int, comments: int, reposts: int, views: int) -> float:
    if views <= 0:
        return 0.0
    return (likes + comments * 2 + reposts * 3) / views


def fetch_group_analysis(link: str, posts_count: int = 50) -> GroupAnalysis:
    logger.info("vk: fetch_group_analysis link=%s posts_count=%s", link, posts_count)
    group_id_or_name = _parse_group_id_or_screen_name(link)
    if not group_id_or_name:
        raise ValueError("Не удалось извлечь ID или short_name группы из ссылки")

    vk = vk_api.VkApi(token=settings.vk_access_token, api_version=settings.vk_api_version)
    api = vk.get_api()

    groups_raw = api.groups.getById(group_ids=group_id_or_name)
    if not groups_raw:
        raise ValueError("Группа не найдена")

    g = groups_raw[0]
    group = GroupInfo(
        id=g["id"],
        screen_name=g.get("screen_name", ""),
        name=g.get("name", ""),
        description=g.get("description", ""),
        members_count=g.get("members_count", 0),
        status=g.get("status", {}).get("text", "") if isinstance(g.get("status"), dict) else str(g.get("status", "")),
    )

    owner_id = -group.id
    wall = api.wall.get(owner_id=owner_id, count=posts_count, filter="owner")

    posts: list[PostStats] = []
    for item in wall.get("items", []):
        likes = item.get("likes", {}).get("count", 0)
        comments = item.get("comments", {}).get("count", 0)
        reposts = item.get("reposts", {}).get("count", 0)
        views = item.get("views", {}).get("count", 0) if item.get("views") else 0
        text = (item.get("text") or "").strip()
        engagement = _engagement(likes, comments, reposts, views)
        posts.append(
            PostStats(
                post_id=item["id"],
                likes=likes,
                comments=comments,
                reposts=reposts,
                views=views,
                engagement=engagement,
                text=text,
            )
        )

    top = sorted(posts, key=lambda p: p.engagement, reverse=True)[:10]
    logger.info("vk: done group=%s members=%s posts=%s top=%s", group.name, group.members_count, len(posts), len(top))
    return GroupAnalysis(group=group, posts=posts, top_posts_by_engagement=top)
