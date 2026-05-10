from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ── Enums ────────────────────────────────────────────────────────────────────

PostType = Literal['news_digest', 'deep_dive', 'tool_spotlight', 'opinion']
PostStatus = Literal['pending', 'waiting_publish', 'published', 'failed', 'cancelled']
RawTweetStatus = Literal['new', 'processed']
SourceType = Literal['hackernews', 'reddit', 'devto']
CommentActionType = Literal['reaction', 'comment', 'skipped']
CommentActionStatus = Literal['scheduled', 'awaiting_forward', 'done', 'failed']
CommentPersona = Literal['skeptic', 'excited', 'curious', 'ironic', 'neutral']


# ── Database models ───────────────────────────────────────────────────────────

class RawTweet(BaseModel):
    id: str
    text: str
    author: str
    likes: int
    retweets: int
    url: str
    scraped_at: datetime
    topic: str
    status: RawTweetStatus = 'new'
    source: SourceType = 'hackernews'
    merge_group_id: str | None = None


class NewsItem(BaseModel):
    id: str
    text: str
    source_url: str
    author: str
    likes: int
    topic: str
    relevance_score: float = 0.0
    sources: list[SourceType] = Field(default_factory=list)
    merged_count: int = 1


class Post(BaseModel):
    id: str | None = None
    type: PostType
    content: str
    prompt_version: str
    scheduled_at: datetime
    published_at: datetime | None = None
    status: PostStatus = 'pending'
    telegram_message_id: int | None = None
    retry_count: int = 0
    source_tweet_ids: list[str] = Field(default_factory=list)
    image_prompt: str | None = None
    image_url: str | None = None
    image_skipped_reason: str | None = None


class PollOption(BaseModel):
    text: str
    votes: int = 0


class Poll(BaseModel):
    id: str | None = None
    question: str
    options: list[PollOption]
    scheduled_at: datetime
    published_at: datetime | None = None
    status: PostStatus = 'pending'
    telegram_poll_id: str | None = None
    topic: str


class CommentAction(BaseModel):
    id: str
    post_id: str
    channel_message_id: int
    discussion_chat_id: int | None = None
    discussion_message_id: int | None = None
    action_type: CommentActionType
    emoji: str | None = None
    comment_text: str | None = None
    persona: CommentPersona | None = None
    scheduled_at: datetime
    executed_at: datetime | None = None
    status: CommentActionStatus = 'scheduled'
    error: str | None = None


class Analytics(BaseModel):
    post_id: str
    views: int = 0
    reactions: int = 0
    shares: int = 0
    recorded_at: datetime


# ── Agent result models ───────────────────────────────────────────────────────

class ScraperResult(BaseModel):
    total_fetched: int
    new_saved: int
    errors: list[str] = Field(default_factory=list)


class UniquenessResult(BaseModel):
    is_unique: bool
    # confidence зарезервирован для числового порога в будущем (0.0–1.0)
    confidence: float
    reason: str


class AnalyzerResult(BaseModel):
    status: Literal['ready', 'duplicate', 'no_data', 'error']
    post_id: str | None = None
    post: Post | None = None
    reason: str | None = None


class PublisherResult(BaseModel):
    success: bool
    published_at: datetime | None = None
    error: str | None = None


# ── Tester models ─────────────────────────────────────────────────────────────

@dataclass
class TestCase:
    name: str
    command: str
    expect_contains: list[str]
    setup_command: str | None = None
    extract_id: bool = False


@dataclass
class TestResult:
    name: str
    passed: bool
    command: str
    response: str = ''
    error: str = ''


@dataclass
class TestReport:
    passed: int
    failed: int
    results: list[TestResult] = field(default_factory=list)

    def summary(self) -> str:
        total = self.passed + self.failed
        icon = '✅' if self.failed == 0 else '❌'
        lines = [f'{icon} Тесты: {self.passed}/{total} прошли\n']
        for r in self.results:
            status = '✅' if r.passed else '❌'
            lines.append(f'{status} {r.name}')
            if not r.passed:
                lines.append(f'   Команда: {r.command}')
                lines.append(f'   Ошибка: {r.error}')
        return '\n'.join(lines)


# ── Agent event (in-memory, Coordinator ←→ Sub-agents) ───────────────────────

@dataclass
class AgentEvent:
    agent: Literal['scraper', 'analyzer', 'publisher', 'tester']
    status: Literal['success', 'failure', 'duplicate', 'test_failed', 'no_data']
    payload: dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)
