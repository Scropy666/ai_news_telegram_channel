import asyncio
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import structlog

from src.config import settings
from src.database.models import RawTweet, ScraperResult, SourceType
from src.database.client import get_db
from src.scrapers import hackernews, reddit, devto
from src.scrapers.merger import merge_duplicates

logger = structlog.get_logger()

TOPICS_FILE = Path(__file__).parent.parent.parent / 'config' / 'topics.json'
SOURCES_FILE = Path(__file__).parent.parent.parent / 'config' / 'sources.json'

DEFAULT_QUERIES = [
    {'query': 'AI agents', 'topic': 'ai_agents', 'sources': ['hackernews']},
    {'query': 'LLM', 'topic': 'llm_releases', 'sources': ['hackernews']},
    {'query': 'OpenAI', 'topic': 'openai_anthropic', 'sources': ['hackernews']},
    {'query': 'Anthropic', 'topic': 'openai_anthropic', 'sources': ['hackernews']},
    {'query': 'AI startup', 'topic': 'startups', 'sources': ['hackernews']},
    {'query': 'MCP model context protocol', 'topic': 'devtools', 'sources': ['hackernews']},
    {'query': 'AI developer tools', 'topic': 'devtools', 'sources': ['hackernews']},
]


def load_topics() -> list[dict]:
    if TOPICS_FILE.exists():
        entries = json.load(open(TOPICS_FILE, 'r', encoding='utf-8')).get('queries', DEFAULT_QUERIES)
        # backward compat: добавляем sources если нет
        for e in entries:
            e.setdefault('sources', ['hackernews'])
        return entries
    return DEFAULT_QUERIES


def _load_sources_config() -> dict:
    if SOURCES_FILE.exists():
        return json.load(open(SOURCES_FILE, 'r', encoding='utf-8'))
    return {'reddit': {'subreddits': []}, 'devto': {'tags': []}}


def save_topics(queries: list[dict]) -> None:
    with open(TOPICS_FILE, 'w', encoding='utf-8') as f:
        json.dump({'queries': queries}, f, ensure_ascii=False, indent=2)


def add_topic(query: str) -> dict | None:
    """Добавить тег. Возвращает None если тег уже существует."""
    queries = load_topics()
    query_lower = query.lower().strip()
    if any(q['query'].lower().strip() == query_lower for q in queries):
        return None
    topic_key = re.sub(r'[^a-z0-9]+', '_', query_lower).strip('_')
    entry = {'query': query, 'topic': topic_key, 'sources': ['hackernews']}
    queries.append(entry)
    save_topics(queries)
    return entry


def remove_topic(index: int) -> bool:
    queries = load_topics()
    if index < 1 or index > len(queries):
        return False
    queries.pop(index - 1)
    save_topics(queries)
    return True


async def scrape_by_tags(tags: list[str]) -> list[RawTweet]:
    """Ручной скрапинг по тегам — только HackerNews с расширенным окном."""
    all_tweets: list[RawTweet] = []
    for tag in tags:
        tweets = await hackernews.fetch(tag, topic='manual', hours=24 * 30, min_points=1)
        all_tweets.extend(tweets)
        await asyncio.sleep(0.3)
    return all_tweets


def _save_tweets(tweets: list[RawTweet]) -> int:
    db = get_db()
    seen: set[str] = set()
    rows = []
    for t in tweets:
        if t.id in seen:
            continue
        seen.add(t.id)
        row = t.model_dump()
        row['scraped_at'] = row['scraped_at'].isoformat()
        rows.append(row)
    db.table('raw_tweets').upsert(rows, on_conflict='id').execute()
    return len(rows)


async def run() -> ScraperResult:
    queries = load_topics()
    sources_cfg = _load_sources_config()
    subreddits = sources_cfg.get('reddit', {}).get('subreddits', [])
    devto_tags = sources_cfg.get('devto', {}).get('tags', [])

    all_tweets: list[RawTweet] = []
    errors: list[str] = []

    # ── HackerNews: per-topic query (query-based search, поэтому разный результат на каждый запрос)
    for entry in queries:
        if 'hackernews' not in entry.get('sources', ['hackernews']):
            continue
        try:
            hn_tweets = await hackernews.fetch(entry['query'], entry['topic'])
            all_tweets.extend(hn_tweets)
            await asyncio.sleep(0.5)
        except Exception as e:
            errors.append(f"HN {entry['query']}: {e}")

    # ── Reddit: один раз глобально (subreddits одинаковые для всех тем)
    reddit_enabled = any('reddit' in e.get('sources', []) for e in queries)
    if reddit_enabled and subreddits:
        try:
            rd_tweets = await reddit.fetch_all(subreddits, topic='general')
            all_tweets.extend(rd_tweets)
            logger.info('reddit_batch_done', count=len(rd_tweets))
        except Exception as e:
            errors.append(f"Reddit: {e}")

    # ── Dev.to: один раз глобально (теги из sources.json одинаковые для всех тем)
    devto_enabled = any('devto' in e.get('sources', []) for e in queries)
    if devto_enabled and devto_tags:
        try:
            dv_tweets = await devto.fetch_all(devto_tags, topic='general')
            all_tweets.extend(dv_tweets)
            logger.info('devto_batch_done', count=len(dv_tweets))
        except Exception as e:
            errors.append(f"Dev.to: {e}")

    # ── Дедупликация по id внутри батча
    seen_ids: set[str] = set()
    unique_tweets: list[RawTweet] = []
    for t in all_tweets:
        if t.id not in seen_ids:
            seen_ids.add(t.id)
            unique_tweets.append(t)

    # ── Cross-source мерж по заголовку / URL
    merged_tweets = merge_duplicates(unique_tweets, threshold=settings.merge_similarity_threshold)

    new_saved = 0
    if merged_tweets:
        new_saved = _save_tweets(merged_tweets)

    result = ScraperResult(
        total_fetched=len(merged_tweets),
        new_saved=new_saved,
        errors=errors,
    )
    logger.info('scraper_done', **result.model_dump())
    return result
