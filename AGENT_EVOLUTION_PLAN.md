# План эволюции: автопубликатор → самостоятельный ИИ-агент

> **Статус:** черновик архитектора. Цель — превратить детерминированный pipeline в ИИ-агента,
> который **сам решает**, что публиковать, исходя из **актуальности** и **важности/виральности** темы.
> **Режим автономии на старте: `suggest`** — агент принимает решение и обосновывает его, человек ветирует одной кнопкой.

---

## 0. Как пользоваться этим документом (для разработчика — модель Sonnet)

Этот документ написан Архитектором/Тим-Лидом. Ты — разработчик. Правила работы:

1. **Делай фазы строго по порядку.** Фаза N+1 опирается на артефакты Фазы N. Не начинай следующую, пока текущая не прошла «Критерии приёмки».
2. **Внутри фазы делай задачи по порядку** (T1 → T2 → …). Каждая задача атомарна и проверяема.
3. **Iron Law проекта:** `SCOPE → TRACE → DIAGNOSE → FIX → VERIFY`. Сначала пойми, потом меняй.
4. **Не выходи за рамки задачи.** Если задача говорит «добавь поле X», не рефактори соседние функции.
5. После каждой задачи запускай её **Verify**-команду. Если не проходит — чини, прежде чем идти дальше.
6. Любую неясность по продуктовому решению — **спрашивай**, не выдумывай.

### Жёсткие правила проекта (нарушать нельзя — см. `CLAUDE.md`)

- ❌ **Промпты в коде запрещены.** Любой промпт — только `.md`-файл в `config/prompts/` + запись в `config/registry.yaml`, читается через `get_prompt(name)`.
- ❌ **Прямые вызовы между sub-агентами запрещены.** Оркестрацию делает Coordinator (`src/agents/coordinator.py`). Editor вызывается из координатора, а не из `analyzer` напрямую.
- ❌ **Хардкод секретов запрещён.** Только `src/config.py` (`settings`).
- ✅ **Async/await везде.** Никаких блокирующих вызовов в event loop.
- ✅ **Логирование только через `structlog`** (`logger = structlog.get_logger()`), формат событий как в проекте: `logger.info('event_name', key=value)`.
- ✅ **Модели данных — Pydantic** (`src/database/models.py`).
- ✅ **Опасные операции** (DROP/DELETE без WHERE/rm -rf/git reset --hard) — только после явного подтверждения пользователя.

---

## 1. Архитектура: было → станет

### Было (детерминированный pipeline)
```
run_pipeline (coordinator)
  └─ scraper.run()                  # собрать
  └─ analyzer.run_multi()           # отфильтровать + сгенерировать (тип = ротация по кругу)
  └─ send_post_for_review()         # 7 кнопок → ЧЕЛОВЕК выбирает что и когда
```
Решения принимает: **человек** (кнопки) + **хардкод** (`POST_TYPES_ROTATION`, `HIGH_VALUE_KEYWORDS`).

### Станет (агент с решающим слоём)
```
run_pipeline (coordinator)
  └─ scraper.run()                          # OBSERVE
  └─ analyzer.get_ranked_items()            # NewsItem[] с heat-метрикой (Фаза 1)
  └─ editor_agent.decide(items)             # DECIDE: publish/hold/reject + тип + обоснование (Фаза 2)
  └─ analyzer.run_multi(decisions=...)      # генерация ТОЛЬКО одобренных
  └─ send_post_for_review(reason=...)       # SUGGEST: показать решение+обоснование, человек ветирует (Фаза 3)
```
Решения принимает: **ИИ (Editor Agent)** на основе heat-сигналов. Человек — только вето.

Фаза 4 заменяет жёсткий порядок на **tool-calling цикл**, где агент сам выбирает последовательность действий.

### Карта изменений по файлам

| Файл | Фаза | Что происходит |
|------|------|----------------|
| `src/agent_core/__init__.py` | 1 | НОВЫЙ (пустой пакет) |
| `src/agent_core/heat.py` | 1 | НОВЫЙ — расчёт горячести/актуальности |
| `src/database/models.py` | 1,2 | +поля в `RawTweet`/`NewsItem`; +модель `EditorDecision` |
| `src/scrapers/hackernews.py` | 1 | сохранять `created_at` истории |
| `src/agents/analyzer_agent.py` | 1,3 | heat в ранжировании; `get_ranked_items()`; `run_multi(decisions=)` |
| `src/agents/editor_agent.py` | 2 | НОВЫЙ — решающий агент |
| `config/prompts/editor_decision_v1.0.0.md` | 2 | НОВЫЙ промпт |
| `config/registry.yaml` | 2 | +запись `editor_decision` |
| `config/agent_goals.json` | 1,2 | НОВЫЙ — пороги и guardrails |
| `src/config.py` | 1 | +поля настроек агента |
| `src/agents/coordinator.py` | 3 | вызов editor в pipeline; reason в review |
| `src/main.py` | 3 | новая раскладка кнопок (approve/veto) |
| `src/agent_core/tools.py` | 4 | НОВЫЙ — инструменты для tool-calling |
| `src/agent_core/loop.py` | 4 | НОВЫЙ — OODA-цикл агента |

---

## ФАЗА 1 — Heat Score (детерминированный костяк)

**Зачем:** дать агенту числовую опору «насколько тема горячая и свежая». Без неё Editor (Фаза 2) решает вслепую.
**Принцип:** «горячо» = много очков **быстро** (velocity) + активно обсуждают (comments) + всплыло на нескольких площадках (cross-source) + свежо (recency).

### T1.1 — Создать пакет `agent_core`
- Создай пустой файл `src/agent_core/__init__.py`.
- **Verify:** `venv\Scripts\python.exe -c "import src.agent_core"` без ошибок.

### T1.2 — Сохранять время публикации истории в источнике
Сейчас `RawTweet` не хранит, когда новость вышла. Без этого нельзя считать возраст и velocity.

- В `src/database/models.py`, класс `RawTweet`, добавь поле:
  ```python
  created_at_source: datetime | None = None  # когда новость вышла в источнике (для расчёта возраста/velocity)
  ```
- В `src/scrapers/hackernews.py`, в цикле построения `RawTweet`, добавь извлечение `created_at_i` (Unix-секунды от Algolia):
  ```python
  created_i = h.get('created_at_i')
  created_dt = datetime.fromtimestamp(created_i, tz=timezone.utc) if created_i else None
  ```
  и передай `created_at_source=created_dt` в конструктор `RawTweet`.
- ⚠️ Поле опциональное (`reddit.py`, `devto.py` могут его не заполнять — оставляй `None`). Не трогай их в этой задаче.
- **Verify:** `venv\Scripts\python.exe -c "from src.database.models import RawTweet; print('ok')"`.

### T1.3 — Прокинуть сигналы в `NewsItem`
- В `src/database/models.py`, класс `NewsItem`, добавь поля:
  ```python
  num_comments: int = 0                       # активность обсуждения
  created_at_source: datetime | None = None   # возраст новости
  heat: float = 0.0                           # итоговый heat-score (заполняет Фаза 1)
  heat_breakdown: dict = Field(default_factory=dict)  # компоненты heat для прозрачности (показываем в review)
  ```
- **Verify:** импорт модели без ошибок.

### T1.4 — Модуль `heat.py`
Создай `src/agent_core/heat.py`. Чистые функции, без обращений к БД/сети.

```python
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
```

- **Verify:** напиши быстрый smoke-тест в терминале:
  ```
  venv\Scripts\python.exe -c "from datetime import datetime,timezone,timedelta; from src.agent_core.heat import compute_heat; print(compute_heat(likes=500,num_comments=200,merged_count=2,num_sources=2,created_at_source=datetime.now(timezone.utc)-timedelta(hours=3)).as_dict())"
  ```
  Ожидание: словарь с `total` в диапазоне 0..1, все компоненты заполнены.

### T1.5 — Интегрировать heat в ранжирование
В `src/agents/analyzer_agent.py`, функция `_make_item` (внутри `_filter_and_rank`):
- После создания `NewsItem` (сейчас она возвращается напрямую) — вычисли heat и положи в поля.
- Нужны `num_comments` (сумма `t.retweets` по группе — в проекте `retweets` хранит число комментариев) и `created_at_source` представителя `best`.

Конкретно, перед `return NewsItem(...)` собери:
```python
from src.agent_core.heat import compute_heat
total_comments = sum(t.retweets for t in group)
hb = compute_heat(
    likes=total_likes,
    num_comments=total_comments,
    merged_count=len(group),
    num_sources=len(sources),
    created_at_source=best.created_at_source,
)
```
и добавь в конструктор `NewsItem(...)`: `num_comments=total_comments, created_at_source=best.created_at_source, heat=hb.total, heat_breakdown=hb.as_dict()`.

- В конце `_filter_and_rank` **измени сортировку** с `relevance_score` на `heat`:
  ```python
  items.sort(key=lambda x: x.heat, reverse=True)
  ```
  (поле `relevance_score` оставь как есть — оно ещё используется в фильтре порога; не удаляй).
- **Verify:** `venv\Scripts\python.exe -c "import src.agents.analyzer_agent"` без ошибок. Юнит-логика проверится в Фазе 2 на реальном прогоне.

### T1.6 — Файл целей и порогов `agent_goals.json`
Создай `config/agent_goals.json`:
```json
{
  "objective": "Публиковать самые актуальные и виральные новости об ИИ. Критерии: свежесть, важность, обсуждаемость.",
  "autonomy_mode": "suggest",
  "guardrails": {
    "max_posts_per_run": 5,
    "max_posts_per_day": 8,
    "min_heat_to_publish": 0.25,
    "min_heat_to_consider": 0.12
  }
}
```
Добавь загрузчик в `src/config.py` НЕ через pydantic-settings (это JSON, не env). Сделай отдельную функцию в новом модуле или в `agent_core`. Создай `src/agent_core/goals.py`:
```python
import json
from pathlib import Path
from functools import lru_cache

GOALS_PATH = Path(__file__).parent.parent.parent / 'config' / 'agent_goals.json'

@lru_cache(maxsize=1)
def load_goals() -> dict:
    return json.loads(GOALS_PATH.read_text(encoding='utf-8'))

def guardrail(key: str, default=None):
    return load_goals().get('guardrails', {}).get(key, default)
```
- **Verify:** `venv\Scripts\python.exe -c "from src.agent_core.goals import guardrail; print(guardrail('min_heat_to_publish'))"` → `0.25`.

### ✅ Критерии приёмки Фазы 1
- [ ] `import src.agent_core.heat`, `import src.agent_core.goals` работают.
- [ ] `RawTweet` и `NewsItem` имеют новые поля, импорт моделей не падает.
- [ ] HackerNews-скрапер заполняет `created_at_source`.
- [ ] `_filter_and_rank` сортирует по `heat`, каждый `NewsItem` имеет непустой `heat_breakdown`.
- [ ] `agent_goals.json` читается через `goals.guardrail()`.
- [ ] Существующий запуск `/scrape` и `/post` не сломан (прогони бота локально, если есть доступ к .env; иначе — статический импорт всех затронутых модулей без ошибок).

---

## ФАЗА 2 — Editor Agent (ИИ принимает решение)

**Зачем:** заменить хардкод-ротацию типов и человеческий выбор «что публиковать» на решение ЛЛМ по критериям актуальность + виральность.
**Важно:** Editor решает на уровне `NewsItem` (ДО генерации), чтобы не тратить токены на генерацию отклонённых тем.

### T2.1 — Модель решения
В `src/database/models.py` добавь:
```python
EditorAction = Literal['publish', 'hold', 'reject']

class EditorDecision(BaseModel):
    item_id: str                 # = NewsItem.id
    action: EditorAction
    post_type: PostType          # какой формат поста выбрал агент
    priority: int = 0            # порядок (меньше = важнее)
    reason: str = ''             # обоснование для человека (показываем в review)
```
- **Verify:** импорт моделей без ошибок.

### T2.2 — Промпт решения
Создай `config/prompts/editor_decision_v1.0.0.md`. Требования к содержимому:
- Роль: «Ты — главный редактор Telegram-канала об ИИ. Решаешь, какие новости публиковать.»
- Критерии: **актуальность** (свежесть), **важность** (значимость для индустрии ИИ), **виральность** (обсуждаемость, кросс-источник). Метрики heat даны тебе как подсказка, но финальное суждение — твоё.
- Вход: плейсхолдер `{{ITEMS}}` — пронумерованный список новостей с полями (id, заголовок, источники, likes, комментарии, возраст, heat-компоненты).
- Тип поста выбирай из: `news_digest`, `deep_dive`, `tool_spotlight`, `opinion`. Дай правило выбора (1 крупная тема → deep_dive; инструмент → tool_spotlight; несколько мелких → news_digest; тренд/спор → opinion).
- **Формат вывода — строго JSON-массив**, без markdown-обёртки:
  ```json
  [{"item_id":"hn_123","action":"publish","post_type":"deep_dive","priority":0,"reason":"свежий релиз, обсуждают на HN+Reddit"}]
  ```
- Язык `reason` — русский.
- Зарегистрируй промпт в `config/registry.yaml` в секции `prompts`:
  ```yaml
  editor_decision:
    default_version: "1.0.0"
    description: "Решение редактора: какие новости публиковать, тип и обоснование"
    status: active
  ```
- **Verify:** `venv\Scripts\python.exe -c "from src.generator.prompt_registry import get_prompt; print(get_prompt('editor_decision')[1])"` → `1.0.0`.

### T2.3 — Сам агент `editor_agent.py`
Создай `src/agents/editor_agent.py`. Образец для стиля работы с Groq и парсинга JSON — `analyzer_agent._check_uniqueness` (та же модель, тот же приём `re.search(r'\{.*\}'...)`, тот же `try/except` с фолбэком).

```python
import json
import re
import structlog
from datetime import datetime, timezone

from groq import AsyncGroq

from src.config import settings
from src.database.models import NewsItem, EditorDecision
from src.generator.prompt_registry import get_prompt
from src.agent_core.goals import guardrail

logger = structlog.get_logger()
client = AsyncGroq(api_key=settings.groq_api_key)
MODEL = 'llama-3.3-70b-versatile'


def _format_items(items: list[NewsItem], now: datetime) -> str:
    lines = []
    for it in items:
        hb = it.heat_breakdown or {}
        age_h = '?' if not it.created_at_source else round((now - it.created_at_source).total_seconds()/3600, 1)
        lines.append(
            f"id={it.id} | {it.text}\n"
            f"   источники={','.join(it.sources) or 'hackernews'} likes={it.likes} "
            f"comments={it.num_comments} возраст_ч={age_h} heat={it.heat} "
            f"(vel={hb.get('velocity')},rec={hb.get('recency')},disc={hb.get('discussion')},cross={hb.get('cross_source')})"
        )
    return '\n\n'.join(lines)


async def decide(items: list[NewsItem]) -> list[EditorDecision]:
    """ИИ-редактор решает по каждой новости: publish/hold/reject + тип поста + обоснование."""
    if not items:
        return []

    # отсечь совсем слабые до LLM (экономия токенов)
    min_consider = guardrail('min_heat_to_consider', 0.0)
    candidates = [it for it in items if it.heat >= min_consider]
    if not candidates:
        logger.info('editor_no_candidates', total=len(items), min_consider=min_consider)
        return []

    now = datetime.now(timezone.utc)
    template, version = get_prompt('editor_decision')
    prompt = template.replace('{{ITEMS}}', _format_items(candidates, now))

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
    except Exception as e:
        logger.warning('editor_decide_failed', error=str(e))
        # фолбэк: безопасное поведение — взять топ по heat как publish
        decisions = _fallback_decisions(candidates)

    # применить guardrail: не больше max_posts_per_run «publish», по heat-порогу
    decisions = _apply_guardrails(decisions, candidates)
    logger.info('editor_decided', total=len(candidates),
                publish=sum(1 for d in decisions if d.action == 'publish'))
    return decisions
```

Также реализуй вспомогательные `_fallback_decisions(candidates)` и `_apply_guardrails(decisions, candidates)`:
- `_fallback_decisions`: при сбое LLM — топ-N по `heat` (N = `max_posts_per_run`) пометить `publish` с `post_type='news_digest'`, `reason='fallback по heat'`, остальные `reject`.
- `_apply_guardrails`: оставить `publish` только тем, у кого `NewsItem.heat >= min_heat_to_publish`; обрезать число `publish` до `max_posts_per_run` (по `priority`, затем по heat); прочих перевести в `hold`.

- **Verify:** статический импорт `venv\Scripts\python.exe -c "import src.agents.editor_agent"`. Логику фолбэка проверь юнит-вызовом `decide([])` → `[]`.

### ✅ Критерии приёмки Фазы 2
- [ ] `EditorDecision` импортируется; промпт `editor_decision` резолвится через registry.
- [ ] `editor_agent.decide(items)` возвращает `list[EditorDecision]`; при пустом входе — `[]`; при сбое LLM — фолбэк по heat (не падает).
- [ ] Guardrails соблюдаются: число `publish` ≤ `max_posts_per_run`, все `publish` имеют `heat ≥ min_heat_to_publish`.
- [ ] Промпт НЕ хардкожен в `.py` — только в `.md` + registry.

---

## ФАЗА 3 — Suggest-режим (агент решает, человек ветирует)

**Зачем:** включить решающий слой в реальный pipeline и заменить 7 кнопок выбора на «согласиться / вето» с обоснованием агента. Оркестрацию ведёт Coordinator (соблюдаем анти-паттерн «никаких прямых вызовов между агентами»).

### T3.1 — Дать analyzer'у отдать ранжированные items без генерации
В `src/agents/analyzer_agent.py` добавь функцию:
```python
def get_ranked_items(
    source_tweets: list[RawTweet] | None = None,
    limit: int = MAX_INPUT_ITEMS,
) -> list[NewsItem]:
    """Вернуть отфильтрованные и отранжированные по heat NewsItem БЕЗ генерации постов."""
    tweets = source_tweets if source_tweets is not None else _load_new_tweets(hours=24)
    if not tweets:
        return []
    return _filter_and_rank(tweets)[:limit]
```
- **Verify:** импорт без ошибок.

### T3.2 — `run_multi` принимает решения редактора
Расширь сигнатуру `run_multi` параметром `decisions: list[EditorDecision] | None = None`. Поведение:
- Если `decisions` передан — генерируй посты **только** для items с `action == 'publish'`, используя `post_type` из решения (а не `POST_TYPES_ROTATION`). Сопоставляй item ↔ decision по `NewsItem.id == EditorDecision.item_id`.
- Верни список постов, причём для каждого поста где-то сохрани соответствующий `reason` (см. T3.3 — как донести reason до review). Простой путь: возвращать не `list[Post]`, а оставить `run_multi` как есть, но добавить параллельную функцию-обёртку. **Решение архитектора:** пусть `run_multi` возвращает `list[Post]`, а соответствие `post_id → reason` положи во временный словарь, который вернёшь вместе с постами. Измени возврат на `tuple[list[Post], dict[str, str]]` ТОЛЬКО для нового пути с decisions; чтобы не ломать старый код, сделай **новую** функцию:
  ```python
  async def run_with_decisions(
      items: list[NewsItem],
      decisions: list[EditorDecision],
      tags: list[str] | None = None,
  ) -> list[tuple[Post, str]]:
      """Сгенерировать посты по одобренным решениям. Возвращает (Post, reason)."""
  ```
  Внутри переиспользуй `generate_post(...)` (как в `run_multi`) и `_check_uniqueness` (уникальность по-прежнему обязательна — анти-паттерн «публикация без проверки уникальности»). Возвращай пары `(post, decision.reason)`.
- ⚠️ Не удаляй старый `run_multi` — он используется в ручном `/post`. Новый путь — отдельная функция.
- **Verify:** импорт без ошибок.

### T3.3 — Coordinator: встроить Editor в pipeline
В `src/agents/coordinator.py`, функция `run_pipeline`. Сейчас она зовёт `run_multi`. Перестрой авто-ветку (когда `tags` НЕ заданы) так:
```python
from src.agents.analyzer_agent import get_ranked_items, run_with_decisions
from src.agents.editor_agent import decide

items = get_ranked_items()
if not items:
    await notify_admin('📭 Нет новых новостей для анализа.')
    return
decisions = await decide(items)                      # ИИ решает
pairs = await run_with_decisions(items, decisions)   # генерим одобренные
if not pairs:
    await notify_admin('📭 Редактор не одобрил ни одной темы к публикации.')
    return
await notify_admin(f'🧠 Редактор одобрил {len(pairs)} тем. Проверь решения 👇')
for i, (post, reason) in enumerate(pairs, 1):
    await send_post_for_review(post, index=i, total=len(pairs), drafts=drafts, reason=reason)
```
- ⚠️ Ветку с `tags` (ручной `/post <тег>`) **не трогай** — там человек уже явно выбрал тему, Editor не нужен.
- **Verify:** импорт `import src.agents.coordinator` без ошибок.

### T3.4 — Показать обоснование агента в review + кнопки approve/veto
В `src/agents/coordinator.py`:
- Добавь параметр `reason: str = ''` в `send_post_for_review`. Вставь блок обоснования в `header`:
  ```python
  reason_block = f'🧠 Редактор: {reason}\n\n' if reason else ''
  header = f'📝 Пост {index}/{total} — {label}\n{reason_block}'
  ```
- Замени `build_post_keyboard` на suggest-раскладку. Анти-паттерн ломать нельзя, поэтому **переиспользуй существующие callback-коды** (`pn` = опубликовать сейчас, `p1h` = через час, `rj` = вето/отклонить). Новая раскладка:
  ```python
  def build_post_keyboard(post_id: str) -> InlineKeyboardMarkup:
      return InlineKeyboardMarkup([
          [InlineKeyboardButton('✅ Опубликовать', callback_data=f'pn_{post_id}'),
           InlineKeyboardButton('🚫 Вето',        callback_data=f'rj_{post_id}')],
          [InlineKeyboardButton('⏱ Через час',    callback_data=f'p1h_{post_id}'),
           InlineKeyboardButton('💾 В черновики', callback_data=f'sv_{post_id}')],
      ])
  ```
- Обработчик `callback_post_action` в `src/main.py` уже принимает коды `pn/p1h/sv/rj` (паттерн `^(pn|p30|p1h|p4h|p8h|sv|rj)_`). **Менять main.py не нужно** — коды переиспользованы.
- **Verify:** импорт coordinator без ошибок; вручную прогони бота и проверь, что при `/post` без аргументов приходит сообщение с блоком «🧠 Редактор: …» и двумя рядами кнопок.

### ✅ Критерии приёмки Фазы 3
- [ ] Авто-pipeline (`/post` без тегов и по расписанию) идёт через Editor: scrape → rank → decide → generate(approved) → review.
- [ ] В сообщении review виден `🧠 Редактор: <обоснование>` и тип поста выбран агентом (не ротацией).
- [ ] Кнопки: ✅ Опубликовать / 🚫 Вето / ⏱ Через час / 💾 В черновики. Существующий обработчик работает без правок.
- [ ] Уникальность по-прежнему проверяется перед показом.
- [ ] Ручной `/post <тег>` работает по-старому (Editor не вмешивается).
- [ ] Guardrail `max_posts_per_day` соблюдается (если за сутки уже опубликовано N — pipeline не шлёт сверх лимита; реализуй счётчик по таблице `posts` за 24ч в coordinator перед циклом review).

---

## ФАЗА 4 — Tool-calling цикл (то, что делает систему агентом, а не scoring-функцией)

**Зачем:** убрать жёсткий порядок `scrape→rank→decide→generate`. Дать агенту инструменты и право самому выбирать последовательность: например, увидел слабый heat по теме → дозапросил источник → только потом сгенерировал.
**⚠️ Это продвинутая фаза. Делай её только после стабильных Фаз 1–3.** Groq `llama-3.3-70b-versatile` поддерживает function-calling (OpenAI-совместимый формат `tools`).

### T4.1 — Описать инструменты `tools.py`
Создай `src/agent_core/tools.py`:
- JSON-схемы инструментов (формат OpenAI `tools`): `get_candidates`, `scrape_topic(query)`, `generate_draft(item_id, post_type)`, `submit_for_review(item_id, reason)`, `finish(summary)`.
- Реестр `TOOL_SCHEMAS: list[dict]` и диспетчер `async def dispatch(name: str, args: dict, ctx: AgentContext) -> dict`, который вызывает соответствующие функции существующих агентов **через coordinator-слой** (не напрямую агент-агент).
- `AgentContext` (dataclass) держит состояние прогона: загруженные items, сгенерированные драфты, `drafts` бота, счётчики guardrail.

### T4.2 — OODA-цикл `loop.py`
Создай `src/agent_core/loop.py`:
- `async def run_agent_cycle(ctx: AgentContext, max_steps: int = 8) -> None`.
- Системный промпт (через registry — новый `config/prompts/agent_system_v1.0.0.md`) с целью из `agent_goals.json` и описанием инструментов.
- Цикл: отправляем сообщения + `tools` в Groq → если модель вернула `tool_calls`, исполняем `dispatch`, добавляем результаты в историю, повторяем; если вызвала `finish` или достигнут `max_steps` — выходим.
- Жёсткие предохранители: `max_steps`, лимиты из guardrails, обязательная проверка уникальности внутри `generate_draft`.

### T4.3 — Переключатель режима
- В `agent_goals.json` добавь `"orchestration": "pipeline" | "agent_loop"`.
- В `coordinator.run_pipeline`: если `orchestration == 'agent_loop'` — вызывай `run_agent_cycle`, иначе — линейный путь Фазы 3. Это позволяет включать/выключать агентный режим без удаления стабильного pipeline.

### ✅ Критерии приёмки Фазы 4
- [ ] Агент проходит полный цикл (scrape→decide→generate→submit) **через tool-calls**, не по жёсткому порядку.
- [ ] `max_steps` и guardrails не дают зациклиться / превысить лимиты постов.
- [ ] Режим переключается флагом `orchestration`; pipeline-режим Фазы 3 остаётся рабочим фолбэком.
- [ ] Все действия логируются (`logger.info('agent_step', tool=..., args=...)`).

---

## ФАЗА 5 — Обратная связь (ОТЛОЖЕНО)

Для текущей цели (актуальность + виральность) обучение на реакциях аудитории **не требуется** — «горячесть» оценивается в момент решения по heat-сигналам.
Когда/если цель сместится к вовлечённости:
- Оживить `models.py:Analytics` (сбор реакций через `MessageReactionUpdated`, Bot API 7.0+; просмотры — только через MTProto/Telethon).
- Добавить `reflection_agent.py`: раз в сутки коррелировать heat-предсказание с фактическими реакциями и калибровать веса `heat.WEIGHTS` / пороги в `agent_goals.json`.
Оставляем как явный backlog, не реализуем сейчас.

---

## Кросс-фазовое: тестирование и откат

- **Юнит-тесты (минимум):** `heat.compute_heat` (граничные: возраст=0, нет comments, 1 источник); `editor._apply_guardrails` (обрезка по лимиту и порогу); `editor.decide([])` → `[]`.
- **Интеграционный smoke:** ручной `/post` без тегов на тестовом канале → проверить, что приходит решение редактора с обоснованием.
- **Откат:** каждая фаза — отдельный коммит (и желательно отдельная ветка). Фазы 1–2 аддитивны (новые поля/модули) и не ломают существующий поток до Фазы 3. Фаза 3 меняет UX review — если что-то не так, откатывается ревертом одного коммита.
- **Безопасность:** перед любыми операциями с БД (новые поля в моделях НЕ требуют DROP/ALTER, т.к. Supabase REST допускает доп. поля) — не выполняй разрушительных SQL. Если понадобится колонка в БД — сначала согласуй с пользователем.

## Порядок коммитов (рекомендация)
1. `feat(agent): heat scoring foundation (Phase 1)`
2. `feat(agent): editor decision agent (Phase 2)`
3. `feat(agent): suggest-mode pipeline with editor (Phase 3)`
4. `feat(agent): tool-calling OODA loop (Phase 4)`

Каждый коммит — только после прохождения «Критериев приёмки» своей фазы.
