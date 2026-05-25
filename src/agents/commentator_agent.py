import random
import structlog
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from telegram import ReactionTypeEmoji
from telegram.error import TelegramError

from src.config import settings
from src.database.client import get_db
from src.database.models import Post, CommentAction
from src.generator.prompt_registry import get_prompt
from src.utils import get_commentator_bot

logger = structlog.get_logger()

_scheduler = None

PERSONA_EMOJIS: dict[str, list[str]] = {
    'skeptic': ['🤔', '🤨', '😐'],
    'excited': ['🔥', '🤯', '🎉'],
    'curious': ['🤔', '👀'],
    'ironic':  ['😈', '🥱'],
    'neutral': ['👍', '❤️'],
    'expert':  ['💡', '📌'],
}

PERSONAS = list(PERSONA_EMOJIS.keys())


def set_scheduler(scheduler) -> None:
    global _scheduler
    _scheduler = scheduler


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pick_persona() -> str:
    return random.choice(PERSONAS)


def _pick_emoji(persona: str) -> str:
    return random.choice(PERSONA_EMOJIS[persona])


def _decide_actions() -> list[str]:
    """Rolls dice independently for reaction and comment. Returns non-empty list."""
    actions = []
    if random.random() < settings.commentator_react_probability:
        actions.append('reaction')
    if random.random() < settings.commentator_comment_probability:
        actions.append('comment')
    return actions or ['skipped']


def _humanize(text: str, persona: str = '') -> str:
    """Maybe strip trailing period; add light typos (skipped for expert persona)."""
    if not text:
        return text
    if text.endswith('.') and random.random() < 0.6:
        text = text[:-1]
    if persona == 'expert':
        return text
    words = text.split()
    result = []
    for word in words:
        if len(word) > 3 and random.random() < 0.1:
            i = random.randint(1, len(word) - 2)
            word = word[:i] + word[i + 1] + word[i] + word[i + 2:]
        result.append(word)
    return ' '.join(result)


async def _generate_comment(post_content: str, persona: str) -> str:
    from groq import AsyncGroq
    prompt_template, _ = get_prompt(f'commentator_{persona}')
    prompt = prompt_template.replace('{{POST}}', post_content[:1500])
    client = AsyncGroq(api_key=settings.groq_api_key)
    response = await client.chat.completions.create(
        model='llama-3.3-70b-versatile',
        messages=[{'role': 'user', 'content': prompt}],
        max_tokens=100,
        temperature=0.9,
    )
    return response.choices[0].message.content.strip()


async def _check_rate_limit() -> bool:
    """Returns True if comment is allowed (< 3 comments done in last hour)."""
    db = get_db()
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    rows = (
        db.table('comment_actions')
        .select('id')
        .eq('action_type', 'comment')
        .eq('status', 'done')
        .gte('executed_at', one_hour_ago)
        .execute()
        .data
    )
    return len(rows) < 3


# ── DB helpers ────────────────────────────────────────────────────────────────

def _save_action(action: CommentAction) -> None:
    db = get_db()
    db.table('comment_actions').insert({
        'id': action.id,
        'post_id': action.post_id,
        'channel_message_id': action.channel_message_id,
        'discussion_chat_id': action.discussion_chat_id,
        'discussion_message_id': action.discussion_message_id,
        'action_type': action.action_type,
        'emoji': action.emoji,
        'comment_text': action.comment_text,
        'persona': action.persona,
        'scheduled_at': action.scheduled_at.isoformat(),
        'executed_at': action.executed_at.isoformat() if action.executed_at else None,
        'status': action.status,
        'error': action.error,
    }).execute()


def _update_action(action_id: str, **kwargs) -> None:
    db = get_db()
    if 'executed_at' in kwargs and isinstance(kwargs['executed_at'], datetime):
        kwargs['executed_at'] = kwargs['executed_at'].isoformat()
    db.table('comment_actions').update(kwargs).eq('id', action_id).execute()


def _get_post_content(post_id: str) -> str:
    db = get_db()
    rows = db.table('posts').select('content').eq('id', post_id).execute().data
    return rows[0]['content'] if rows else ''


def _schedule_job(action_id: str, run_at: datetime, job_id_prefix: str = 'comment') -> None:
    if not _scheduler:
        return
    from apscheduler.triggers.date import DateTrigger
    _scheduler.add_job(
        execute_action,
        DateTrigger(run_date=run_at),
        args=[action_id],
        id=f'{job_id_prefix}_{action_id}',
        replace_existing=True,
    )


# ── Core API ──────────────────────────────────────────────────────────────────

async def schedule_actions(post: Post) -> list[CommentAction]:
    """Create commentator actions for a freshly published post."""
    if not settings.commentator_enabled:
        return []
    if not get_commentator_bot():
        logger.info('commentator_skipped_no_token')
        return []
    if not post.telegram_message_id:
        logger.warning('commentator_no_message_id', post_id=post.id)
        return []

    action_types = _decide_actions()
    created: list[CommentAction] = []

    if action_types == ['skipped']:
        logger.info('commentator_skipped', post_id=post.id)
        return []

    for atype in action_types:
        delay_s = random.uniform(settings.commentator_min_delay_s, settings.commentator_max_delay_s)
        run_at = datetime.now(timezone.utc) + timedelta(seconds=delay_s)
        persona = _pick_persona()
        emoji = _pick_emoji(persona)

        action = CommentAction(
            id=str(uuid4()),
            post_id=post.id,
            channel_message_id=post.telegram_message_id,
            action_type=atype,
            emoji=emoji,
            persona=persona,
            scheduled_at=run_at,
            status='scheduled',
        )
        _save_action(action)
        _schedule_job(action.id, run_at)

        created.append(action)
        logger.info('commentator_scheduled', action_id=action.id, type=atype, delay_s=int(delay_s))

    return created


async def execute_action(action_id: str) -> None:
    """Execute a single commentator action. Called by APScheduler."""
    db = get_db()
    rows = db.table('comment_actions').select('*').eq('id', action_id).execute().data
    if not rows:
        logger.warning('commentator_action_not_found', action_id=action_id)
        return

    action = CommentAction(**rows[0])
    if action.status in ('done', 'failed'):
        return

    bot = get_commentator_bot()
    if not bot:
        _update_action(action_id, status='failed', error='no_commentator_bot')
        return

    if action.action_type == 'reaction':
        try:
            await bot.set_message_reaction(
                chat_id=settings.telegram_channel_id,
                message_id=action.channel_message_id,
                reaction=[ReactionTypeEmoji(emoji=action.emoji)],
            )
            _update_action(action_id, status='done', executed_at=datetime.now(timezone.utc))
            logger.info('commentator_reaction_sent', action_id=action_id, emoji=action.emoji)
        except TelegramError as e:
            _update_action(action_id, status='failed', error=str(e))
            logger.error('commentator_reaction_failed', action_id=action_id, error=str(e))

    elif action.action_type == 'comment':
        if not action.discussion_message_id:
            # Forward hasn't arrived yet — retry in 5 minutes
            _update_action(action_id, status='awaiting_forward')
            _schedule_job(action_id, datetime.now(timezone.utc) + timedelta(minutes=5), 'comment_retry')
            logger.info('commentator_awaiting_forward', action_id=action_id)
            return

        if not await _check_rate_limit():
            # Rate limited — retry in 30 minutes
            _schedule_job(action_id, datetime.now(timezone.utc) + timedelta(minutes=30), 'comment_ratelimit')
            logger.info('commentator_rate_limited', action_id=action_id)
            return

        post_content = _get_post_content(action.post_id)
        try:
            raw_comment = await _generate_comment(post_content, action.persona)
            comment = _humanize(raw_comment, action.persona or '')
            await bot.send_message(
                chat_id=action.discussion_chat_id,
                text=comment,
                reply_to_message_id=action.discussion_message_id,
            )
            _update_action(
                action_id,
                status='done',
                executed_at=datetime.now(timezone.utc),
                comment_text=comment,
            )
            logger.info('commentator_comment_sent', action_id=action_id, persona=action.persona)
        except TelegramError as e:
            _update_action(action_id, status='failed', error=str(e))
            logger.error('commentator_comment_failed', action_id=action_id, error=str(e))


async def on_discussion_forward(channel_msg_id: int, discussion_chat_id: int, discussion_msg_id: int) -> None:
    """Called when an auto-forward from the channel arrives in the discussion group."""
    db = get_db()
    rows = (
        db.table('comment_actions')
        .select('id, action_type, status')
        .eq('channel_message_id', channel_msg_id)
        .execute()
        .data
    )
    # Filter to scheduled/awaiting_forward comment actions
    targets = [r for r in rows if r['action_type'] == 'comment' and r['status'] in ('scheduled', 'awaiting_forward')]
    for row in targets:
        _update_action(row['id'], discussion_chat_id=discussion_chat_id, discussion_message_id=discussion_msg_id)
        # If already waiting on forward, kick off execution immediately
        if row['status'] == 'awaiting_forward':
            _schedule_job(row['id'], datetime.now(timezone.utc) + timedelta(seconds=5), 'comment_forward_ready')

    if targets:
        logger.info('commentator_forward_matched', channel_msg_id=channel_msg_id, updated=len(targets))


async def force_comment(post_id: str) -> dict:
    """Immediately send reaction + comment for a post. Used by /comment command."""
    db = get_db()

    post_rows = db.table('posts').select('*').eq('id', post_id).execute().data
    if not post_rows:
        return {'ok': False, 'error': f'Пост {post_id} не найден в БД'}

    from src.database.models import Post as PostModel
    post = PostModel(**post_rows[0])
    if not post.telegram_message_id:
        return {'ok': False, 'error': 'Пост ещё не опубликован (нет telegram_message_id)'}

    bot = get_commentator_bot()
    if not bot:
        return {'ok': False, 'error': 'COMMENTATOR_BOT_TOKEN не задан'}

    persona = _pick_persona()
    emoji = _pick_emoji(persona)
    result: dict = {'persona': persona, 'emoji': emoji}

    # Reaction
    try:
        await bot.set_message_reaction(
            chat_id=settings.telegram_channel_id,
            message_id=post.telegram_message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
        result['reaction'] = emoji
        logger.info('force_comment_reaction_sent', post_id=post_id, emoji=emoji)
    except TelegramError as e:
        result['reaction_error'] = str(e)
        logger.error('force_comment_reaction_failed', post_id=post_id, error=str(e))

    # Find discussion_message_id from any existing comment_action for this post
    action_rows = (
        db.table('comment_actions')
        .select('discussion_chat_id, discussion_message_id')
        .eq('post_id', post_id)
        .execute()
        .data
    )
    linked = next(
        (r for r in action_rows if r.get('discussion_message_id') is not None),
        None,
    )

    if not linked:
        result['comment_error'] = 'discussion_message_id не найден (форвард ещё не пришёл?)'
        return result

    post_content = _get_post_content(post_id)
    try:
        raw_comment = await _generate_comment(post_content, persona)
        comment = _humanize(raw_comment, persona)
        await bot.send_message(
            chat_id=linked['discussion_chat_id'],
            text=comment,
            reply_to_message_id=linked['discussion_message_id'],
        )
        result['comment'] = comment
        logger.info('force_comment_sent', post_id=post_id, persona=persona)
    except TelegramError as e:
        result['comment_error'] = str(e)
        logger.error('force_comment_failed', post_id=post_id, error=str(e))

    return result


async def restore_pending_actions(scheduler) -> None:
    """Called at startup: register APScheduler jobs for pending actions."""
    set_scheduler(scheduler)

    db = get_db()
    now = datetime.now(timezone.utc)

    # Mark stale awaiting_forward (>30 min old) as failed
    stale_before = (now - timedelta(minutes=30)).isoformat()
    stale_rows = (
        db.table('comment_actions')
        .select('id')
        .eq('status', 'awaiting_forward')
        .lt('scheduled_at', stale_before)
        .execute()
        .data
    )
    for row in stale_rows:
        _update_action(row['id'], status='failed', error='forward_never_arrived')

    # Restore scheduled actions
    scheduled_rows = (
        db.table('comment_actions')
        .select('*')
        .eq('status', 'scheduled')
        .execute()
        .data
    )

    from apscheduler.triggers.date import DateTrigger
    restored = 0
    for row in scheduled_rows:
        action = CommentAction(**row)
        run_at = action.scheduled_at
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)

        if run_at <= now:
            # Overdue — stagger with random delay to avoid simultaneous bursts
            run_at = now + timedelta(seconds=random.uniform(60, 180))

        scheduler.add_job(
            execute_action,
            DateTrigger(run_date=run_at),
            args=[action.id],
            id=f'comment_{action.id}',
            replace_existing=True,
        )
        restored += 1

    logger.info('commentator_restored', restored=restored, stale_failed=len(stale_rows))
