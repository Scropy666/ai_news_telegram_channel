"""Editor Agent — ИИ-редактор, который решает какие новости публиковать.

Принимает список NewsItem (отранжированных по heat), вызывает LLM и возвращает
список EditorDecision (publish/hold/reject + тип поста + обоснование).

Промпт: config/prompts/editor_decision_v1.0.0.md (через registry — не хардкожен).
Образец парсинга JSON: analyzer_agent._check_uniqueness (re.search + try/except).
"""
from __future__ import annotations

import json
import re
import structlog
from datetime import datetime, timezone

from groq import AsyncGroq

from src.config import settings
from src.database.models import NewsItem, EditorDecision, PostType
from src.generator.prompt_registry import get_prompt
from src.agent_core.goals import guardrail

logger = structlog.get_logger()
client = AsyncGroq(api_key=settings.groq_api_key)
MODEL = 'llama-3.3-70b-versatile'


# ── Formatting ────────────────────────────────────────────────────────────────

def _format_items(items: list[NewsItem], now: datetime) -> str:
    """Форматировать список NewsItem в текст для подстановки в промпт."""
    lines = []
    for it in items:
        hb = it.heat_breakdown or {}
        if it.created_at_source is None:
            age_h: float | str = '?'
        else:
            age_h = round((now - it.created_at_source).total_seconds() / 3600, 1)
        lines.append(
            f"id={it.id} | {it.text}\n"
            f"   источники={','.join(it.sources) or 'hackernews'} likes={it.likes} "
            f"comments={it.num_comments} возраст_ч={age_h} heat={it.heat} "
            f"(vel={hb.get('velocity')},rec={hb.get('recency')},"
            f"disc={hb.get('discussion')},cross={hb.get('cross_source')})"
        )
    return '\n\n'.join(lines)


# ── Fallback ──────────────────────────────────────────────────────────────────

def _fallback_decisions(candidates: list[NewsItem]) -> list[EditorDecision]:
    """При сбое LLM — безопасный фолбэк: топ-N по heat помечаем publish, остальных reject."""
    max_posts = guardrail('max_posts_per_run', 3)
    sorted_items = sorted(candidates, key=lambda it: it.heat, reverse=True)
    decisions: list[EditorDecision] = []
    for i, item in enumerate(sorted_items):
        if i < max_posts:
            decisions.append(EditorDecision(
                item_id=item.id,
                action='publish',
                post_type='news_digest',
                priority=i,
                reason='fallback по heat',
            ))
        else:
            decisions.append(EditorDecision(
                item_id=item.id,
                action='reject',
                post_type='news_digest',
                priority=i,
                reason='fallback: за пределами лимита',
            ))
    return decisions


# ── Guardrails ────────────────────────────────────────────────────────────────

def _apply_guardrails(
    decisions: list[EditorDecision],
    candidates: list[NewsItem],
) -> list[EditorDecision]:
    """
    Применить guardrails из agent_goals.json:
    1. Publish только если heat NewsItem >= min_heat_to_publish.
    2. Ограничить число publish до max_posts_per_run (сортируем по priority, затем heat).
    3. Превышающие лимит publish переводим в hold.
    """
    min_heat: float = guardrail('min_heat_to_publish', 0.25)
    max_posts: int = guardrail('max_posts_per_run', 5)

    # Индекс heat по item_id для быстрого доступа
    heat_index: dict[str, float] = {it.id: it.heat for it in candidates}

    result: list[EditorDecision] = []
    publish_candidates: list[EditorDecision] = []

    for d in decisions:
        if d.action == 'publish':
            item_heat = heat_index.get(d.item_id, 0.0)
            if item_heat < min_heat:
                # Понижаем до hold — тема слишком холодная для публикации
                result.append(d.model_copy(update={
                    'action': 'hold',
                    'reason': d.reason + f' [guardrail: heat {item_heat:.3f} < {min_heat}]',
                }))
            else:
                publish_candidates.append(d)
        else:
            result.append(d)

    # Сортируем кандидатов на publish: сначала по priority (asc), затем по heat (desc)
    publish_candidates.sort(
        key=lambda d: (d.priority, -heat_index.get(d.item_id, 0.0))
    )

    # Берём не больше max_posts_per_run
    for i, d in enumerate(publish_candidates):
        if i < max_posts:
            result.append(d)
        else:
            result.append(d.model_copy(update={
                'action': 'hold',
                'reason': d.reason + f' [guardrail: превышен лимит {max_posts} публикаций за прогон]',
            }))

    return result


# ── Main decide function ──────────────────────────────────────────────────────

async def decide(items: list[NewsItem]) -> list[EditorDecision]:
    """ИИ-редактор решает по каждой новости: publish/hold/reject + тип поста + обоснование."""
    if not items:
        return []

    # Отсекаем совсем слабые до вызова LLM (экономия токенов)
    min_consider: float = guardrail('min_heat_to_consider', 0.0)
    candidates = [it for it in items if it.heat >= min_consider]
    if not candidates:
        logger.info('editor_no_candidates', total=len(items), min_consider=min_consider)
        return []

    now = datetime.now(timezone.utc)
    template, version = get_prompt('editor_decision')
    prompt = template.replace('{{ITEMS}}', _format_items(candidates, now))

    logger.info('editor_decide_start', candidates=len(candidates), prompt_version=version)

    try:
        resp = await client.chat.completions.create(
            model=MODEL,
            messages=[{'role': 'user', 'content': prompt}],
            max_tokens=1024,
            temperature=0.3,
        )
        raw = resp.choices[0].message.content.strip()
        m = re.search(r'\[.*\]', raw, re.DOTALL)
        if not m:
            raise ValueError(f'No JSON array in response: {raw[:200]}')
        data = json.loads(m.group())
        decisions = [EditorDecision(**d) for d in data]
        logger.info('editor_llm_parsed', decisions=len(decisions))
    except Exception as e:
        logger.warning('editor_decide_failed', error=str(e))
        # Фолбэк: безопасное поведение — взять топ по heat как publish
        decisions = _fallback_decisions(candidates)

    # Применить guardrails: порог heat и лимит числа publish
    decisions = _apply_guardrails(decisions, candidates)
    logger.info(
        'editor_decided',
        total=len(candidates),
        publish=sum(1 for d in decisions if d.action == 'publish'),
        hold=sum(1 for d in decisions if d.action == 'hold'),
        reject=sum(1 for d in decisions if d.action == 'reject'),
    )
    return decisions
