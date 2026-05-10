import asyncio
import structlog
from datetime import datetime, timezone

from telegram.error import TelegramError

from src.config import settings
from src.database.models import Post, Poll, PublisherResult
from src.database.client import get_db
from src.generator.poll_generator import generate_poll
from src.generator.image_generator import fetch_image_bytes, make_fallback_image, make_seed
from src.utils import get_bot

logger = structlog.get_logger()


# ── Publish post ──────────────────────────────────────────────────────────────

async def _get_image_bytes(post: Post) -> tuple[bytes | None, str | None]:
    """Получить байты картинки: Pollinations → Pillow fallback. Возвращает (bytes, skipped_reason)."""
    if not settings.images_enabled or not post.image_prompt:
        return None, 'images_disabled_or_no_prompt'

    if settings.dry_run:
        logger.info('dry_run_image', post_id=post.id, image_url=post.image_url)
        return None, 'dry_run'

    seed = make_seed(post.id or 'default')
    img_bytes = await fetch_image_bytes(post.image_prompt, seed)
    if img_bytes:
        return img_bytes, None

    # Pillow fallback
    try:
        img_bytes = make_fallback_image(post.content[:120])
        return img_bytes, 'pollinations_failed_used_pillow'
    except Exception as e:
        logger.warning('pillow_fallback_failed', error=str(e))
        return None, f'all_image_methods_failed: {e}'


async def _send_with_image(bot, post: Post, img_bytes: bytes) -> int:
    """Отправить фото + текст одним сообщением."""
    msg = await bot.send_photo(
        chat_id=settings.telegram_channel_id,
        photo=img_bytes,
        caption=post.content,
        parse_mode='HTML',
    )
    return msg.message_id


async def publish_post(post: Post) -> PublisherResult:
    if settings.dry_run:
        logger.info('dry_run_post', post_id=post.id, preview=post.content[:100], image_url=post.image_url)
        _mark_published(post.id, message_id=0)
        return PublisherResult(success=True, published_at=datetime.now(timezone.utc))

    bot = get_bot()
    img_bytes, skipped_reason = await _get_image_bytes(post)

    for attempt in range(1, settings.max_retry_attempts + 1):
        try:
            if img_bytes:
                message_id = await _send_with_image(bot, post, img_bytes)
            else:
                msg = await bot.send_message(
                    chat_id=settings.telegram_channel_id,
                    text=post.content,
                    parse_mode='HTML',
                )
                message_id = msg.message_id

            published_at = datetime.now(timezone.utc)
            _mark_published(post.id, message_id, published_at, skipped_reason)
            logger.info('post_published', post_id=post.id, message_id=message_id, has_image=img_bytes is not None)

            if settings.commentator_enabled:
                from src.agents.commentator_agent import schedule_actions
                post.telegram_message_id = message_id
                await schedule_actions(post)

            return PublisherResult(success=True, published_at=published_at)

        except TelegramError as e:
            logger.warning('publish_attempt_failed', post_id=post.id, attempt=attempt, error=str(e))
            _increment_retry(post.id)
            if attempt < settings.max_retry_attempts:
                await asyncio.sleep(3600)

    _mark_failed(post.id)
    logger.error('post_failed_all_retries', post_id=post.id)
    return PublisherResult(success=False, error=f'Все {settings.max_retry_attempts} попытки исчерпаны')


# ── Publish poll ──────────────────────────────────────────────────────────────

async def publish_poll(topic: str) -> PublisherResult:
    poll = await generate_poll(topic, scheduled_at=datetime.now(timezone.utc))

    if settings.dry_run:
        logger.info('dry_run_poll', question=poll.question)
        return PublisherResult(success=True, published_at=datetime.now(timezone.utc))

    bot = get_bot()
    try:
        msg = await bot.send_poll(
            chat_id=settings.telegram_channel_id,
            question=poll.question,
            options=[opt.text for opt in poll.options],
            is_anonymous=True,
            allows_multiple_answers=False,
        )
        _mark_poll_published(poll.id, str(msg.poll.id))
        logger.info('poll_published', poll_id=poll.id)
        return PublisherResult(success=True, published_at=datetime.now(timezone.utc))

    except TelegramError as e:
        logger.error('poll_failed', poll_id=poll.id, error=str(e))
        return PublisherResult(success=False, error=str(e))


# ── DB helpers ────────────────────────────────────────────────────────────────

def _mark_published(
    post_id: str,
    message_id: int,
    published_at: datetime | None = None,
    image_skipped_reason: str | None = None,
) -> None:
    db = get_db()
    update: dict = {
        'status': 'published',
        'published_at': (published_at or datetime.now(timezone.utc)).isoformat(),
        'telegram_message_id': message_id,
    }
    if image_skipped_reason:
        update['image_skipped_reason'] = image_skipped_reason
    db.table('posts').update(update).eq('id', post_id).execute()


def mark_waiting_publish(post_id: str, publish_at: datetime) -> None:
    db = get_db()
    db.table('posts').update({
        'status': 'waiting_publish',
        'scheduled_at': publish_at.isoformat(),
    }).eq('id', post_id).execute()


def _mark_failed(post_id: str) -> None:
    db = get_db()
    db.table('posts').update({'status': 'failed'}).eq('id', post_id).execute()


def _mark_cancelled(post_id: str) -> None:
    db = get_db()
    db.table('posts').update({'status': 'cancelled'}).eq('id', post_id).execute()


def _increment_retry(post_id: str) -> None:
    db = get_db()
    rows = db.table('posts').select('retry_count').eq('id', post_id).execute().data
    if rows:
        db.table('posts').update({'retry_count': rows[0]['retry_count'] + 1}).eq('id', post_id).execute()


def _mark_poll_published(poll_id: str, telegram_poll_id: str) -> None:
    db = get_db()
    db.table('polls').update({
        'status': 'published',
        'published_at': datetime.now(timezone.utc).isoformat(),
        'telegram_poll_id': telegram_poll_id,
    }).eq('id', poll_id).execute()


def cancel_post(post_id: str) -> None:
    _mark_cancelled(post_id)


def save_post_to_db(post: Post) -> None:
    from src.generator.content_generator import save_post
    save_post(post)
