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


def _seed_topics_from_file() -> list[dict]:
    """Источник для первичного заполнения таблицы topics, когда она пуста:
    существующий topics.json (сохраняет текущие теги) или DEFAULT_QUERIES."""
    if TOPICS_FILE.exists():
        entries = json.load(open(TOPICS_FILE, 'r', encoding='utf-8')).get('queries', [])
        for e in entries:
            e.setdefault('sources', ['hackernews'])
        return entries or DEFAULT_QUERIES
    return DEFAULT_QUERIES


def _insert_topics(entries: list[dict]) -> None:
    if not entries:
        return
    rows = [
        {'query': e['query'], 'topic': e['topic'], 'sources': e.get('sources', ['hackernews'])}
        for e in entries
    ]
    get_db().table('topics').insert(rows).execute()


def load_topics() -> list[dict]:
    """Загрузить теги из Supabase. При первом запуске (пустая таблица) —
    seed из topics.json/DEFAULT_QUERIES. Хранение в БД нужно, чтобы теги,
    добавленные через /add_topic, переживали редеплои Railway (эфемерная ФС).

    Если таблицы topics ещё нет (DDL не выполнен) — graceful fallback на файл,
    чтобы деплой не зависел от порядка «создать таблицу / задеплоить код»."""
    db = get_db()
    try:
        rows = db.table('topics').select('query, topic, sources').order('id').execute().data
        if not rows:
            _insert_topics(_seed_topics_from_file())
            rows = db.table('topics').select('query, topic, sources').order('id').execute().data
    except Exception as e:
        logger.warning('topics_table_unavailable_fallback_file', error=str(e))
        return _seed_topics_from_file()
    return [
        {'query': r['query'], 'topic': r['topic'], 'sources': r.get('sources') or ['hackernews']}
        for r in rows
    ]


def _load_sources_config() -> dict:
    if SOURCES_FILE.exists():
        return json.load(open(SOURCES_FILE, 'r', encoding='utf-8'))
    return {'reddit': {'subreddits': []}, 'devto': {'tags': []}}


def save_topics(queries: list[dict]) -> None:
    """Заменить все теги переданным списком (snapshot-restore — используется
    тестами для восстановления состояния). id != 0 = все строки таблицы."""
    db = get_db()
    db.table('topics').delete().neq('id', 0).execute()
    _insert_topics(queries)


def add_topic(query: str) -> dict | None:
    """Добавить тег. Возвращает None если тег уже существует."""
    query_lower = query.lower().strip()
    if any(q['query'].lower().strip() == query_lower for q in load_topics()):
        return None
    topic_key = re.sub(r'[^a-z0-9]+', '_', query_lower).strip('_')
    entry = {'query': query, 'topic': topic_key, 'sources': ['hackernews']}
    try:
        get_db().table('topics').insert(entry).execute()
    except Exception as e:
        logger.warning('add_topic_insert_failed', query=query, error=str(e))
        return None
    return entry


def remove_topic(index: int) -> bool:
    db = get_db()
    rows = db.table('topics').select('id').order('id').execute().data
    if index < 1 or index > len(rows):
        return False
    target_id = rows[index - 1]['id']
    db.table('topics').delete().eq('id', target_id).execute()
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
        if row.get('created_at_source'):
            row['created_at_source'] = row['created_at_source'].isoformat()
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

    # ── HackerNews + Reddit keyword search: per-topic query
    reddit_search_pairs: list[tuple[str, str]] = []
    for entry in queries:
        sources = entry.get('sources', ['hackernews'])
        if 'hackernews' in sources:
            try:
                hn_tweets = await hackernews.fetch(entry['query'], entry['topic'])
                all_tweets.extend(hn_tweets)
                await asyncio.sleep(0.5)
            except Exception as e:
                errors.append(f"HN {entry['query']}: {e}")
        if 'reddit' in sources:
            reddit_search_pairs.append((entry['query'], entry['topic']))

    if reddit_search_pairs:
        try:
            rd_search_tweets = await reddit.search_all(reddit_search_pairs)
            all_tweets.extend(rd_search_tweets)
            logger.info('reddit_search_done', count=len(rd_search_tweets))
        except Exception as e:
            errors.append(f"Reddit search: {e}")

    # ── Reddit subreddit fetch (legacy, requires OAuth + sources.json subreddits config)
    if subreddits:
        try:
            rd_tweets = await reddit.fetch_all(subreddits, topic='general')
            all_tweets.extend(rd_tweets)
            logger.info('reddit_subreddit_done', count=len(rd_tweets))
        except Exception as e:
            errors.append(f"Reddit subreddits: {e}")

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
