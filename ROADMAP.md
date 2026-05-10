# ROADMAP: 3 новые фичи для AI News Bot

> Статус: 🔄 В работе  
> Дата начала: 2026-05-04  
> Порядок реализации: **Фича 2 → Фича 1 → Фича 3**

---

## Фича 2: Reddit + Dev.to как источники данных

**Цель:** расширить скрапинг с HackerNews на Reddit и Dev.to; при совпадении истории из нескольких источников — объединять для генерации более богатого поста.

### SQL-миграция (выполнить в Supabase SQL Editor первым делом)

```sql
ALTER TABLE raw_tweets
  ADD COLUMN source TEXT NOT NULL DEFAULT 'hackernews',
  ADD COLUMN merge_group_id TEXT;

CREATE INDEX idx_raw_tweets_source ON raw_tweets(source);
CREATE INDEX idx_raw_tweets_merge_group ON raw_tweets(merge_group_id);
```

### Новые файлы

| Файл | Назначение |
|------|-----------|
| `src/scrapers/__init__.py` | пакет |
| `src/scrapers/hackernews.py` | HN-логика (перенос из scraper_agent) |
| `src/scrapers/reddit.py` | Reddit JSON API (анонимный, без OAuth) |
| `src/scrapers/devto.py` | Dev.to API |
| `src/scrapers/merger.py` | cross-source дедупликация |
| `config/sources.json` | subreddits и dev.to-теги |

### Шаги

- [x] **2.1** Выполнить SQL-миграцию в Supabase (колонки `source`, `merge_group_id`)
- [x] **2.2** Добавить `source: SourceType` и `merge_group_id: str | None` в `RawTweet` (`models.py`)
- [x] **2.3** Добавить `sources: list[SourceType]` и `merged_count: int` в `NewsItem` (`models.py`)
- [x] **2.4** Добавить настройки в `config.py`: `reddit_user_agent`, `reddit_min_score`, `devto_min_reactions`, `merge_similarity_threshold`
- [x] **2.5** Создать `config/sources.json` с subreddits и dev.to-тегами
- [x] **2.6** Создать `src/scrapers/__init__.py` (пустой)
- [x] **2.7** Создать `src/scrapers/hackernews.py` (перенести `_fetch_hn_stories` из `scraper_agent.py`)
- [x] **2.8** Создать `src/scrapers/reddit.py` (GET `reddit.com/r/{sub}/new.json`, prefix `rd_`)
- [x] **2.9** Создать `src/scrapers/devto.py` (GET `dev.to/api/articles?tag={tag}`, prefix `dv_`)
- [x] **2.10** Создать `src/scrapers/merger.py` (canonical URL match + Jaccard шинглы, порог 0.6)
- [x] **2.11** Обновить `scraper_agent.py`: стать оркестратором — вызывать все три scraper'а, мержить, сохранять
- [x] **2.12** Обновить `topics.json`: добавить поле `sources` к каждой записи (дефолт `["hackernews"]`)
- [x] **2.13** Обновить `analyzer_agent._filter_and_rank()`: группировать по `merge_group_id`, агрегировать источники
- [x] **2.14** Обновить `_format_news_for_prompt()` в `content_generator.py`: показывать все источники для merged-item
- [x] **2.15** Обновить `/topics` в `main.py`: показывать источники для каждой записи
- [x] **2.16** Прогнать `/scrape` и проверить логи: сколько записей, сколько grouped, правильные id-префиксы

### Технические детали

- **Reddit:** OAuth2 client credentials (Reddit заблокировал анонимный JSON). Токен: `POST https://www.reddit.com/api/v1/access_token` Basic Auth (client_id:secret), body `grant_type=client_credentials`. API: `https://oauth.reddit.com/r/{sub}/new`. Credentials: `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` в `.env`. Если не заданы — Reddit скипается без ошибки. Создать app: reddit.com/prefs/apps → type=script
- **Dev.to:** `GET https://dev.to/api/articles?tag={tag}&per_page=30`; поля: `title`, `url`, `positive_reactions_count`, `comments_count`, `user.username`, `id`
- **Мерж-стратегия:** сначала canonical URL (убрать utm_*, www, trailing slash) → при совпадении точный дубль; затем Jaccard similarity на word-3-граммах заголовков, порог 0.6
- **`merge_group_id`** вычисляется только для новых tweets в текущем батче; старые записи не трогаем
- **Backward compat:** если в записи topics.json нет поля `sources` → считать `["hackernews"]`

### Риски

- Reddit может вернуть 429/403 для анонимных запросов → throttle 1 req/s, логировать, не падать
- Jaccard 0.6 может склеить несвязанные новости с общими словами → проверить на реальных данных, при необходимости поднять до 0.7
- Сумма likes после мержа может ломать relevance_score → `min(sum_likes, 10_000)` в `_relevance_score`

---

## Фича 1: Картинки к постам (Pollinations.ai + Pillow fallback)

**Цель:** каждый пост сопровождать минималистичной картинкой. Основной инструмент — Pollinations.ai (бесплатный, без API-ключа). Если недоступен — генерировать текстовую карточку через Pillow.

### SQL-миграция

```sql
ALTER TABLE posts
  ADD COLUMN image_prompt TEXT,
  ADD COLUMN image_url TEXT,
  ADD COLUMN image_skipped_reason TEXT;
```

### Новые файлы

| Файл | Назначение |
|------|-----------|
| `src/generator/image_generator.py` | генерация image-prompt + скачивание/fallback |
| `config/prompts/image_prompt_v1.0.0.md` | промпт для генерации описания картинки |

### Шаги

- [x] **1.1** Выполнить SQL-миграцию (`image_prompt`, `image_url`, `image_skipped_reason` в `posts`)
- [x] **1.2** Добавить три поля в `Post` (`models.py`)
- [x] **1.3** Добавить в `config.py`: `pollinations_base_url`, `pollinations_width`, `pollinations_height`, `pollinations_timeout_s`, `images_enabled`
- [x] **1.4** Создать `config/prompts/image_prompt_v1.0.0.md` и зарегистрировать в `registry.yaml`
- [x] **1.5** Создать `src/generator/image_generator.py` (`generate_image_prompt`, `fetch_image_bytes`, `make_fallback_image`)
- [x] **1.6** В `content_generator.generate_post()`: после генерации текста вызвать `generate_image_prompt()`, записать `post.image_prompt` и `post.image_url`
- [x] **1.7** В `publisher_agent.publish_post()`: Pollinations → Pillow fallback → text-only fallback; caption ≤1024 или photo+reply
- [x] **1.8** Обновить `_mark_published()`: сохранять `image_skipped_reason` при fallback
- [x] **1.9** Обновить preview в `coordinator.send_post_for_review()`: отправлять фото + текст при approve
- [x] **1.10** В `dry_run`: генерировать prompt, но не дёргать Pollinations (логировать url)
- [x] **1.11** Установить Pillow: добавить `Pillow>=10.0.0` в `requirements.txt`
- [x] **1.12** Установить Pillow в venv и прогнать e2e: `/post` → убедиться что фото приходит

### Технические детали

- **Pollinations URL:** `https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=512&nologo=true&seed={seed}`
- **seed** = `hash(post.id) % 2**31` — детерминированный, чтобы preview == публикация
- **Pillow fallback:** тёмный фон `#1a1a2e`, белый текст, первые 100 символов заголовка, размер 1024×512
- **Caption лимит:** Telegram ограничивает caption 1024 символами; если текст длиннее — фото без caption + текст отдельным сообщением (reply)
- **Image prompt стиль:** "minimalist flat illustration, soft pastel colors, no text, no letters, abstract, {topic_keywords}"
- **Groq-вызов для image prompt:** max_tokens=80, temperature=0.7 (короткий промпт, не нужен большой контекст)

### Риски

- Pollinations нестабилен (free tier): тайм-ауты, 502 → 2 retry по 15s, затем Pillow fallback
- Качество картинок для tech-тематики слабое → стиль "abstract minimalist" даёт приемлемый результат; можно доработать промпт без кода
- Telegram parse_mode='HTML' в caption — такой же как в `send_message`; проблем не ожидается

---

## Фича 3: Агент-комментатор

**Цель:** автономный бот-персонаж, который реагирует на посты в канале — ставит реакции и пишет человеческие комментарии в discussion group.

> ⚠️ Требует предварительной настройки:
> - Создать нового бота через BotFather → токен в `.env` как `COMMENTATOR_BOT_TOKEN`
> - Добавить бота как администратора канала (для реакций)
> - Добавить бота в discussion group (для комментариев)

> ⛔ Голосование в опросах через Bot API **невозможно** — не реализуем.

### SQL-миграция

```sql
CREATE TABLE comment_actions (
  id TEXT PRIMARY KEY,
  post_id TEXT NOT NULL REFERENCES posts(id),
  channel_message_id INTEGER NOT NULL,
  discussion_chat_id BIGINT,
  discussion_message_id INTEGER,
  action_type TEXT NOT NULL,          -- 'reaction' | 'comment' | 'skipped'
  emoji TEXT,
  comment_text TEXT,
  persona TEXT,                        -- 'skeptic' | 'excited' | 'curious' | 'ironic' | 'neutral'
  scheduled_at TIMESTAMPTZ NOT NULL,
  executed_at TIMESTAMPTZ,
  status TEXT NOT NULL DEFAULT 'scheduled',  -- 'scheduled' | 'awaiting_forward' | 'done' | 'failed'
  error TEXT
);
CREATE INDEX idx_comment_actions_status ON comment_actions(status);
CREATE INDEX idx_comment_actions_post ON comment_actions(post_id);
```

### Новые файлы

| Файл | Назначение |
|------|-----------|
| `src/agents/commentator_agent.py` | основной агент |
| `config/prompts/commentator_skeptic_v1.0.0.md` | промпт персоны "скептик" |
| `config/prompts/commentator_excited_v1.0.0.md` | промпт персоны "восхищённый" |
| `config/prompts/commentator_curious_v1.0.0.md` | промпт персоны "любопытный" |
| `config/prompts/commentator_ironic_v1.0.0.md` | промпт персоны "ироничный" |
| `config/prompts/commentator_neutral_v1.0.0.md` | промпт персоны "нейтральный" |

### Шаги

- [ ] **3.1** Выполнить SQL-миграцию (таблица `comment_actions`) ← **твоя задача**
- [x] **3.2** Добавить enums и модель `CommentAction` в `models.py`
- [x] **3.3** Добавить в `config.py`: `commentator_bot_token`, `commentator_enabled`, `commentator_react_probability`, `commentator_comment_probability`, `commentator_min_delay_s`, `commentator_max_delay_s`
- [x] **3.4** Добавить `get_commentator_bot()` в `utils.py` — singleton `Bot`, возвращает `None` если token пустой
- [x] **3.5** Создать 5 prompt-файлов для персон и зарегистрировать в `registry.yaml`
- [x] **3.6** Создать `src/agents/commentator_agent.py`
- [x] **3.7** В `publisher_agent.publish_post()`: после успешной публикации вызвать `schedule_actions(post)` если `commentator_enabled`
- [x] **3.8** В `main.py`: добавить `MessageHandler` на `IS_AUTOMATIC_FORWARD` → `on_discussion_forward()`
- [x] **3.9** В `main.py post_init()`: вызвать `restore_pending_actions(scheduler)`
- [x] **3.10** Добавить команды `/commentator_status` и `/commentator_test` в `main.py`
- [ ] **3.11** Прогнать e2e: опубликовать тестовый пост → дождаться форварда → проверить `discussion_message_id` в БД → `/commentator_test <post_id>` → проверить реакцию и комментарий ← **твоя задача**

### Технические детали

- **Реакции:** `commentator_bot.set_message_reaction(chat_id=channel_id, message_id=channel_message_id, reaction=[ReactionTypeEmoji(emoji='🔥')])`
- **Комментарии:** `commentator_bot.send_message(chat_id=discussion_chat_id, reply_to_message_id=discussion_message_id, text=comment_text)`
- **discussion_chat_id:** получить через `main_bot.get_chat(channel_id)` → поле `linked_chat_id`
- **Форвард:** Telegram автоматически форвардит пост канала в discussion group (1–10 сек); ловим через `MessageHandler(filters.IS_AUTOMATIC_FORWARD, handler)`
- **Задержка:** `random.uniform(commentator_min_delay_s, commentator_max_delay_s)` секунд после публикации
- **Вероятность реакции:** 50% по умолчанию; вероятность комментария: 30% по умолчанию
- **При рестарте:** просроченные `scheduled` action запускаем с задержкой 60–180 сек (не палимся одновременными ответами)
- **Race на discussion_message_id:** если `execute_action` срабатывает раньше чем пришёл форвард → статус `awaiting_forward`, повторная попытка через 5 минут

### Персоны и примеры комментариев

| Персона | Характер | Пример |
|---------|---------|--------|
| `skeptic` | сомневается, не верит | "хм, это ж не первый раз уже. посмотрим что будет через месяц" |
| `excited` | восхищается, удивляется | "вот это да!! не ожидал что так быстро" |
| `curious` | задаёт вопросы, хочет разобраться | "интересно а как они это вообще сделали технически" |
| `ironic` | ироничный, с сарказмом | "ага, очередная революция в ai, которая всё изменит 😅" |
| `neutral` | просто реагирует | "спасибо, полезная инфа" |

### Риски

- Спам-фильтры Telegram: слишком частые комментарии → ограничить не более 3 комментов в час глобально
- `discussion_message_id` не приходит (нет linked group или задержка >30 мин) → watchdog: через 30 мин помечаем `failed`
- `set_message_reaction` требует что бот — admin канала
- PTB 21.5 поддерживает `set_message_reaction` — перепроверить при установке

---

## Сводный прогресс

| Фича | Статус | Прогресс |
|------|--------|----------|
| Фича 2: Reddit + Dev.to | 🔄 В работе | 16/16 шагов (Reddit API — в процессе получения доступа) |
| Фича 1: Картинки | ✅ Готово | 12/12 шагов |
| Фича 3: Комментатор | 🔄 В работе | 10/11 шагов (нужна discussion group) |

---

## Changelog

- **2026-05-04** — план создан; начинаем с Фичи 2
- **2026-05-04** — Фича 2: выполнены шаги 2.2–2.15; ожидает SQL-миграции (2.1) и проверки (2.16)
- **2026-05-04** — Фича 2: Reddit переведён на OAuth2 (анонимный API заблокирован)
- **2026-05-04** — Фича 1: выполнены шаги 1.2–1.11; ожидает SQL-миграции (1.1) и e2e-теста (1.12)
- **2026-05-05** — Фича 3: выполнены шаги 3.2–3.10; ожидает SQL-миграции (3.1) и e2e-теста (3.11)
- **2026-05-05** — Фикс: edit_message_text → _edit_query_message (фото-сообщения не поддерживают edit_message_text)
- **2026-05-10** — Фича 1 завершена: SQL-миграция + Pillow e2e прошёл успешно
- **2026-05-10** — Фича 2: SQL-миграция и e2e /scrape выполнены; Reddit на паузе (получение API-доступа)
- **2026-05-10** — Фича 3: добавлена команда /comment; e2e (3.11) заблокирован — нет discussion group
