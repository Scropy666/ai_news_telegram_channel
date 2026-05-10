# Architecture Design — AI News Telegram Channel

> Разработано с применением скиллов `agent-designer` + `agent-workflow-designer`  
> Модель: Claude Opus (архитектурные решения)

---

## 1. Requirements Analysis

### Функциональные требования
- Автоматический сбор AI/tech новостей каждые 2 часа
- Генерация уникальных постов (без дублирования смысла)
- Публикация в Telegram-канал по расписанию
- Управление системой через Telegram-бота (команды администратора)
- Автоматическое тестирование команд бота

### Нефункциональные требования
- Персистентность: расписание и теги выживают перезапуск
- Retry при сбоях публикации (до 3 попыток, интервал 1ч)
- Уведомления администратора при ошибках
- Один Telegram-бот, один канал
- Бесплатный LLM (Groq)

### Ограничения
- Python 3.11, Windows-совместимость
- Supabase REST API (не asyncpg)
- Без Redis — персистентность через файлы и Supabase

---

## 2. Pattern Selection

По методологии `agent-workflow-designer`:

```
Паттерн: Orchestrator + Sequential Pipeline + Evaluator Loop

Orchestrator  — Coordinator управляет жизненным циклом всей системы
Pipeline      — Scraper → Analyzer → Publisher (строгая последовательность)
Evaluator     — внутри Analyzer: generate → uniqueness_check → accept/reject
Router        — Coordinator маршрутизирует события (успех / ошибка / тест провален)
```

**Почему не Swarm:** агенты имеют чёткую последовательную зависимость, peer-to-peer coordination избыточна.  
**Почему не чистый Pipeline:** нужен центральный Coordinator для команд бота, мониторинга и эскалации ошибок.

---

## 3. System Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                      COORDINATOR AGENT                          │
│                                                                 │
│  APScheduler ──▶ trigger jobs                                   │
│  Telegram Bot ──▶ handle commands                               │
│  Event Bus ──▶ route success/failure/alerts                     │
└────┬────────────────┬──────────────────┬──────────────┬─────────┘
     │                │                  │              │
     ▼                ▼                  ▼              ▼
┌─────────┐    ┌──────────────┐   ┌──────────┐   ┌──────────┐
│ SCRAPER │    │   ANALYZER   │   │PUBLISHER │   │  TESTER  │
│  AGENT  │    │    AGENT     │   │  AGENT   │   │  AGENT   │
│         │    │              │   │          │   │          │
│ HN API  │    │ filter+rank  │   │ format   │   │ TG API   │
│ topics  │──▶│ LLM generate │──▶│ publish  │   │ test cmds│
│ config  │    │ uniqueness✓  │   │ retry    │   │ validate │
└────┬────┘    └──────┬───────┘   └────┬─────┘   └────┬─────┘
     │                │                │              │
     ▼                ▼                ▼              ▼
┌─────────────────────────────────────────────────────────────────┐
│                         SUPABASE                                │
│   raw_tweets (new→processed)  │  posts (pending→published)     │
│   polls                       │  analytics                     │
└─────────────────────────────────────────────────────────────────┘
```

---

## 4. Agent Role Definitions

### 4.1 Coordinator Agent (`src/agents/coordinator.py`)

**Identity:** Центральный оркестратор системы. Единственный агент с доступом к Telegram Bot.

**Responsibilities:**
- Запуск и остановка всех sub-agents
- Управление APScheduler (load/save schedule.json)
- Обработка Telegram-команд (`/status`, `/schedule`, `/topics`, etc.)
- Маршрутизация событий между агентами
- Уведомление администратора при сбоях

**Interfaces:**
```python
# Input: Telegram Update (commands)
# Input: AgentEvent (from sub-agents via callback/queue)
# Output: Telegram messages (admin + channel via Publisher)
# Output: Scheduler jobs (APScheduler)
```

**Decision boundaries:**
- Решает КОГДА запускать Scraper/Analyzer/Publisher
- НЕ генерирует контент, НЕ пишет в канал напрямую
- При критической ошибке sub-agent → notify admin, не падает сам

---

### 4.2 Scraper Agent (`src/agents/scraper_agent.py`)

**Identity:** Сборщик сырых данных из внешних источников.

**Responsibilities:**
- Загрузка тегов из `config/topics.json`
- Запрос HackerNews Algolia API по каждому тегу
- Дедупликация по `id` в рамках одного цикла
- Upsert в Supabase `raw_tweets` (статус `new`)
- Возврат количества новых записей Coordinator-у

**Interfaces:**
```python
async def run() -> ScraperResult:
    # ScraperResult: total_fetched, new_saved, errors[]
```

**Constraints:**
- Только чтение конфига, только запись в raw_tweets
- Не вызывает LLM, не пишет в Telegram
- Timeout на HTTP-запрос: 15 сек

---

### 4.3 Analyzer Agent (`src/agents/analyzer_agent.py`)

**Identity:** Аналитик и генератор контента с встроенным quality gate.

**Responsibilities:**
1. Загрузка `raw_tweets` со статусом `new` из Supabase
2. Фильтрация и ранжирование по engagement + релевантности
3. Загрузка последних опубликованных постов (RAG-контекст)
4. Генерация поста через Groq LLM (versioned prompt)
5. **Uniqueness Check** — сравнение с последними N постами
6. Если уникален: сохранить в `posts` (status=`pending`), вернуть `ready`
7. Если дубль: логировать причину, вернуть `duplicate`
8. Обновить статус обработанных `raw_tweets` → `processed`

**Evaluator Loop:**
```
generate_post()
      │
      ▼
uniqueness_check(post, recent_posts)
      │
   unique? ──No──▶ log_duplicate() ──▶ return AnalyzerResult(status='duplicate')
      │
     Yes
      │
      ▼
save_to_posts(status='pending')
      │
      ▼
return AnalyzerResult(status='ready', post_id=...)
```

**Uniqueness Check Strategy:**
```python
# Используем LLM для семантической проверки (не embedding, т.к. Groq бесплатный)
# Архитектура: UniquenessChecker с pluggable strategy — легко добавить числовой порог позже

class UniquenessResult:
    is_unique: bool
    confidence: float   # 0.0–1.0, резерв для будущего числового порога
    reason: str

class UniquenessChecker:
    # strategy: 'llm' (сейчас) | 'embedding' | 'threshold' (будущее)
    async def check(self, new_post: str, recent_posts: list[str]) -> UniquenessResult:
        ...

# LLM-промпт:
"""
Новый пост: {new_post}

Последние опубликованные посты:
{recent_posts}

Вопрос: Содержит ли новый пост существенно новую информацию,
не освещённую в последних постах? Ответь строго JSON:
{"unique": true/false, "confidence": 0.0-1.0, "reason": "..."}
"""
# Если unique=false → duplicate
# confidence уже возвращается — при добавлении порога достаточно добавить:
#   if result.confidence < THRESHOLD: override to duplicate
```

**Interfaces:**
```python
async def run(post_type: str) -> AnalyzerResult:
    # AnalyzerResult: status ('ready'|'duplicate'|'no_data'), post_id?, reason?
```

---

### 4.4 Publisher Agent (`src/agents/publisher_agent.py`)

**Identity:** Публикатор контента в Telegram-канал.

**Responsibilities:**
- Загрузка поста из Supabase по `post_id`
- Форматирование текста (Markdown, ≤4096 символов)
- Публикация в канал через `bot.send_message()`
- Обновление статуса: `pending` → `published`, запись `published_at`
- Retry-логика: до 3 попыток с интервалом 1 час
- При исчерпании попыток → `failed`, уведомление Coordinator

**Approve Flow (для scheduled постов):**
```
Analyzer сохраняет пост (status='pending')
        │
        ▼
Coordinator отправляет Admin превью поста:
  "📝 Готов пост [тип]:
   {content}
   
   [✅ Опубликовать]  [❌ Отклонить]"
        │
   Admin нажимает кнопку
        │
   ✅ Да ──▶ Publisher.run(post_id) ──▶ канал
   ❌ Нет ──▶ update_status('cancelled')
```

**Retry Contract (после approve):**
```python
for attempt in range(max_retries):
    success = await send_to_channel(post)
    if success:
        update_status('published')
        return PublisherResult(success=True)
    await asyncio.sleep(3600)  # 1 час

update_status('failed')
await notify_coordinator(PostFailedEvent(post_id=...))
```

**Interfaces:**
```python
async def run(post_id: str) -> PublisherResult:
    # PublisherResult: success, published_at?, error?

async def run_poll(topic: str) -> PublisherResult:
    # Генерация + публикация опроса (опросы без approve — публикуются сразу)
```

---

### 4.5 Tester Agent (`src/agents/tester_agent.py`)

**Identity:** QA-агент, автоматически проверяющий корректность команд бота.

**Responsibilities:**
- Отправка тест-команд боту через Telegram Bot API
- Парсинг ответа и сравнение с ожидаемым поведением
- Формирование отчёта: PASS / FAIL + причина
- Передача FAIL-отчётов Coordinator-у

**Test Cases (v1):**
```python
TEST_SUITE = [
    TestCase(
        name="add_schedule",
        command="/add_schedule news_digest 10:00 MON,WED",
        expect_contains=["Добавлено", "news_digest", "10:00"],
    ),
    TestCase(
        name="remove_schedule",
        setup="/add_schedule opinion 23:00 *",
        extract_id=True,  # извлечь ID из ответа
        command="/remove_schedule {id}",
        expect_contains=["удалена"],
    ),
    TestCase(
        name="topics_list",
        command="/topics",
        expect_contains=["Теги для парсинга"],
    ),
    TestCase(
        name="add_topic",
        command="/add_topic Claude AI",
        expect_contains=["Добавлен тег", "claude_ai"],
    ),
    TestCase(
        name="status",
        command="/status",
        expect_contains=["Статус агента", "Dry run"],
    ),
]
```

**Trigger:** автоматически при каждом деплое (запуске бота). Coordinator вызывает `tester_agent.run_suite()` в `post_init` после старта всех агентов. Результат отправляется администратору сводным сообщением.

```
Bot start → post_init → запуск scheduler → run_suite() → отчёт в admin chat
  ✅ 5/5 тестов прошли — система готова
  ❌ 2/5 провалились — [add_schedule: ожидал 'Добавлено', получил ...]
```

**Interfaces:**
```python
async def run_suite(suite: list[TestCase]) -> TestReport:
    # TestReport: passed, failed, details[]

async def run_single(test: TestCase) -> TestResult:
```

---

## 5. Handoff Contracts

Каждый переход данных между агентами описан явным контрактом (по `agent-workflow-designer`):

### Scraper → Analyzer (via Supabase)

```
Table: raw_tweets
Trigger field: status = 'new'
Payload: {
  id: str,          # 'hn_{objectID}'
  text: str,        # заголовок статьи
  author: str,
  likes: int,       # points на HN
  url: str,
  topic: str,       # категория из topics.json
  scraped_at: str,  # ISO datetime
  status: 'new'
}
Timeout: Analyzer читает записи не старше 24ч
On success: Analyzer обновляет status → 'processed'
On skip: записи остаются 'new' до следующего цикла
```

### Analyzer → Publisher (via Supabase)

```
Table: posts
Trigger field: status = 'pending'
Payload: {
  id: str,
  type: PostType,          # news_digest | deep_dive | ...
  content: str,            # готовый текст поста
  prompt_version: str,     # 'news_digest_v1.0.0'
  scheduled_at: str,
  status: 'pending',
  source_tweet_ids: list[str]
}
Timeout: Publisher публикует в течение 5 мин после получения события
On success: status → 'published', published_at = now()
On failure: retry_count++, после 3 попыток → status = 'failed'
```

### Agent → Coordinator (in-memory event)

```python
@dataclass
class AgentEvent:
    agent: str          # 'scraper' | 'analyzer' | 'publisher' | 'tester'
    status: str         # 'success' | 'failure' | 'duplicate' | 'test_failed'
    payload: dict       # контекст события
    timestamp: datetime
```

---

## 6. Communication Design

**Основной паттерн: Shared State via Supabase**

Агенты не вызывают друг друга напрямую. Взаимодействие через БД + in-memory callbacks к Coordinator.

```
Почему Supabase, а не очередь (Redis/RabbitMQ):
✓ Уже используем Supabase
✓ Бесплатно, без дополнительной инфраструктуры
✓ Персистентность из коробки
✓ Можно смотреть состояние через Supabase dashboard
✗ Нет push-уведомлений (polling вместо event-driven)
→ Решение: Coordinator опрашивает DB после завершения каждого агента
```

---

## 7. Safety & Guardrails

| Риск | Guardrail |
|------|-----------|
| Дублирующийся пост | Uniqueness check в Analyzer (LLM-based) |
| Спам в канал | Расписание через schedule.json, dry_run режим |
| Сбой LLM | Retry + notify admin, пост не публикуется |
| Сбой Telegram API | Retry 3x с интервалом 1ч, затем failed |
| Дубли в raw_tweets | Дедупликация по id до upsert |
| Несанкционированный доступ | is_admin() проверка на каждую команду |

**Human-in-the-Loop:** команда `/create_post` показывает превью с кнопками подтверждения перед публикацией.

---

## 8. Evaluation Metrics

| Метрика | Цель | Источник |
|---------|------|---------|
| Scrape success rate | >95% циклов без ошибок | structlog |
| Posts per day | 2 поста/день | posts table |
| Duplicate rate | <20% | analyzer logs |
| Publish success rate | >97% | posts.status |
| Test suite pass rate | 100% | tester reports |

---

## 9. Project Structure

```
ai_news_telegram_channel/
├── CLAUDE.md
├── ARCHITECTURE.md          # этот файл
├── .env
├── .env.example
├── requirements.txt
├── config/
│   ├── schedule.json
│   ├── topics.json
│   └── prompts/
│       ├── registry.yaml
│       ├── news_digest_v1.0.0.md
│       ├── deep_dive_v1.0.0.md
│       ├── tool_spotlight_v1.0.0.md
│       ├── opinion_v1.0.0.md
│       └── poll_generator_v1.0.0.md
└── src/
    ├── main.py                  # точка входа → запускает Coordinator
    ├── config.py                # Pydantic Settings
    ├── agents/
    │   ├── __init__.py
    │   ├── coordinator.py       # оркестратор + Telegram bot handlers
    │   ├── scraper_agent.py     # HN scraping
    │   ├── analyzer_agent.py    # filter + generate + uniqueness check
    │   ├── publisher_agent.py   # Telegram channel publishing
    │   └── tester_agent.py      # automated bot testing
    ├── database/
    │   ├── client.py
    │   └── models.py
    ├── generator/
    │   ├── content_generator.py
    │   ├── poll_generator.py
    │   └── prompt_registry.py
    └── scheduler/
        └── scheduler.py
```

---

## 10. Implementation Order (Sprint Plan)

### Sprint 1 — Foundation
1. `src/config.py` — Pydantic Settings
2. `src/database/models.py` — все Pydantic-модели
3. `src/database/client.py` — Supabase client
4. `src/generator/prompt_registry.py` + prompts
5. `config/` файлы (schedule.json, topics.json)

### Sprint 2 — Core Agents
6. `src/agents/scraper_agent.py`
7. `src/agents/analyzer_agent.py` (включая uniqueness check)
8. `src/agents/publisher_agent.py`

### Sprint 3 — Orchestration
9. `src/scheduler/scheduler.py`
10. `src/agents/coordinator.py` (lifecycle + команды бота)
11. `src/main.py`

### Sprint 4 — Testing & Polish
12. `src/agents/tester_agent.py`
13. End-to-end тест с dry_run=true
14. Финальная конфигурация

---

## Decisions Log

| # | Вопрос | Решение |
|---|--------|---------|
| 1 | Uniqueness threshold | LLM-суждение (YES/NO + confidence float). Числовой порог добавляется позже через `UniquenessChecker.threshold` без переписывания логики |
| 2 | Approve для scheduled постов | Ручной approve через инлайн-кнопки в Telegram. Опросы публикуются без approve |
| 3 | Запуск Tester Agent | Автоматически при каждом старте бота (в `post_init`) |
