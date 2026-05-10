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

logger = structlog.get_logger()

client = AsyncGroq(api_key=settings.groq_api_key)
MODEL = 'llama-3.3-70b-versatile'
MAX_POST_LENGTH = 950  # must fit in Telegram photo caption (1024 limit minus header margin)


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
            '\n=== ПРИМЕРЫ ПРОШЛЫХ ПОСТОВ (для соответствия стилю) ===',
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
    full_prompt = prompt_template.replace('{{CONTEXT}}', context)

    logger.info('generating_post', type=post_type, news_count=len(news_items), tags=tags)

    response = await client.chat.completions.create(
        model=MODEL,
        messages=[{'role': 'user', 'content': full_prompt}],
        max_tokens=500,
        temperature=0.8,
    )

    content = response.choices[0].message.content.strip()
    if len(content) > MAX_POST_LENGTH:
        # Trim at word boundary to avoid cutting mid-word
        trimmed = content[:MAX_POST_LENGTH - 1]
        last_space = trimmed.rfind(' ')
        if last_space > MAX_POST_LENGTH * 0.8:
            trimmed = trimmed[:last_space]
        content = trimmed.rstrip('.,;:') + '…'

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
