"""
src/agent_core/tools.py — Phase 4 tool layer for the autonomous agent loop.

Defines AgentContext (runtime state), TOOL_SCHEMAS (OpenAI function-calling
format), and async dispatch(). MUST NOT import src/agents/coordinator.py.
"""
from __future__ import annotations

import structlog
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.database.models import NewsItem, Post
from src.agent_core.goals import guardrail

logger = structlog.get_logger()

VALID_POST_TYPES = ['news_digest', 'deep_dive', 'tool_spotlight', 'opinion']


# ── AgentContext ──────────────────────────────────────────────────────────────

@dataclass
class AgentContext:
    """Изменяемое состояние одного прогона агентного цикла."""
    items_by_id: dict[str, NewsItem] = field(default_factory=dict)
    """Отранжированные кандидаты, загруженные через get_candidates."""

    generated: dict[str, tuple] = field(default_factory=dict)
    """item_id → (Post, reason): черновики, сгенерированные но ещё не поданные на review."""

    to_review: list[tuple] = field(default_factory=list)
    """(Post, reason): коммитнутые черновики, которые coordinator отправит на review."""

    published_context: list[str] = field(default_factory=list)
    """Тексты последних опубликованных постов — контекст для проверки уникальности."""

    submitted_count: int = 0
    """Количество постов, поданных на review в этом прогоне."""

    remaining_day_slots: int = 0
    """Оставшиеся слоты публикации за сутки (max_posts_per_day − уже опубликовано)."""

    max_per_run: int = 5
    """Максимум постов за один прогон (guardrail max_posts_per_run)."""

    tags: list[str] | None = None
    """Необязательные пользовательские теги — прокидываются в generate_and_check."""

    finished: bool = False
    """True, когда агент вызвал инструмент finish."""

    summary: str = ''
    """Итоговое резюме агента (заполняется через finish)."""


# ── Tool schemas (OpenAI function-calling format) ─────────────────────────────

TOOL_SCHEMAS: list[dict] = [
    {
        'type': 'function',
        'function': {
            'name': 'get_candidates',
            'description': (
                'Получить список кандидатов для публикации (ранжированных по heat-score). '
                'Вызови эту функцию ПЕРВОЙ, чтобы увидеть доступные новости. '
                'Если список пустой или не хватает интересных тем — используй scrape_topic.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {},
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'scrape_topic',
            'description': (
                'Дополнительно скрапить HackerNews по произвольному запросу и обновить список кандидатов. '
                'Используй, если текущие кандидаты не содержат нужной темы или их слишком мало. '
                'После вызова список кандидатов обновляется автоматически.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {
                        'type': 'string',
                        'description': 'Поисковый запрос на английском (например: "Claude 4 release" или "AI agents benchmark").',
                    },
                },
                'required': ['query'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'generate_draft',
            'description': (
                'Сгенерировать черновик поста для выбранного кандидата и автоматически проверить уникальность. '
                'Если пост неуникален — возвращает ok=false с причиной; не вызывай submit_for_review для него. '
                'Перед submit_for_review ОБЯЗАТЕЛЬНО вызови generate_draft для этого item_id.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'item_id': {
                        'type': 'string',
                        'description': 'ID новости из списка кандидатов (поле id в get_candidates).',
                    },
                    'post_type': {
                        'type': 'string',
                        'enum': VALID_POST_TYPES,
                        'description': (
                            'Формат поста: '
                            'news_digest — дайджест нескольких новостей; '
                            'deep_dive — глубокий разбор одной крупной темы; '
                            'tool_spotlight — обзор конкретного инструмента или библиотеки; '
                            'opinion — авторское мнение на тренд или спор.'
                        ),
                    },
                },
                'required': ['item_id', 'post_type'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'submit_for_review',
            'description': (
                'Передать ранее сгенерированный уникальный черновик на проверку редактором-человеком. '
                'Перед вызовом ОБЯЗАТЕЛЬНО вызови generate_draft для этого item_id. '
                'Возвращает slots_left — сколько постов ещё можно отправить в этом прогоне.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'item_id': {
                        'type': 'string',
                        'description': 'ID новости, для которой был вызван generate_draft.',
                    },
                    'reason': {
                        'type': 'string',
                        'description': 'Обоснование выбора на русском языке, одно предложение.',
                    },
                },
                'required': ['item_id', 'reason'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'finish',
            'description': (
                'Завершить рабочий цикл. Вызови, когда отправлены все нужные посты '
                'или нет подходящих кандидатов. Обязателен для корректного завершения прогона.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'summary': {
                        'type': 'string',
                        'description': 'Краткое резюме прогона на русском: что сделано и почему.',
                    },
                },
                'required': ['summary'],
            },
        },
    },
]


# ── Dispatch ──────────────────────────────────────────────────────────────────

async def dispatch(name: str, args: dict, ctx: AgentContext) -> dict:
    """Маршрутизатор инструментов. Возвращает JSON-serializable dict.
    Никогда не пробрасывает исключение — все ошибки оборачиваются в {'error': ...}."""
    logger.info('agent_tool', tool=name, args=args)

    try:
        if name == 'get_candidates':
            return await _tool_get_candidates(ctx)
        elif name == 'scrape_topic':
            return await _tool_scrape_topic(args, ctx)
        elif name == 'generate_draft':
            return await _tool_generate_draft(args, ctx)
        elif name == 'submit_for_review':
            return _tool_submit_for_review(args, ctx)
        elif name == 'finish':
            return _tool_finish(args, ctx)
        else:
            return {'error': f'unknown tool {name}'}
    except Exception as e:
        logger.warning('agent_tool_error', tool=name, error=str(e))
        return {'error': str(e)}


async def _tool_get_candidates(ctx: AgentContext) -> dict:
    if not ctx.items_by_id:
        from src.agents import analyzer_agent
        items = analyzer_agent.get_ranked_items()
        ctx.items_by_id = {item.id: item for item in items}

    now = datetime.now(timezone.utc)
    candidates = []
    for item in ctx.items_by_id.values():
        age_hours = None
        try:
            if item.created_at_source:
                src_dt = item.created_at_source
                if src_dt.tzinfo is None:
                    src_dt = src_dt.replace(tzinfo=timezone.utc)
                delta = now - src_dt
                if delta.total_seconds() >= 0:
                    age_hours = round(delta.total_seconds() / 3600, 1)
        except Exception:
            age_hours = None

        candidates.append({
            'id': item.id,
            'title': item.text[:120],
            'heat': item.heat,
            'sources': item.sources,
            'likes': item.likes,
            'num_comments': item.num_comments,
            'age_hours': age_hours,
            'breakdown': item.heat_breakdown,
        })

    return {'candidates': candidates}


async def _tool_scrape_topic(args: dict, ctx: AgentContext) -> dict:
    query = args.get('query', '')
    if not query:
        return {'error': 'query is required'}

    from src.agents import scraper_agent, analyzer_agent
    try:
        tweets = await scraper_agent.scrape_by_tags([query])
        saved = scraper_agent._save_tweets(tweets)
        items = analyzer_agent.get_ranked_items()
        ctx.items_by_id = {item.id: item for item in items}
        return {
            'scraped': len(tweets),
            'saved': saved,
            'candidates_now': len(ctx.items_by_id),
        }
    except Exception as e:
        return {'error': str(e)}


async def _tool_generate_draft(args: dict, ctx: AgentContext) -> dict:
    item_id = args.get('item_id', '')
    post_type = args.get('post_type', '')

    item = ctx.items_by_id.get(item_id)
    if item is None:
        return {'error': 'unknown item_id'}

    if post_type not in VALID_POST_TYPES:
        return {'error': f'invalid post_type {post_type!r}; must be one of {VALID_POST_TYPES}'}

    # Uniqueness context = published posts + posts already committed to review
    context = ctx.published_context + [p.content for (p, _) in ctx.to_review]

    from src.agents import analyzer_agent
    post, uniqueness = await analyzer_agent.generate_and_check(
        item, post_type, context, tags=ctx.tags
    )

    if post is None:
        return {'ok': False, 'reason': uniqueness.reason}

    ctx.generated[item_id] = (post, '')
    return {
        'ok': True,
        'item_id': item_id,
        'is_unique': uniqueness.is_unique,
        'uniqueness_reason': uniqueness.reason,
        'preview': post.content[:200],
    }


def _tool_submit_for_review(args: dict, ctx: AgentContext) -> dict:
    item_id = args.get('item_id', '')
    reason = args.get('reason', '')

    if ctx.submitted_count >= ctx.max_per_run:
        return {'ok': False, 'reason': 'max_posts_per_run reached'}

    if ctx.submitted_count >= ctx.remaining_day_slots:
        return {'ok': False, 'reason': 'max_posts_per_day reached'}

    if item_id not in ctx.generated:
        return {'ok': False, 'reason': 'no draft generated for this item; call generate_draft first'}

    post, _ = ctx.generated[item_id]
    ctx.to_review.append((post, reason))
    ctx.submitted_count += 1
    del ctx.generated[item_id]

    slots_left = max(min(ctx.max_per_run, ctx.remaining_day_slots) - ctx.submitted_count, 0)
    return {
        'ok': True,
        'submitted': ctx.submitted_count,
        'slots_left': slots_left,
    }


def _tool_finish(args: dict, ctx: AgentContext) -> dict:
    summary = args.get('summary', '')
    ctx.finished = True
    ctx.summary = summary
    return {'ok': True}
