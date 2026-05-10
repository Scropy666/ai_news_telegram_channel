import structlog
from datetime import datetime, timezone
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import TelegramError

from src.config import settings
from src.database.models import Post
from src.scheduler.scheduler import TYPE_LABELS
from src.utils import get_bot

logger = structlog.get_logger()

# Варианты отложенной публикации: код → (label, минуты)
PUBLISH_OPTIONS = [
    ('pn',  '⚡ Сейчас',        0),
    ('p30', '⏱ Через 30 мин',  30),
    ('p1h', '🕐 Через 1 час',   60),
    ('p4h', '🕓 Через 4 часа', 240),
    ('p8h', '🕗 Через 8 часов',480),
]


async def notify_admin(message: str) -> None:
    try:
        await get_bot().send_message(
            chat_id=settings.telegram_admin_chat_id,
            text=f'🤖 {message}',
        )
    except TelegramError:
        logger.error('admin_notify_failed', message=message[:100])


def build_post_keyboard(post_id: str) -> InlineKeyboardMarkup:
    """Клавиатура с вариантами публикации для одного поста."""
    rows = []
    # Варианты публикации по времени (по 2 в ряд)
    publish_row = []
    for code, label, _ in PUBLISH_OPTIONS:
        publish_row.append(InlineKeyboardButton(label, callback_data=f'{code}_{post_id}'))
        if len(publish_row) == 2:
            rows.append(publish_row)
            publish_row = []
    if publish_row:
        rows.append(publish_row)
    # Сохранить / Отклонить
    rows.append([
        InlineKeyboardButton('💾 Сохранить', callback_data=f'sv_{post_id}'),
        InlineKeyboardButton('🗑 Отклонить',  callback_data=f'rj_{post_id}'),
    ])
    return InlineKeyboardMarkup(rows)


async def send_post_for_review(post: Post, index: int, total: int, drafts: dict) -> None:
    """Отправить один пост на review с клавиатурой выбора. Сохранить в drafts."""
    drafts[post.id] = post

    label = TYPE_LABELS.get(post.type, post.type)
    header = f'📝 Пост {index}/{total} — {label}\n\n'
    bot = get_bot()
    keyboard = build_post_keyboard(post.id)

    try:
        if post.image_url and settings.images_enabled:
            await bot.send_photo(
                chat_id=settings.telegram_admin_chat_id,
                photo=post.image_url,
                caption=header + post.content,
                parse_mode='HTML',
                reply_markup=keyboard,
            )
        else:
            text = header + post.content
            if len(text) > 4096:
                text = text[:4090] + '...'
            await bot.send_message(
                chat_id=settings.telegram_admin_chat_id,
                text=text,
                parse_mode='HTML',
                reply_markup=keyboard,
            )
        logger.info('post_sent_for_review', post_id=post.id, index=index, has_image=bool(post.image_url))
    except TelegramError as e:
        logger.error('send_review_failed', post_id=post.id, error=str(e))


# ── Pipeline ──────────────────────────────────────────────────────────────────

async def run_pipeline(
    post_type: str,
    drafts: dict | None = None,
    tags: list[str] | None = None,
) -> None:
    """Scrape → generate batch → отправить все посты на review.

    tags: если указаны — скрапим только по этим тегам, иначе по topics.json.
    """
    from src.agents.scraper_agent import run as scraper_run, scrape_by_tags, _save_tweets
    from src.agents.analyzer_agent import run_multi

    if drafts is None:
        drafts = {}

    logger.info('pipeline_start', post_type=post_type, tags=tags)

    source_tweets = None  # None = analyzer загрузит из БД сам

    if tags:
        raw_tweets = await scrape_by_tags(tags)
        if not raw_tweets:
            await notify_admin(f'📭 По тегам {", ".join(tags)} ничего не найдено.')
            return
        source_tweets = raw_tweets  # передаём напрямую, минуя DB-запрос
        logger.info('pipeline_scraped_by_tags', tags=tags, found=len(raw_tweets))
    else:
        scraper_result = await scraper_run()
        logger.info('pipeline_scraped', new_saved=scraper_result.new_saved)
        if scraper_result.errors:
            await notify_admin(f'⚠️ Scraper warnings: {", ".join(scraper_result.errors[:2])}')

    posts = await run_multi(source_tweets=source_tweets, post_type=post_type, tags=tags)

    if not posts:
        await notify_admin(
            '📭 Не удалось сгенерировать ни одного уникального поста.\n'
            'Все темы уже были освещены или нет новых данных.'
        )
        return

    await notify_admin(f'✅ Сгенерировано {len(posts)} постов. Выбери действие для каждого 👇')

    for i, post in enumerate(posts, 1):
        await send_post_for_review(post, index=i, total=len(posts), drafts=drafts)

    logger.info('pipeline_done', posts_sent=len(posts))


async def run_poll_pipeline(topic: str) -> None:
    """Опросы публикуются без approve."""
    from src.agents.publisher_agent import publish_poll

    logger.info('poll_pipeline_start', topic=topic)
    result = await publish_poll(topic)

    if not result.success:
        await notify_admin(f'❌ Ошибка публикации опроса: {result.error}')
