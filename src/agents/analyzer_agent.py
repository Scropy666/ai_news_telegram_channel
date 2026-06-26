import json
import re
import structlog
from datetime import datetime, timezone, timedelta
from pathlib import Path

from groq import AsyncGroq

from src.config import settings
from src.database.models import (
    RawTweet, NewsItem, Post, PostType,
    UniquenessResult, AnalyzerResult,
)
from src.database.client import get_db
from src.generator.content_generator import generate_post, save_post, get_recent_published_posts
from src.generator.prompt_registry import get_prompt
from src.agent_core.heat import compute_heat

logger = structlog.get_logger()

client = AsyncGroq(api_key=settings.groq_api_key)
MODEL = 'llama-3.3-70b-versatile'
MAX_POSTS = 5          # макс. уникальных постов на выходе
MAX_INPUT_ITEMS = 20   # макс. статей на входе после фильтрации

# Типы постов для ротации при генерации батча
POST_TYPES_ROTATION: list[PostType] = ['news_digest', 'deep_dive', 'tool_spotlight', 'opinion']

SPAM_PATTERNS = [
    r'follow me', r'giveaway', r'dm for', r'buy now',
    r'sign up', r'limited offer', r'💰', r'🚀🚀🚀',
]

HIGH_VALUE_KEYWORDS = [
    'release', 'launch', 'announce', 'breakthrough', 'new model',
    'funding', 'raised', 'agent', 'open source', 'paper', 'benchmark',
    'GPT', 'Claude', 'Gemini', 'Llama', 'Mistral', 'API', 'MCP',
    'выпустили', 'запустили', 'анонс', 'модель', 'агент',
]


# ── Filter & Rank ─────────────────────────────────────────────────────────────

def _is_spam(tweet: RawTweet) -> bool:
    text_lower = tweet.text.lower()
    return any(re.search(p, text_lower) for p in SPAM_PATTERNS)


def _load_topic_keywords() -> list[str]:
    """Load query strings from topics.json and split into searchable tokens."""
    topics_path = Path(__file__).parent.parent.parent / 'config' / 'topics.json'
    try:
        data = json.loads(topics_path.read_text(encoding='utf-8'))
    except Exception:
        return []
    keywords: set[str] = set()
    for entry in data.get('queries', []):
        query = entry.get('query', '')
        keywords.add(query)
        for token in query.split():
            if len(token) >= 3:
                keywords.add(token)
    return list(keywords)


def _relevance_score(tweet: RawTweet, topic_keywords: list[str]) -> float:
    score = 0.0
    text_lower = tweet.text.lower()
    for kw in HIGH_VALUE_KEYWORDS + topic_keywords:
        if kw.lower() in text_lower:
            score += 0.15
    score += min(tweet.likes / 10_000, 0.4)
    score += min(tweet.retweets / 2_000, 0.2)
    return min(score, 1.0)


def _filter_and_rank(tweets: list[RawTweet]) -> list[NewsItem]:
    topic_keywords = _load_topic_keywords()

    # Группируем по merge_group_id — каждая группа даёт один NewsItem
    groups: dict[str, list[RawTweet]] = {}
    ungrouped: list[RawTweet] = []
    for tweet in tweets:
        if _is_spam(tweet):
            continue
        if tweet.merge_group_id:
            groups.setdefault(tweet.merge_group_id, []).append(tweet)
        else:
            ungrouped.append(tweet)

    items: list[NewsItem] = []
    seen_texts: set[str] = set()

    def _make_item(group: list[RawTweet]) -> NewsItem | None:
        # Берём представителя с наибольшим engagement
        best = max(group, key=lambda t: t.likes)
        key = best.text[:80].lower().strip()
        if key in seen_texts:
            return None
        seen_texts.add(key)
        total_likes = min(sum(t.likes for t in group), 10_000)  # cap чтобы не ломать scoring
        score = _relevance_score(best, topic_keywords)
        # Бонус за мульти-источник
        if len(group) > 1:
            score = min(score + 0.1 * (len(group) - 1), 1.0)
        sources = list({t.source for t in group})
        total_comments = sum(t.retweets for t in group)
        hb = compute_heat(
            likes=total_likes,
            num_comments=total_comments,
            merged_count=len(group),
            num_sources=len(sources),
            created_at_source=best.created_at_source,
        )
        return NewsItem(
            id=best.id,
            text=best.text,
            source_url=best.url,
            author=best.author,
            likes=total_likes,
            topic=best.topic,
            relevance_score=score,
            sources=sources,
            merged_count=len(group),
            num_comments=total_comments,
            created_at_source=best.created_at_source,
            heat=hb.total,
            heat_breakdown=hb.as_dict(),
        )

    for group in groups.values():
        item = _make_item(group)
        if item and (item.relevance_score >= 0.15 or item.topic == 'manual'):
            items.append(item)

    for tweet in ungrouped:
        item = _make_item([tweet])
        if item and (item.relevance_score >= 0.15 or tweet.topic == 'manual'):
            items.append(item)

    items.sort(key=lambda x: x.heat, reverse=True)
    logger.info('filter_done', input=len(tweets), output=len(items))
    return items


def _group_by_topic(items: list[NewsItem]) -> dict[str, list[NewsItem]]:
    groups: dict[str, list[NewsItem]] = {}
    for item in items:
        groups.setdefault(item.topic, []).append(item)
    return groups


# ── Load from DB ──────────────────────────────────────────────────────────────

def _load_new_tweets(hours: int = 24) -> list[RawTweet]:
    db = get_db()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = (
        db.table('raw_tweets')
        .select('*')
        .eq('status', 'new')
        .gte('scraped_at', since)
        .order('scraped_at', desc=True)
        .execute()
        .data
    )
    return [RawTweet(**r) for r in rows]


def _mark_tweets_processed(tweet_ids: list[str]) -> None:
    if not tweet_ids:
        return
    db = get_db()
    db.table('raw_tweets').update({'status': 'processed'}).in_('id', tweet_ids).execute()


# ── Uniqueness Check ──────────────────────────────────────────────────────────

def _format_recent_posts(posts: list[str]) -> str:
    if not posts:
        return '(нет опубликованных постов)'
    return '\n\n---\n\n'.join(
        f'Пост {i+1}:\n{p[:600]}' for i, p in enumerate(posts)
    )


async def _check_uniqueness(new_post: str, context_posts: list[str]) -> UniquenessResult:
    """Проверить уникальность нового поста относительно списка context_posts."""
    if not context_posts:
        return UniquenessResult(is_unique=True, confidence=1.0, reason='Нет постов для сравнения')

    prompt_template, _ = get_prompt('uniqueness_check')
    full_prompt = (
        prompt_template
        .replace('{{NEW_POST}}', new_post)
        .replace('{{RECENT_POSTS}}', _format_recent_posts(context_posts))
    )

    try:
        response = await client.chat.completions.create(
            model=MODEL,
            messages=[{'role': 'user', 'content': full_prompt}],
            max_tokens=128,
            temperature=0.1,
        )
        raw = response.choices[0].message.content.strip()
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not json_match:
            raise ValueError(f'No JSON in response: {raw}')
        data = json.loads(json_match.group())
        return UniquenessResult(
            is_unique=bool(data.get('unique', True)),
            confidence=float(data.get('confidence', 0.5)),
            reason=str(data.get('reason', '')),
        )
    except Exception as e:
        logger.warning('uniqueness_check_failed', error=str(e))
        return UniquenessResult(is_unique=True, confidence=0.5, reason=f'Ошибка проверки: {e}')


# ── Multi-post generation ─────────────────────────────────────────────────────

async def run_multi(
    source_tweets: list[RawTweet] | None = None,
    post_type: PostType | None = None,
    tags: list[str] | None = None,
) -> list[Post]:
    """
    Итерирует по всем группам топиков (до MAX_INPUT_ITEMS статей на входе),
    генерирует пост для каждой группы, отбирает уникальные (до MAX_POSTS).
    Уникальность проверяется относительно опубликованных постов И уже
    сгенерированных в этом батче — чтобы посты не повторяли друг друга.
    source_tweets: если указаны — используем их, иначе загружаем из БД.
    post_type: фиксированный тип для всех постов (ручной запуск).
    tags: теги пользователя — прокидываются в промпт.
    Посты НЕ сохраняются в БД — это ответственность вызывающего кода.
    """
    logger.info('analyzer_multi_start', post_type=post_type, tags=tags)

    tweets = source_tweets if source_tweets is not None else _load_new_tweets(hours=24)
    if not tweets:
        logger.info('analyzer_no_data')
        return []

    items = _filter_and_rank(tweets)[:MAX_INPUT_ITEMS]
    if not items:
        logger.info('analyzer_no_relevant_items')
        return []

    # Группируем по топикам — каждая группа даёт один кандидат-пост
    by_topic = _group_by_topic(items)
    topic_groups = list(by_topic.values())

    # Контекст уникальности: опубликованные + сгенерированные в этом батче
    published = get_recent_published_posts(limit=settings.uniqueness_recent_posts)
    generated: list[Post] = []
    rejected_count = 0

    for i, group in enumerate(topic_groups):
        if len(generated) >= MAX_POSTS:
            break

        # Ротируем тип поста для разнообразия; если задан явно — используем его
        pt: PostType = post_type if post_type else POST_TYPES_ROTATION[i % len(POST_TYPES_ROTATION)]
        source_items = group[:5]

        try:
            post = await generate_post(
                source_items,
                post_type=pt,
                scheduled_at=datetime.now(timezone.utc),
                tags=tags,
            )
        except Exception as e:
            logger.warning('generate_post_failed', post_type=pt, error=str(e))
            continue

        # Проверяем уникальность: vs опубликованных + vs уже сгенерированных в батче
        context = published + [p.content for p in generated]
        uniqueness = await _check_uniqueness(post.content, context)

        logger.info(
            'uniqueness_result',
            post_type=pt,
            topic=group[0].topic,
            is_unique=uniqueness.is_unique,
            confidence=uniqueness.confidence,
            reason=uniqueness.reason,
        )

        if uniqueness.is_unique:
            generated.append(post)
        else:
            rejected_count += 1

    _mark_tweets_processed([t.id for t in tweets])
    logger.info('analyzer_multi_done', generated=len(generated), rejected=rejected_count)
    return generated


# ── Ranked items (no generation) ─────────────────────────────────────────────

def get_ranked_items(
    source_tweets: list[RawTweet] | None = None,
    limit: int = MAX_INPUT_ITEMS,
) -> list[NewsItem]:
    """Вернуть отфильтрованные и отранжированные по heat NewsItem БЕЗ генерации постов."""
    tweets = source_tweets if source_tweets is not None else _load_new_tweets(hours=24)
    if not tweets:
        return []
    return _filter_and_rank(tweets)[:limit]


def load_new_tweets(hours: int = 24) -> list[RawTweet]:
    """Публичная обёртка: загрузить необработанные твиты (status='new')."""
    return _load_new_tweets(hours=hours)


def mark_tweets_processed(tweet_ids: list[str]) -> None:
    """Публичная обёртка: пометить твиты обработанными (status='processed').

    Нужна авто-ветке pipeline: после рассмотрения батча твиты должны
    помечаться обработанными, иначе они будут переанализироваться каждый прогон.
    """
    _mark_tweets_processed(tweet_ids)


# ── Run with editor decisions ─────────────────────────────────────────────────

async def run_with_decisions(
    items: list[NewsItem],
    decisions: list['EditorDecision'],
    tags: list[str] | None = None,
) -> list[tuple[Post, str]]:
    """Сгенерировать посты по одобренным решениям редактора.

    Генерирует только items с action == 'publish'. Тип поста берётся из decision.post_type.
    Проверяет уникальность (анти-паттерн: публикация без проверки запрещена).
    Возвращает пары (Post, reason) для каждого одобренного и уникального поста.
    """
    from src.database.models import EditorDecision

    logger.info('run_with_decisions_start', items=len(items), decisions=len(decisions))

    # Индекс items по id для O(1) поиска
    item_index: dict[str, NewsItem] = {it.id: it for it in items}

    # Отобрать только publish-решения, отсортированные по priority затем по heat
    publish_decisions = sorted(
        [d for d in decisions if d.action == 'publish'],
        key=lambda d: (d.priority, -item_index.get(d.item_id, NewsItem(
            id='', text='', source_url='', author='', likes=0, topic=''
        )).heat),
    )

    if not publish_decisions:
        logger.info('run_with_decisions_no_publish')
        return []

    # Контекст уникальности: опубликованные + сгенерированные в этом батче
    published = get_recent_published_posts(limit=settings.uniqueness_recent_posts)
    results: list[tuple[Post, str]] = []
    rejected_count = 0

    for decision in publish_decisions:
        item = item_index.get(decision.item_id)
        if item is None:
            logger.warning('run_with_decisions_item_not_found', item_id=decision.item_id)
            continue

        try:
            post = await generate_post(
                [item],
                post_type=decision.post_type,
                scheduled_at=datetime.now(timezone.utc),
                tags=tags,
            )
        except Exception as e:
            logger.warning('run_with_decisions_generate_failed',
                           item_id=decision.item_id, post_type=decision.post_type, error=str(e))
            continue

        # Проверяем уникальность: vs опубликованных + vs уже сгенерированных в батче
        context = published + [p.content for p, _ in results]
        uniqueness = await _check_uniqueness(post.content, context)

        logger.info(
            'run_with_decisions_uniqueness',
            item_id=decision.item_id,
            post_type=decision.post_type,
            is_unique=uniqueness.is_unique,
            confidence=uniqueness.confidence,
            reason=uniqueness.reason,
        )

        if uniqueness.is_unique:
            results.append((post, decision.reason))
        else:
            rejected_count += 1
            logger.info('run_with_decisions_rejected_duplicate',
                        item_id=decision.item_id, reason=uniqueness.reason)

    logger.info('run_with_decisions_done', generated=len(results), rejected=rejected_count)
    return results
