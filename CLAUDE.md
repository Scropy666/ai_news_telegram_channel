# AI News Telegram Channel — Multi-Agent System

> ⚠️ **SAFETY FIRST**: Перед выполнением любых операций ознакомься с `.claude/settings.json` → раздел `safety`.  
> 🔴 **Запрещено без подтверждения**: `DROP TABLE`, `DROP DATABASE`, `rm -rf`, `git reset --hard`, `git push --force`, и другие разрушительные команды.  
> Подробный список: `.claude/skills/safety-guardrails.md`

## Концепция

Эволюция проекта `telegram-agent`: вместо одного монолитного агента — система специализированных sub-agents, каждый из которых отвечает за свою зону ответственности. Это делает систему масштабируемой, тестируемой и легко расширяемой.

## Модели Claude

| Задача | Модель |
|--------|--------|
| Проектирование архитектуры, декомпозиция задач, управление roadmap | **Claude Opus** (`claude-opus-4-7`) |
| Написание кода, реализация задач, рефакторинг | **Claude Sonnet** (`claude-sonnet-4-6`) |

## Архитектура: Sub-Agents

```
┌──────────────────────────────────────────────┐
│              Coordinator Agent               │
│   Управляет всеми агентами, маршрутизирует   │
│   задачи, принимает решения об эскалации     │
└────────┬────────┬────────┬──────────┬────────┘
         │        │        │          │
    ┌────▼──┐ ┌───▼───┐ ┌──▼───┐ ┌───▼────┐
    │Scraper│ │Analyz-│ │Publi-│ │Tester  │
    │Agent  │ │er     │ │sher  │ │Agent   │
    │       │ │Agent  │ │Agent │ │        │
    └───────┘ └───────┘ └──────┘ └────────┘
```

### 1. Scraper Agent — Агент скрапинга

**Ответственность:** Сбор сырых данных из внешних источников и сохранение в БД.

**Что делает:**
- Скрапит новости с HackerNews (Algolia API) по настраиваемому набору тегов
- Фильтрует по минимальному engagement (points/likes)
- Дедуплицирует по `id` перед сохранением
- Сохраняет сырые данные в таблицу `raw_tweets` (Supabase)
- Запускается по расписанию (каждые 2 часа) и по команде `/scrape`

**Источники данных:**
- HackerNews Algolia API (основной)
- Возможность добавить дополнительные источники через конфиг

---

### 2. Analyzer Agent — Агент анализа и генерации

**Ответственность:** Анализ скрапленных данных, генерация поста, проверка уникальности.

**Что делает:**
1. Забирает из `raw_tweets` последние необработанные записи
2. Фильтрует и ранжирует по релевантности и engagement
3. Извлекает из `posts` последние опубликованные посты (RAG — для соответствия стилю и проверки уникальности)
4. Генерирует новый пост через LLM (Groq, модель `llama-3.3-70b-versatile`)
5. **Проверяет семантическую уникальность**: сравнивает сгенерированный пост с уже опубликованными — если смысл дублирует существующий пост, отклоняет и логирует причину
6. Если пост уникален — сохраняет в таблицу `posts` со статусом `pending`
7. Уведомляет Coordinator Agent о готовности поста

**Типы постов:**
- `news_digest` — дайджест 3-5 новостей
- `deep_dive` — глубокий разбор одной темы
- `tool_spotlight` — обзор инструмента
- `opinion` — авторское мнение на тренд

---

### 3. Publisher Agent — Агент публикации

**Ответственность:** Оформление и публикация поста/опроса в Telegram-канал.

**Что делает:**
- Забирает посты со статусом `pending` из таблицы `posts`
- Форматирует текст для Telegram (Markdown, длина ≤ 4096 символов)
- Публикует в Telegram-канал через Bot API
- Обновляет статус поста на `published`, записывает `published_at`
- При ошибке публикации — retry до `MAX_RETRY_ATTEMPTS` раз с интервалом 1 час
- После исчерпания попыток — уведомляет Coordinator Agent

**Также публикует опросы:**
- Генерация через poll_generator (Groq)
- Отправка через `bot.send_poll()`

---

### 4. Tester Agent — Агент тестирования

**Ответственность:** Автоматическое тестирование функционала через Telegram API.

**Что делает:**
- Тестирует команды бота через Telegram Bot API (отправляет команды, анализирует ответы)
- Проверяет соответствие поведения бота заявленным требованиям
- Тест-кейсы: `/add_schedule`, `/remove_schedule`, `/topics`, `/add_topic`, `/scrape`, `/create_post` и др.
- При обнаружении несоответствия — формирует баг-репорт и передаёт Coordinator Agent
- Может запускаться вручную или автоматически после деплоя изменений

**Тест-сценарии (примеры):**
```
Команда: /add_schedule news_digest 10:00 MON,WED
Ожидание: задача добавлена, ID возвращён, /schedule показывает новую задачу
Проверка: /schedule содержит добавленную задачу с правильным временем и днями

Команда: /remove_schedule <id>
Ожидание: задача удалена, /schedule не содержит её
```

---

### 5. Coordinator Agent — Агент-координатор

**Ответственность:** Оркестрация всей системы, маршрутизация задач, мониторинг.

**Что делает:**
- Запускает sub-agents по расписанию (APScheduler)
- Получает события от агентов (готов пост / ошибка / тест провален)
- Маршрутизирует: Analyzer → Publisher, Tester → Developer notification
- Принимает команды от администратора через Telegram-бот
- Предоставляет `/status` с агрегированным состоянием всей системы
- Логирует все события с помощью structlog

---

## Что сохраняется из `telegram-agent`

- **Один Telegram-бот** — один токен, один admin chat_id
- **Один Telegram-канал** — публикации идут в один канал
- **Supabase** — та же схема БД (`raw_tweets`, `posts`, `polls`, `analytics`)
- **Groq LLM** — `llama-3.3-70b-versatile` через AsyncGroq
- **Промпты** — версионированные `.md` файлы в `config/prompts/`, registry.yaml
- **Расписание** — `config/schedule.json` (персистентное, редактируется через бота)
- **Теги скрапинга** — `config/topics.json` (редактируется через `/add_topic`, `/remove_topic`)
- **Команды бота** — весь существующий набор команд сохраняется

---

## Технический стек

```
Python 3.11
python-telegram-bot 21.5    # Telegram Bot API
groq 0.11.0                 # LLM (llama-3.3-70b-versatile)
supabase >=2.15.0           # База данных (REST API)
apscheduler 3.10.4          # Планировщик (AsyncIOScheduler, Europe/Moscow)
pydantic 2.8.2              # Модели данных
pydantic-settings 2.4.0     # Конфиг из .env
structlog 24.4.0            # Структурированные логи
httpx 0.27.0                # HTTP-клиент (HN API)
tenacity 9.0.0              # Retry logic
PyYAML 6.0.2                # Конфиги
```

---

## Структура проекта

```
ai_news_telegram_channel/
├── CLAUDE.md                    # Этот файл
├── .env                         # Секреты (не в git)
├── .env.example
├── requirements.txt
├── config/
│   ├── schedule.json            # Расписание публикаций
│   ├── topics.json              # Теги для скрапинга
│   └── prompts/                 # Версионированные промпты
│       ├── registry.yaml
│       ├── news_digest_v1.0.0.md
│       ├── deep_dive_v1.0.0.md
│       ├── tool_spotlight_v1.0.0.md
│       ├── opinion_v1.0.0.md
│       └── poll_generator_v1.0.0.md
└── src/
    ├── main.py                  # Точка входа, Coordinator Agent
    ├── config.py                # Pydantic Settings
    ├── agents/
    │   ├── coordinator.py       # Coordinator Agent
    │   ├── scraper_agent.py     # Scraper Agent
    │   ├── analyzer_agent.py    # Analyzer Agent
    │   ├── publisher_agent.py   # Publisher Agent
    │   └── tester_agent.py      # Tester Agent
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

## Safety Guardrails (Безопасность — приоритет #1)

### 🔴 Операции, требующие явного подтверждения

**НИКОГДА не выполняй без подтверждения пользователя:**

| Категория | Операции |
|-----------|----------|
| **Database** | `DROP TABLE`, `DROP DATABASE`, `TRUNCATE TABLE`, `DELETE FROM` без WHERE, `ALTER TABLE DROP COLUMN` |
| **Filesystem** | `rm -rf`, `rm -rf /`, `mkfs`, `dd if=/dev/zero`, `truncate -s 0` на важных файлах |
| **Git** | `git reset --hard`, `git push --force`, `git clean -fd`, `git filter-branch` |
| **System** | `iptables -F`, `kill -9 1`, `userdel -r`, `systemctl stop` критичных сервисов |
| **Docker/K8s** | `docker system prune -a`, `kubectl delete all --all`, `kubectl drain --force` |
| **Cloud** | `aws s3 rm --recursive`, `gcloud sql instances delete`, `az group delete` |

### 🛡️ Протокол подтверждения

Перед опасной операцией:
1. **STOP** — Немедленно остановись
2. **VERIFY** — Убедись, что команда необходима
3. **BACKUP** — Проверь наличие бэкапов
4. **ASK** — Запроси подтверждение с уникальным кодом
5. **LOG** — Задокументируй, что будет сделано
6. **EXECUTE** — Только после явного подтверждения

**Подробный список**: `.claude/skills/safety-guardrails.md`  
**Конфигурация**: `.claude/settings.json` → раздел `safety`

---

## Anti-Patterns (избегать)

- ❌ Прямые вызовы между агентами — только через Coordinator
- ❌ Хардкодить промпты в коде — только через registry
- ❌ Хранить credentials в коде — только env vars
- ❌ Публиковать посты без проверки уникальности
- ❌ Игнорировать ошибки без уведомления Coordinator
- ❌ Синхронные блокирующие вызовы — async/await везде

---

## Запуск агента

```bash
# Активировать окружение и запустить бота
venv\Scripts\python.exe -m src.main
```

## Iron Law

**NO FIXES WITHOUT: SCOPE → TRACE → DIAGNOSE → FIX → VERIFY**

При любом изменении: сначала понять проблему, потом чинить.
