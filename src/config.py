from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8')

    telegram_bot_token: str
    telegram_channel_id: str
    telegram_admin_chat_id: str

    groq_api_key: str

    supabase_url: str
    supabase_service_role_key: str
    supabase_access_token: str = ''

    max_retry_attempts: int = 3
    min_engagement_likes: int = 50
    dry_run: bool = False

    # Uniqueness checker — сколько последних постов анализировать
    uniqueness_recent_posts: int = 5

    # Image generation (Pollinations.ai)
    images_enabled: bool = True
    pollinations_base_url: str = 'https://image.pollinations.ai/prompt'
    pollinations_width: int = 1024
    pollinations_height: int = 512
    pollinations_timeout_s: int = 30

    # Reddit scraper (OAuth2 — нужны client_id и client_secret из https://www.reddit.com/prefs/apps)
    reddit_client_id: str = ''
    reddit_client_secret: str = ''
    reddit_username: str = 'ai_news_bot'   # для User-Agent
    reddit_min_score: int = 20

    # Dev.to scraper
    devto_min_reactions: int = 10

    # Cross-source merger
    merge_similarity_threshold: float = 0.6

    # Commentator agent
    commentator_bot_token: str = ''
    commentator_enabled: bool = True
    commentator_react_probability: float = 0.7
    commentator_comment_probability: float = 0.7
    commentator_min_delay_s: int = 60
    commentator_max_delay_s: int = 900


settings = Settings()
