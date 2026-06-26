import structlog
from datetime import datetime, timezone, timedelta
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import TelegramError

from src.config import settings
from src.database.models import Post
from src.scheduler.scheduler import TYPE_LABELS
from src.utils import get_bot
from src.generator.image_generator import fetch_image_bytes, make_seed
from src.agent_core.goals import guardrail

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
    """Suggest-раскладка: одобрить/вето + через час/в черновики.

    Переиспользует существующие callback-коды (pn/rj/p1h/sv), обрабатываемые
    callback_post_action в main.py. Менять main.py не требуется.
    """
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton('✅ Опубликовать', callback_data=f'pn_{post_id}'),
            InlineKeyboardButton('🚫 Вето',         callback_data=f'rj_{post_id}'),
        ],
        [
            InlineKeyboardButton('⏱ Через час',    callback_data=f'p1h_{post_id}'),
            InlineKeyboardButton('💾 В черновики',  callback_data=f'sv_{post_id}'),
        ],
    ])


async def send_post_for_review(
    post: Post,
    index: int,
    total: int,
    drafts: dict,
    reason: str = '',
) -> None:
    """Отправить один пост на review с клавиатурой выбора. Сохранить в drafts.

    reason: обоснование редактора (отображается блоком «🧠 Редактор: …»).
    """
    drafts[post.id] = post

    label = TYPE_LABELS.get(post.type, post.type)
    reason_block = f'🧠 Редактор: {reason}\n\n' if reason else ''
    header = f'📝 Пост {index}/{total} — {label}\n{reason_block}'
    bot = get_bot()
    keyboard = build_post_keyboard(post.id)

    async def _send_as_text() -> None:
        text = header + post.content
        if len(text) > 4096:
            text = text[:4090] + '...'
        await bot.send_message(
            chat_id=settings.telegram_admin_chat_id,
            text=text,
            parse_mode='HTML',
            reply_markup=keyboard,
        )

    try:
        img_bytes: bytes | None = None
        if post.image_prompt and settings.images_enabled:
            img_bytes = await fetch_image_bytes(post.image_prompt, make_seed(post.id or 'default'))

        if img_bytes:
            caption = header + post.content
            if len(caption) > 1024:
                caption = caption[:1020] + '...'
            try:
                await bot.send_photo(
                    chat_id=settings.telegram_admin_chat_id,
                    photo=img_bytes,
                    caption=caption,
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except TelegramError as img_err:
                logger.warning('send_photo_failed_fallback', post_id=post.id, error=str(img_err))
                await _send_as_text()
        else:
            await _send_as_text()
        logger.info('post_sent_for_review', post_id=post.id, index=index, has_image=bool(img_bytes))
    except TelegramError as e:
        logger.error('send_review_failed', post_id=post.id, error=str(e))


# ── Pipeline ──────────────────────────────────────────────────────────────────

def _count_posts_last_24h() -> int:
    """Посчитать посты, опубликованные или поставленные в очередь за последние 24 часа.

    Используется для guardrail max_posts_per_day.
    Считаем по scheduled_at (включает waiting_publish) и published_at (published).
    """
    from src.database.client import get_db
    db = get_db()
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    rows = (
        db.table('posts')
        .select('id')
        .in_('status', ['published', 'waiting_publish', 'pending'])
        .gte('scheduled_at', since)
        .execute()
        .data
    )
    return len(rows)


async def run_pipeline(
    post_type: str,
    drafts: dict | None = None,
    tags: list[str] | None = None,
) -> None:
    """Scrape → generate batch → отправить все посты на review.

    Авто-ветка (tags не заданы): использует Editor Agent для принятия решений.
    Ручная ветка (tags заданы): классический run_multi, Editor не вмешивается.
    """
    from src.agents.scraper_agent import run as scraper_run, scrape_by_tags
    from src.agents.analyzer_agent import (
        run_multi, get_ranked_items, run_with_decisions,
        load_new_tweets, mark_tweets_processed,
    )
    from src.agents.editor_agent import decide

    if drafts is None:
        drafts = {}

    logger.info('pipeline_start', post_type=post_type, tags=tags)

    if tags:
        # ── Ручная ветка: теги заданы пользователем, Editor не нужен ─────────
        raw_tweets = await scrape_by_tags(tags)
        if not raw_tweets:
            await notify_admin(f'📭 По тегам {", ".join(tags)} ничего не найдено.')
            return
        logger.info('pipeline_scraped_by_tags', tags=tags, found=len(raw_tweets))

        posts = await run_multi(source_tweets=raw_tweets, post_type=post_type, tags=tags)

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
        return

    # ── Авто-ветка: Editor Agent принимает решения ────────────────────────────
    scraper_result = await scraper_run()
    logger.info('pipeline_scraped', new_saved=scraper_result.new_saved)
    if scraper_result.errors:
        await notify_admin(f'⚠️ Scraper warnings: {", ".join(scraper_result.errors[:2])}')

    new_tweets = load_new_tweets()
    items = get_ranked_items(source_tweets=new_tweets)
    if not items:
        await notify_admin('📭 Нет новых новостей для анализа.')
        return

    decisions = await decide(items)               # ИИ-редактор принимает решение
    pairs = await run_with_decisions(items, decisions)   # генерим только одобренные

    # Помечаем рассмотренный батч обработанным независимо от исхода —
    # иначе те же твиты будут переанализироваться каждый прогон (как в run_multi).
    mark_tweets_processed([t.id for t in new_tweets])

    if not pairs:
        await notify_admin('📭 Редактор не одобрил ни одной темы к публикации.')
        return

    # Guardrail max_posts_per_day: проверяем сколько уже отправлено за 24ч
    max_per_day: int = guardrail('max_posts_per_day', 8)
    already_today = _count_posts_last_24h()
    remaining_slots = max(max_per_day - already_today, 0)

    logger.info(
        'pipeline_day_guardrail',
        max_per_day=max_per_day,
        already_today=already_today,
        remaining_slots=remaining_slots,
        candidate_pairs=len(pairs),
    )

    if remaining_slots == 0:
        await notify_admin(
            f'🛑 Лимит публикаций за 24ч достигнут ({already_today}/{max_per_day}). '
            f'Редактор одобрил {len(pairs)} тем, но они пропущены.'
        )
        return

    pairs_to_send = pairs[:remaining_slots]
    if len(pairs) > remaining_slots:
        logger.info('pipeline_day_guardrail_trimmed',
                    trimmed=len(pairs) - remaining_slots, sent=remaining_slots)

    await notify_admin(f'🧠 Редактор одобрил {len(pairs_to_send)} тем. Проверь решения 👇')
    for i, (post, reason) in enumerate(pairs_to_send, 1):
        await send_post_for_review(post, index=i, total=len(pairs_to_send), drafts=drafts, reason=reason)

    logger.info('pipeline_done', posts_sent=len(pairs_to_send))


async def run_poll_pipeline(topic: str) -> None:
    """Опросы публикуются без approve."""
    from src.agents.publisher_agent import publish_poll

    logger.info('poll_pipeline_start', topic=topic)
    result = await publish_poll(topic)

    if not result.success:
        await notify_admin(f'❌ Ошибка публикации опроса: {result.error}')
