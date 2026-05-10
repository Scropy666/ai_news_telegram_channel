# AI News Telegram Channel Bot

Automated multi-agent system that scrapes AI/tech news, generates unique posts using LLM, and publishes them to a Telegram channel — with images and a humanlike commentator bot.

## Features

- **Multi-source scraping** — HackerNews, Reddit, Dev.to; cross-source deduplication via Jaccard similarity
- **LLM-powered generation** — 4 post formats (news digest, deep dive, tool spotlight, opinion) via Groq (free tier)
- **Uniqueness check** — semantic comparison against recent posts before publishing; duplicates are rejected
- **AI-generated images** — Pollinations.ai with Pillow text-card fallback
- **Admin approval flow** — posts sent to admin with inline buttons (publish now / schedule / save / reject)
- **Commentator bot** — second bot leaves reactions and human-style comments with randomized delay and 5 personas
- **Persistent scheduling** — cron-like schedule stored in `config/schedule.json`, survives restarts
- **Full admin control** — manage schedule, topics, and posts via Telegram commands

## Architecture

```
┌──────────────────────────────────────────────┐
│             Coordinator Agent                │
│  APScheduler · Telegram Bot · Admin commands │
└──────┬──────────┬──────────┬─────────────────┘
       │          │          │
  ┌────▼───┐ ┌────▼────┐ ┌───▼──────┐
  │Scraper │ │Analyzer │ │Publisher │
  │Agent   │ │Agent    │ │Agent     │
  └────────┘ └────┬────┘ └──────────┘
                  │              │
            Uniqueness     Commentator
            Check (LLM)    Agent
```

Agents communicate through Supabase (shared state), not direct calls.

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.11 |
| Telegram | python-telegram-bot 21.5 |
| LLM | Groq (`llama-3.3-70b-versatile`) |
| Database | Supabase (REST API) |
| Scheduler | APScheduler 3.10 |
| Images | Pollinations.ai + Pillow fallback |
| Config | Pydantic Settings |
| Logging | structlog |

## Quick Start

### 1. Clone & install

```bash
git clone https://github.com/YOUR_USERNAME/ai-news-telegram-channel.git
cd ai-news-telegram-channel
python -m venv venv
venv\Scripts\activate      # Windows
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Fill in `.env`:

```env
TELEGRAM_BOT_TOKEN=        # main bot token from @BotFather
TELEGRAM_CHANNEL_ID=       # @channel_username or numeric ID
TELEGRAM_ADMIN_CHAT_ID=    # your personal chat_id
COMMENTATOR_BOT_TOKEN=     # second bot token (optional)

GROQ_API_KEY=              # free at console.groq.com

SUPABASE_URL=              # https://<project>.supabase.co
SUPABASE_SERVICE_ROLE_KEY= # from Supabase project settings
```

### 3. Set up Supabase

Run the following SQL migrations in your Supabase SQL Editor:

<details>
<summary>Database schema</summary>

```sql
-- Core tables
CREATE TABLE raw_tweets (
  id TEXT PRIMARY KEY,
  text TEXT NOT NULL,
  author TEXT,
  likes INTEGER DEFAULT 0,
  retweets INTEGER DEFAULT 0,
  url TEXT,
  topic TEXT,
  source TEXT NOT NULL DEFAULT 'hackernews',
  merge_group_id TEXT,
  scraped_at TIMESTAMPTZ DEFAULT NOW(),
  status TEXT DEFAULT 'new'
);

CREATE TABLE posts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  type TEXT NOT NULL,
  content TEXT NOT NULL,
  prompt_version TEXT,
  image_prompt TEXT,
  image_url TEXT,
  image_skipped_reason TEXT,
  source_tweet_ids TEXT[],
  scheduled_at TIMESTAMPTZ,
  published_at TIMESTAMPTZ,
  telegram_message_id INTEGER,
  status TEXT DEFAULT 'pending',
  retry_count INTEGER DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Commentator agent
CREATE TABLE comment_actions (
  id TEXT PRIMARY KEY,
  post_id UUID NOT NULL REFERENCES posts(id),
  channel_message_id INTEGER NOT NULL,
  discussion_chat_id BIGINT,
  discussion_message_id INTEGER,
  action_type TEXT NOT NULL,
  emoji TEXT,
  comment_text TEXT,
  persona TEXT,
  scheduled_at TIMESTAMPTZ NOT NULL,
  executed_at TIMESTAMPTZ,
  status TEXT NOT NULL DEFAULT 'scheduled',
  error TEXT
);

CREATE INDEX idx_raw_tweets_status ON raw_tweets(status);
CREATE INDEX idx_raw_tweets_source ON raw_tweets(source);
CREATE INDEX idx_posts_status ON posts(status);
CREATE INDEX idx_comment_actions_status ON comment_actions(status);
```
</details>

### 4. Run

```bash
venv\Scripts\python.exe -m src.main
```

## Bot Commands

| Command | Description |
|---------|-------------|
| `/post [type] [#tag1 #tag2]` | Generate and review a new post |
| `/scrape` | Run scraper manually |
| `/schedule` | Show publishing schedule |
| `/add_schedule <type> <HH:MM> <days>` | Add scheduled job (e.g. `news_digest 10:00 MON,WED`) |
| `/remove_schedule <id>` | Remove a scheduled job |
| `/topics` | List scraping topics |
| `/add_topic <query>` | Add a scraping topic |
| `/remove_topic <query>` | Remove a scraping topic |
| `/poll [topic]` | Publish a poll immediately |
| `/commentator_status` | Show commentator stats (last 24h) |
| `/commentator_test <post_id>` | Trigger commentator actions manually |
| `/status` | System status overview |

### Post types

`news_digest` · `deep_dive` · `tool_spotlight` · `opinion`

### Schedule day formats

`MON,WED,FRI` · `*` (every day) · `MON-FRI`

## Configuration

### Topics (`config/topics.json`)

Controls what gets scraped. Editable via `/add_topic` and `/remove_topic` without restart.

```json
{
  "queries": [
    {
      "query": "AI agents",
      "topic": "ai_agents",
      "sources": ["hackernews", "reddit", "devto"]
    }
  ]
}
```

### Schedule (`config/schedule.json`)

Persistent schedule managed via bot commands. Auto-loaded on startup.

### Prompts (`config/prompts/`)

Versioned LLM prompt files. Each prompt type has a version tracked in `registry.yaml`. Edit prompts without touching code.

## Commentator Bot Setup

The commentator bot simulates a human subscriber:

1. Create a second bot via @BotFather → put token in `COMMENTATOR_BOT_TOKEN`
2. Add the bot as **admin** to your channel (needed for reactions)
3. Add the bot to your **discussion group** (for comments)
4. Set `COMMENTATOR_ENABLED=true`

The bot randomly picks one of 5 personas (`skeptic`, `excited`, `curious`, `ironic`, `neutral`) and posts a short comment with a random delay (1–15 min by default).

## Reddit Setup (optional)

Anonymous Reddit API is blocked. To enable Reddit scraping:

1. Go to [reddit.com/prefs/apps](https://www.reddit.com/prefs/apps) → create app (type: **script**)
2. Add to `.env`:
   ```env
   REDDIT_CLIENT_ID=your_client_id
   REDDIT_CLIENT_SECRET=your_client_secret
   ```

If not set, Reddit is silently skipped.

## Project Structure

```
├── src/
│   ├── main.py                  # entry point, Telegram bot, command handlers
│   ├── config.py                # Pydantic settings from .env
│   ├── agents/
│   │   ├── coordinator.py       # pipeline orchestration, admin review flow
│   │   ├── scraper_agent.py     # HN + Reddit + Dev.to scraping
│   │   ├── analyzer_agent.py    # filter, rank, generate, uniqueness check
│   │   ├── publisher_agent.py   # Telegram channel publishing with retry
│   │   └── commentator_agent.py # reaction + comment scheduling
│   ├── scrapers/
│   │   ├── hackernews.py
│   │   ├── reddit.py
│   │   ├── devto.py
│   │   └── merger.py            # cross-source deduplication
│   ├── generator/
│   │   ├── content_generator.py # LLM post generation + image prompt
│   │   ├── image_generator.py   # Pollinations.ai + Pillow fallback
│   │   ├── poll_generator.py
│   │   └── prompt_registry.py   # versioned prompt loader
│   └── database/
│       ├── client.py
│       └── models.py
├── config/
│   ├── topics.json              # scraping queries (editable via bot)
│   ├── schedule.json            # publishing schedule (editable via bot)
│   └── prompts/                 # versioned LLM prompt files
├── .env.example
└── requirements.txt
```

## License

MIT
