from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class PostStats:
    post_id: int
    likes: int
    comments: int
    reposts: int
    views: int
    engagement: float
    text: str


@dataclass
class GroupInfo:
    id: int
    screen_name: str
    name: str
    description: str
    members_count: int
    status: str = ""


@dataclass
class GroupAnalysis:
    group: GroupInfo
    posts: list[PostStats] = field(default_factory=list)
    top_posts_by_engagement: list[PostStats] = field(default_factory=list)


@dataclass
class AdVariant:
    segment_name: str
    headline: str
    body_text: str
    cta: str
    visual_concept: str
    image_prompt_short: str
    image_prompt: str = ""
    image_path: Optional[str] = None


@dataclass
class CampaignDraft:
    analysis_result: dict[str, Any] = field(default_factory=dict)
    ads: list[AdVariant] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    image_path: Optional[str] = None
    image_url: Optional[str] = None
    audience_description: str = ""
    text: str = ""
