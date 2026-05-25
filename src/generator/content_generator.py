import json
import structlog
from datetime import datetime, timezone
from uuid import uuid4
from groq import AsyncGroq

from src.config import settings
from src.database.models import NewsItem, Post, PostType
from src.database.client import get_db
from src.generator.prompt_registry import get_prompt
from src.generator.image_generator import generate_image_prompt, build_image_url, make_seed
from src.settings_store import get_post_language, LANGUAGE_LABELS

logger = structlog.get_logger()

client = AsyncGroq(api_key=settings.groq_api_key)
MODEL = 'llama-3.3-70b-versatile'
MAX_POST_LENGTH = 950  # must fit in Telegram photo caption (1024 limit minus header margin)
MIN_POST_LENGTH = 600  # posts shorter than this are rejected and retried
_SOURCE_SUFFIX_RESERVE = 120  # chars reserved for "\n\n🔗 <url>"


_SOURCE_LABELS = {'hackernews': 'HackerNews', 'reddit': 'Reddit', 'devto': 'Dev.to'}


def _format_news_for_prompt(items: list[NewsItem]) -> str:
    lines = []
    for i, item in enumerate(items[:5], 1):
        source_label = ' + '.join(_SOURCE_LABELS.get(s, s) for s in (item.sources or ['hackernews']))
        merged_note = f' (обсуждают на {source_label})' if item.merged_count > 1 else f' [{source_label}]'
        lines.append(
            f"{i}. [{item.author}] {item.text}{merged_note}\n"
            f"   Лайки: {item.likes} | Источник: {item.source_url}"
        )
    return '\n\n'.join(lines)


def get_recent_published_posts(limit: int) -> list[str]:
    db = get_db()
    rows = (
        db.table('posts')
        .select('content')
        .in_('status', ['published', 'waiting_publish'])
        .order('scheduled_at', desc=True)
        .limit(limit)
        .execute()
        .data
    )
    return [r['content'] for r in rows]


def _build_context(
    news_items: list[NewsItem],
    similar_posts: list[str],
    tags: list[str] | None = None,
) -> str:
    parts: list[str] = []
    if tags:
        parts += [
            '=== ФОКУС ПОСТА (обязательно) ===',
            (
                f"Пользователь запросил пост по тегам: {', '.join(tags)}.\n"
                f"Пост ДОЛЖЕН быть про эти теги. Если среди новостей ниже есть релевантные — используй их.\n"
                f"Если новости не связаны с тегами — всё равно напиши пост про {', '.join(tags)} "
                f"на основе общеизвестных фактов, но БЕЗ выдуманных цифр/дат/цитат.\n"
                f"Хэштеги в конце — ТОЛЬКО про {', '.join(tags)}, НЕ используй дефолтные #AI #LLM #DevTools."
            ),
        ]
    parts += ['=== НОВОСТИ ДЛЯ ПОСТА ===', _format_news_for_prompt(news_items)]
    if similar_posts and not tags:
        # Прошлые посты как стилевые примеры — только для авто-режима.
        # При ручных тегах они биасят LLM к копированию хэштегов/тем.
        parts += [
            '\n=== ПРИМЕРЫ ПРОШЛЫХ ПОСТОВ (только структура и тон, НЕ язык) ===',
            *[f'---\n{p}' for p in similar_posts],
        ]
    return '\n\n'.join(parts)


async def generate_post(
    news_items: list[NewsItem],
    post_type: PostType,
    scheduled_at: datetime,
    tags: list[str] | None = None,
) -> Post:
    prompt_template, prompt_version = get_prompt(post_type)
    similar_posts = get_recent_published_posts(limit=3)
    context = _build_context(news_items, similar_posts, tags=tags)
    lang = get_post_language()
    lang_label = LANGUAGE_LABELS.get(lang, lang)
    full_prompt = (
        prompt_template
        .replace('{{LANGUAGE}}', lang_label)
        .replace('{{CONTEXT}}', context)
    )
    full_prompt += f'\n\nREMINDER: Write the entire post in {lang_label} only. Do not use any other language regardless of the example posts.'

    logger.info('generating_post', type=post_type, news_count=len(news_items), tags=tags)

    content = ''
    for attempt in range(3):
        prompt = full_prompt
        if attempt > 0:
            prompt += f'\n\n[Предыдущий ответ был слишком коротким ({len(content)} симв.). Напиши развёрнутее — минимум 600 символов.]'

        response = await client.chat.completions.create(
            model=MODEL,
            messages=[{'role': 'user', 'content': prompt}],
            max_tokens=600,
            temperature=0.8,
        )
        candidate = response.choices[0].message.content.strip()

        if len(candidate) > len(content):
            content = candidate  # always keep the longest result

        if len(content) >= MIN_POST_LENGTH:
            break

        logger.warning('post_too_short', attempt=attempt + 1, length=len(content))

    source_url = news_items[0].source_url if news_items else None
    url_suffix = f'\n\n🔗 {source_url}' if source_url else ''
    body_limit = MAX_POST_LENGTH - len(url_suffix)

    if len(content) > body_limit:
        trimmed = content[:body_limit]
        # Prefer cutting at a sentence boundary to avoid incomplete thoughts
        last_sentence_end = max(trimmed.rfind('.'), trimmed.rfind('!'), trimmed.rfind('?'))
        if last_sentence_end > body_limit * 0.7:
            content = trimmed[:last_sentence_end + 1]
        else:
            last_space = trimmed.rfind(' ')
            if last_space > body_limit * 0.8:
                trimmed = trimmed[:last_space]
            content = trimmed.rstrip('.,;:') + '…'

    content = content + url_suffix

    post_id = str(uuid4())
    post = Post(
        id=post_id,
        type=post_type,
        content=content,
        prompt_version=f'{post_type}_v{prompt_version}',
        scheduled_at=scheduled_at,
        status='pending',
        source_tweet_ids=[item.id for item in news_items[:5]],
    )

    if settings.images_enabled:
        img_prompt = await generate_image_prompt(content)
        seed = make_seed(post_id)
        post = post.model_copy(update={
            'image_prompt': img_prompt,
            'image_url': build_image_url(img_prompt, seed),
        })

    logger.info('post_generated', post_id=post.id, length=len(content))
    return post


def save_post(post: Post) -> None:
    db = get_db()
    row = post.model_dump()
    row['scheduled_at'] = row['scheduled_at'].isoformat()
    if row.get('published_at'):
        row['published_at'] = row['published_at'].isoformat()
    db.table('posts').insert(row).execute()
