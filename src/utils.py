from telegram import Bot
from src.config import settings

_commentator_bot: Bot | None = None


def get_bot() -> Bot:
    return Bot(token=settings.telegram_bot_token)


def get_commentator_bot() -> Bot | None:
    global _commentator_bot
    if not settings.commentator_bot_token:
        return None
    if _commentator_bot is None:
        _commentator_bot = Bot(token=settings.commentator_bot_token)
    return _commentator_bot
