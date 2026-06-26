"""Расчёт 'горячести' новости: актуальность + важность + виральность.
Все функции чистые (без I/O), легко тестируются."""
from __future__ import annotations
import math
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

# Параметры берём из agent_goals.json через caller; здесь — дефолты.
RECENCY_TAU_HOURS = 24.0   # за сколько часов «свежесть» падает в e раз
VELOCITY_NORM = 30.0       # очков/час, дающих ~максимум по компоненте velocity
COMMENTS_NORM = 300.0      # комментов, дающих ~максимум по обсуждаемости

WEIGHTS = {               # сумма = 1.0
    'velocity': 0.40,
    'recency': 0.25,
    'discussion': 0.20,
    'cross_source': 0.15,
}


@dataclass
class HeatBreakdown:
    velocity: float
    recency: float
    discussion: float
    cross_source: float
    total: float

    def as_dict(self) -> dict:
        return asdict(self)


def _age_hours(created_at_source: datetime | None, now: datetime) -> float:
    if created_at_source is None:
        return RECENCY_TAU_HOURS  # неизвестно — считаем «средне свежим»
    delta = (now - created_at_source).total_seconds() / 3600.0
    return max(delta, 0.1)


def compute_heat(
    *,
    likes: int,
    num_comments: int,
    merged_count: int,
    num_sources: int,
    created_at_source: datetime | None,
    now: datetime | None = None,
) -> HeatBreakdown:
    now = now or datetime.now(timezone.utc)
    age_h = _age_hours(created_at_source, now)

    velocity = min((likes / age_h) / VELOCITY_NORM, 1.0)
    recency = math.exp(-age_h / RECENCY_TAU_HOURS)
    discussion = min(num_comments / COMMENTS_NORM, 1.0)
    # виральность: всплыло на нескольких источниках/в нескольких записях
    cross = min((max(num_sources, 1) - 1) * 0.5 + (max(merged_count, 1) - 1) * 0.25, 1.0)

    total = (
        WEIGHTS['velocity'] * velocity
        + WEIGHTS['recency'] * recency
        + WEIGHTS['discussion'] * discussion
        + WEIGHTS['cross_source'] * cross
    )
    return HeatBreakdown(
        velocity=round(velocity, 3),
        recency=round(recency, 3),
        discussion=round(discussion, 3),
        cross_source=round(cross, 3),
        total=round(min(total, 1.0), 3),
    )
