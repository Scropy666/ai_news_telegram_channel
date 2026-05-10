import structlog
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, MessageOriginChannel
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

from src.config import settings
from src.agents.coordinator import notify_admin, run_pipeline, run_poll_pipeline
from src.agents.scraper_agent import (
    run as scraper_run, load_topics, add_topic, remove_topic, scrape_by_tags,
)
from src.agents.analyzer_agent import run_multi
from src.agents.publisher_agent import publish_post, cancel_post, save_post_to_db, mark_waiting_publish
from src.agents import tester_agent
from src.scheduler.scheduler import (
    build_scheduler, sync_scheduler, get_schedule_status,
    add_scheduled_job, remove_scheduled_job,
    VALID_TYPES, DAY_MAP, TYPE_LABELS,
)
from src.database.client import get_db

structlog.configure(processors=[
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt='iso'),
    structlog.dev.ConsoleRenderer(),
])
logger = structlog.get_logger()

BOT_COMMANDS = [
    BotCommand('start',           'Показать все команды'),
    BotCommand('status',          'Статистика агента'),
    BotCommand('schedule',        'Текущее расписание публикаций'),
    BotCommand('add_schedule',    'Добавить задачу: <тип> <время> [дни]'),
    BotCommand('remove_schedule', 'Удалить задачу по ID'),
    BotCommand('topics',          'Список поисковых тегов'),
    BotCommand('add_topic',       'Добавить тег для парсинга'),
    BotCommand('remove_topic',    'Удалить тег по номеру'),
    BotCommand('scrape',          'Запустить парсинг новостей'),
    BotCommand('post',            'Запустить полный pipeline (scrape→analyze→approve)'),
    BotCommand('poll',            'Опубликовать опрос прямо сейчас'),
    BotCommand('cancel',          'Отменить пост по ID'),
    BotCommand('watch_post',      'Показать пост из БД по ID'),
    BotCommand('publish',         'Опубликовать пост: <post_id> [HH:MM]'),
    BotCommand('test',                 'Запустить тесты вручную'),
    BotCommand('commentator_status',   'Статистика комментатора за 7 дней'),
    BotCommand('commentator_test',     'Форс-запуск комментатора: <post_id>'),
]


def is_admin(update: Update) -> bool:
    return str(update.effective_chat.id) == settings.telegram_admin_chat_id


# ── Lifecycle ────────────────────────────────────────────────────────────────

async def _restore_scheduled_posts(scheduler) -> None:
    """При старте пересоздаём DateTrigger-задачи для всех waiting_publish постов."""
    from src.database.models import Post as PostModel
    from apscheduler.triggers.date import DateTrigger

    db = get_db()
    rows = db.table('posts').select('*').eq('status', 'waiting_publish').execute().data
    if not rows:
        return

    now = datetime.now(timezone.utc)
    restored = 0
    for row in rows:
        post = PostModel(**row)
        run_at = post.scheduled_at.replace(tzinfo=timezone.utc) if post.scheduled_at.tzinfo is None else post.scheduled_at

        if run_at <= now:
            # Время уже прошло — публикуем немедленно
            import asyncio
            asyncio.create_task(publish_post(post))
        else:
            scheduler.add_job(
                publish_post,
                DateTrigger(run_date=run_at),
                args=[post],
                id=f'delayed_{post.id}',
                replace_existing=True,
            )
        restored += 1

    logger.info('scheduled_posts_restored', count=restored)


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(BOT_COMMANDS)
    app.bot_data.setdefault('drafts', {})

    # Замыкание: планировщик передаёт drafts из bot_data при каждом вызове
    async def scheduled_pipeline(post_type: str):
        await run_pipeline(post_type, drafts=app.bot_data['drafts'])

    scheduler = build_scheduler(
        pipeline_fn=scheduled_pipeline,
        poll_fn=run_poll_pipeline,
    )
    scheduler.start()
    app.bot_data['scheduler'] = scheduler
    logger.info('scheduler_started')

    # Восстанавливаем отложенные посты после перезапуска
    await _restore_scheduled_posts(scheduler)

    # Восстанавливаем запланированные действия комментатора
    from src.agents.commentator_agent import restore_pending_actions
    await restore_pending_actions(scheduler)

    await notify_admin('✅ AI News Agent запущен')

    # Автотест при каждом старте
    report = await tester_agent.run_suite()
    await notify_admin(report.summary())


async def post_shutdown(app: Application) -> None:
    scheduler = app.bot_data.get('scheduler')
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
    logger.info('agent_stopped')


# ── /start ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text(
        '🤖 *AI News Agent*\n\n'
        '*Контент:*\n'
        '/post — scrape → analyze → approve\n'
        '/poll — опубликовать опрос сейчас\n'
        '/scrape — только парсинг\n'
        '/cancel <post\_id> — отменить пост\n\n'
        '*Расписание:*\n'
        '/schedule — текущее расписание\n'
        '/add\_schedule <тип> <время> \[дни\]\n'
        '/remove\_schedule <id>\n\n'
        '*Теги парсинга:*\n'
        '/topics — список тегов\n'
        '/add\_topic <запрос>\n'
        '/remove\_topic <номер>\n\n'
        '*Система:*\n'
        '/status — статистика\n'
        '/test — запустить тесты\n\n'
        f'*Типы постов:* {", ".join(t.replace("_", chr(92) + "_") for t in VALID_TYPES if t != "poll")}',
        parse_mode='Markdown',
    )


# ── /status ──────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    db = get_db()
    pending = db.table('posts').select('id').eq('status', 'pending').execute().data
    published_today = (
        db.table('posts').select('id')
        .eq('status', 'published')
        .gte('published_at', datetime.now(timezone.utc).date().isoformat())
        .execute().data
    )
    scheduler = ctx.bot_data.get('scheduler')
    jobs_count = len(scheduler.get_jobs()) if scheduler else 0
    await update.message.reply_text(
        f'📊 *Статус агента*\n\n'
        f'В очереди: {len(pending)} постов\n'
        f'Опубликовано сегодня: {len(published_today)}\n'
        f'Активных задач: {jobs_count}\n'
        f'Dry run: {settings.dry_run}',
        parse_mode='Markdown',
    )


# ── /schedule ────────────────────────────────────────────────────────────────

async def cmd_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    scheduler = ctx.bot_data.get('scheduler')
    await update.message.reply_text(get_schedule_status(scheduler), parse_mode='Markdown')


async def cmd_add_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    usage = (
        'Использование: /add\_schedule <тип> <время> \[дни\]\n\n'
        'Типы: ' + ', '.join(VALID_TYPES) + '\n'
        'Время: 09:00 | Дни: MON,WED,FRI или \*'
    )
    if len(ctx.args) < 2:
        await update.message.reply_text(usage, parse_mode='Markdown')
        return

    post_type, time_str = ctx.args[0].lower(), ctx.args[1]
    days = ctx.args[2].upper() if len(ctx.args) >= 3 else '*'

    if post_type not in VALID_TYPES:
        await update.message.reply_text(f'❌ Неверный тип: `{post_type}`', parse_mode='Markdown')
        return
    try:
        h, m = time_str.split(':')
        assert 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    except Exception:
        await update.message.reply_text('❌ Неверный формат времени. Пример: `09:00`', parse_mode='Markdown')
        return

    job_id = add_scheduled_job(post_type, time_str, days)
    scheduler = ctx.bot_data.get('scheduler')
    if scheduler:
        sync_scheduler(scheduler, run_pipeline, run_poll_pipeline)

    days_str = 'каждый день' if days == '*' else days
    await update.message.reply_text(
        f'✅ Добавлено: {TYPE_LABELS.get(post_type, post_type)}\n'
        f'⏰ {time_str} | {days_str}\nID: `{job_id}`',
        parse_mode='Markdown',
    )


async def cmd_remove_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not ctx.args:
        await update.message.reply_text('Использование: /remove\_schedule <id>', parse_mode='Markdown')
        return
    job_id = ctx.args[0]
    if remove_scheduled_job(job_id):
        scheduler = ctx.bot_data.get('scheduler')
        if scheduler:
            sync_scheduler(scheduler, run_pipeline, run_poll_pipeline)
        await update.message.reply_text(f'✅ Задача `{job_id}` удалена', parse_mode='Markdown')
    else:
        await update.message.reply_text(f'❌ Задача `{job_id}` не найдена', parse_mode='Markdown')


# ── /topics ──────────────────────────────────────────────────────────────────

async def cmd_topics(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    queries = load_topics()
    if not queries:
        await update.message.reply_text('Список пуст. Добавь: /add\_topic OpenAI', parse_mode='Markdown')
        return
    source_icons = {'hackernews': 'HN', 'reddit': 'RD', 'devto': 'DV'}
    lines = ['🏷 *Теги для парсинга:*\n']
    for i, q in enumerate(queries, 1):
        srcs = q.get('sources', ['hackernews'])
        src_str = ' '.join(f'`{source_icons.get(s, s)}`' for s in srcs)
        lines.append(f'`{i}.` {q["query"]} — {src_str}')
    lines.append('\n/add\_topic <запрос> — добавить\n/remove\_topic <номер> — удалить')
    await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')


async def cmd_add_topic(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not ctx.args:
        await update.message.reply_text(
            'Использование: /add\_topic <поисковый запрос>\nПример: /add\_topic Claude AI news',
            parse_mode='Markdown',
        )
        return
    entry = add_topic(' '.join(ctx.args))
    if entry is None:
        await update.message.reply_text(
            f'⚠️ Тег `{" ".join(ctx.args)}` уже существует\nПосмотреть список: /topics',
            parse_mode='Markdown',
        )
        return
    await update.message.reply_text(
        f'✅ Добавлен тег: `{entry["query"]}`\nКатегория: `{entry["topic"]}`',
        parse_mode='Markdown',
    )


async def cmd_remove_topic(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text(
            'Использование: /remove\_topic <номер>\nПосмотреть номера: /topics',
            parse_mode='Markdown',
        )
        return
    index = int(ctx.args[0])
    if remove_topic(index):
        await update.message.reply_text(f'✅ Тег №{index} удалён')
    else:
        await update.message.reply_text(f'❌ Тег №{index} не найден. Проверь: /topics')


# ── /post /poll /scrape /cancel /test ────────────────────────────────────────

async def cmd_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    args = ctx.args or []

    # Первый аргумент может быть post_type, остальные — теги
    # Форматы:
    #   /post                         → news_digest, topics.json
    #   /post deep_dive               → deep_dive, topics.json
    #   /post OpenAI Claude           → news_digest, теги: OpenAI Claude
    #   /post deep_dive OpenAI Claude → deep_dive, теги: OpenAI Claude
    if args and args[0].lower() in VALID_TYPES:
        post_type = args[0].lower()
        tags = args[1:] if len(args) > 1 else None
    else:
        post_type = 'news_digest'
        tags = args if args else None

    tags_info = f' по тегам: {", ".join(tags)}' if tags else ''
    await update.message.reply_text(f'⏳ Запускаю pipeline ({post_type}){tags_info}...')
    drafts = ctx.bot_data.setdefault('drafts', {})
    await run_pipeline(post_type, drafts=drafts, tags=tags)


async def cmd_poll(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    from src.scheduler.scheduler import random_poll_topic
    topic = random_poll_topic()
    await update.message.reply_text(f'⏳ Генерирую опрос: {topic}...')
    await run_poll_pipeline(topic)


async def cmd_scrape(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text('⏳ Запускаю парсинг...')
    result = await scraper_run()
    await update.message.reply_text(
        f'✅ Парсинг завершён\nПолучено: {result.total_fetched} | Сохранено: {result.new_saved}'
        + (f'\n⚠️ Ошибки: {len(result.errors)}' if result.errors else '')
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not ctx.args:
        await update.message.reply_text('Укажите post\_id: /cancel <id>', parse_mode='Markdown')
        return
    cancel_post(ctx.args[0])
    await update.message.reply_text(f'✅ Пост `{ctx.args[0]}` отменён', parse_mode='Markdown')


async def cmd_watch_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not ctx.args:
        await update.message.reply_text('Использование: /watch\_post <post\_id>', parse_mode='Markdown')
        return
    post_id = ctx.args[0]
    db = get_db()
    rows = db.table('posts').select('*').eq('id', post_id).execute().data
    if not rows:
        await update.message.reply_text(f'❌ Пост `{post_id}` не найден в базе.', parse_mode='Markdown')
        return
    from src.database.models import Post
    post = Post(**rows[0])
    label = TYPE_LABELS.get(post.type, post.type)
    status_icons = {'pending': '⏳', 'published': '✅', 'failed': '❌', 'cancelled': '🗑'}
    icon = status_icons.get(post.status, '❓')
    header = (
        f'{icon} *{label}* | `{post.status}`\n'
        f'ID: `{post.id}`\n'
        f'Создан: {post.scheduled_at.strftime("%d.%m %H:%M") if post.scheduled_at else "—"}\n'
        f'Опубликован: {post.published_at.strftime("%d.%m %H:%M") if post.published_at else "—"}\n\n'
    )
    text = header + post.content
    if len(text) > 4096:
        text = text[:4090] + '...'
    await update.message.reply_text(text, parse_mode='Markdown')


async def cmd_publish(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not ctx.args:
        await update.message.reply_text(
            'Использование: /publish <post\_id> \[HH:MM\]\n\nПримеры:\n'
            '/publish abc-123 — опубликовать сейчас\n'
            '/publish abc-123 15:00 — опубликовать в 15:00',
            parse_mode='Markdown',
        )
        return

    post_id = ctx.args[0]
    time_str = ctx.args[1] if len(ctx.args) >= 2 else None

    db = get_db()
    rows = db.table('posts').select('*').eq('id', post_id).execute().data
    if not rows:
        await update.message.reply_text(f'❌ Пост `{post_id}` не найден.', parse_mode='Markdown')
        return

    from src.database.models import Post
    post = Post(**rows[0])

    if time_str is None:
        # Публикуем сейчас
        await update.message.reply_text('⏳ Публикую...')
        result = await publish_post(post)
        if result.success:
            await update.message.reply_text('✅ Опубликовано!')
        else:
            await update.message.reply_text(f'❌ Ошибка: {result.error}')
    else:
        # Публикуем в указанное время (Europe/Moscow)
        try:
            from zoneinfo import ZoneInfo
            from apscheduler.triggers.date import DateTrigger
            h, m = time_str.split(':')
            assert 0 <= int(h) <= 23 and 0 <= int(m) <= 59
        except Exception:
            await update.message.reply_text('❌ Неверный формат времени. Пример: `15:00`', parse_mode='Markdown')
            return

        tz = ZoneInfo('Europe/Moscow')
        now = datetime.now(tz)
        run_at = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        if run_at <= now:
            # Время уже прошло сегодня — планируем на завтра
            from datetime import timedelta
            run_at += timedelta(days=1)

        scheduler = ctx.bot_data.get('scheduler')
        if scheduler:
            scheduler.add_job(
                publish_post,
                DateTrigger(run_date=run_at),
                args=[post],
                id=f'manual_{post_id}',
                replace_existing=True,
            )
        mark_waiting_publish(post_id, publish_at=run_at)
        await update.message.reply_text(
            f'⏰ Запланировано на {run_at.strftime("%d.%m в %H:%M")} (МСК)',
        )


async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    mode = ctx.args[0].lower() if ctx.args else 'unit'
    if mode == 'e2e':
        await update.message.reply_text(
            '🧪 Запускаю e2e тест (scrape → generate → schedule + cancel)...\n'
            '⏳ Занимает 20–60 секунд из-за LLM-вызовов.'
        )
        report = await tester_agent.run_e2e()
    else:
        await update.message.reply_text('🧪 Запускаю unit-тесты...')
        report = await tester_agent.run_suite()
    await update.message.reply_text(report.summary())


# ── Commentator commands ──────────────────────────────────────────────────────

async def cmd_commentator_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    db = get_db()
    seven_days_ago = (datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                      - __import__('datetime').timedelta(days=7)).isoformat()

    all_rows = db.table('comment_actions').select('action_type, status, executed_at').gte('scheduled_at', seven_days_ago).execute().data

    counts: dict[str, int] = {}
    for row in all_rows:
        key = row['status']
        counts[key] = counts.get(key, 0) + 1

    reactions_done = sum(1 for r in all_rows if r['action_type'] == 'reaction' and r['status'] == 'done')
    comments_done  = sum(1 for r in all_rows if r['action_type'] == 'comment'  and r['status'] == 'done')

    text = (
        f'📊 Комментатор — статистика за 7 дней\n\n'
        f'✅ Выполнено: {counts.get("done", 0)}\n'
        f'⏳ Запланировано: {counts.get("scheduled", 0)}\n'
        f'⌛ Ожидает форварда: {counts.get("awaiting_forward", 0)}\n'
        f'❌ Ошибки: {counts.get("failed", 0)}\n'
        f'⏭ Пропущено: {counts.get("skipped", 0) + sum(1 for r in all_rows if r["action_type"] == "skipped")}\n\n'
        f'Реакций: {reactions_done} | Комментариев: {comments_done}'
    )
    await update.message.reply_text(text)


async def cmd_commentator_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not ctx.args:
        await update.message.reply_text('Использование: /commentator_test <post_id>')
        return

    post_id = ctx.args[0]
    db = get_db()
    rows = (
        db.table('comment_actions')
        .select('id, action_type, status')
        .eq('post_id', post_id)
        .in_('status', ['scheduled', 'awaiting_forward'])
        .execute()
        .data
    )
    if not rows:
        await update.message.reply_text(f'⚠️ Нет активных действий для поста {post_id}')
        return

    from src.agents.commentator_agent import execute_action
    import asyncio
    await update.message.reply_text(f'🧪 Запускаю {len(rows)} действий для поста {post_id}...')
    for row in rows:
        asyncio.create_task(execute_action(row['id']))
    await update.message.reply_text('✅ Задачи запущены. Проверь логи.')


# ── Discussion group auto-forward handler ─────────────────────────────────────

async def handle_auto_forward(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Catches channel posts auto-forwarded to the discussion group."""
    msg = update.message
    if not msg:
        return

    channel_msg_id: int | None = None
    if msg.forward_origin and isinstance(msg.forward_origin, MessageOriginChannel):
        channel_msg_id = msg.forward_origin.message_id
    elif msg.forward_from_message_id:
        channel_msg_id = msg.forward_from_message_id

    if not channel_msg_id:
        return

    from src.agents.commentator_agent import on_discussion_forward
    await on_discussion_forward(channel_msg_id, msg.chat.id, msg.message_id)


# ── Post action callbacks ─────────────────────────────────────────────────────

# Задержки публикации по коду
DELAY_MINUTES = {'pn': 0, 'p30': 30, 'p1h': 60, 'p4h': 240, 'p8h': 480}


async def _edit_query_message(query, text: str) -> None:
    """Edit text or caption depending on whether the message contains a photo."""
    if query.message.photo:
        await query.edit_message_caption(caption=text[:1024])
    else:
        await query.edit_message_text(text)


async def callback_post_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if str(query.from_user.id) != settings.telegram_admin_chat_id:
        return

    # Формат: {code}_{post_id}, где post_id — UUID (36 символов)
    data = query.data
    # Разбиваем по первому _ получая code и post_id
    parts = data.split('_', 1)
    if len(parts) != 2:
        return
    code, post_id = parts

    # Драфты хранятся в памяти бота: bot_data['drafts'][post_id] = Post
    drafts: dict = ctx.bot_data.setdefault('drafts', {})
    post = drafts.get(post_id)

    if post is None:
        await _edit_query_message(query, '❌ Пост не найден. Возможно, бот был перезапущен.')
        return

    if code == 'rj':
        drafts.pop(post_id, None)
        await _edit_query_message(query, '🗑 Пост отклонён и удалён.')
        return

    if code == 'sv':
        save_post_to_db(post)
        drafts.pop(post_id, None)
        await _edit_query_message(query, '💾 Пост сохранён. Опубликуй позже через /cancel или найди в Supabase.')
        return

    # Публикация (немедленно или отложенно)
    delay = DELAY_MINUTES.get(code, 0)
    save_post_to_db(post)
    drafts.pop(post_id, None)

    if delay == 0:
        await _edit_query_message(query, '⏳ Публикую...')
        result = await publish_post(post)
        if result.success:
            await _edit_query_message(query, f'✅ Опубликовано!\n\n{post.content[:400]}')
        else:
            await _edit_query_message(query, f'❌ Ошибка публикации: {result.error}')
    else:
        from datetime import timedelta
        from apscheduler.triggers.date import DateTrigger

        run_at = datetime.now(timezone.utc) + timedelta(minutes=delay)
        scheduler = ctx.bot_data.get('scheduler')
        if scheduler:
            scheduler.add_job(
                publish_post,
                DateTrigger(run_date=run_at),
                args=[post],
                id=f'delayed_{post_id}',
                replace_existing=True,
            )
        mark_waiting_publish(post_id, publish_at=run_at)
        hours = delay // 60
        mins = delay % 60
        time_label = f'{hours}ч' if mins == 0 else f'{delay} мин'
        await _edit_query_message(
            query,
            f'⏰ Запланировано через {time_label}\n'
            f'Публикация в {run_at.strftime("%H:%M")} UTC'
        )


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    logger.info('agent_starting')

    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler('start',           cmd_start))
    app.add_handler(CommandHandler('status',          cmd_status))
    app.add_handler(CommandHandler('schedule',        cmd_schedule))
    app.add_handler(CommandHandler('add_schedule',    cmd_add_schedule))
    app.add_handler(CommandHandler('remove_schedule', cmd_remove_schedule))
    app.add_handler(CommandHandler('topics',          cmd_topics))
    app.add_handler(CommandHandler('add_topic',       cmd_add_topic))
    app.add_handler(CommandHandler('remove_topic',    cmd_remove_topic))
    app.add_handler(CommandHandler('post',            cmd_post))
    app.add_handler(CommandHandler('poll',            cmd_poll))
    app.add_handler(CommandHandler('scrape',          cmd_scrape))
    app.add_handler(CommandHandler('cancel',          cmd_cancel))
    app.add_handler(CommandHandler('watch_post',      cmd_watch_post))
    app.add_handler(CommandHandler('publish',         cmd_publish))
    app.add_handler(CommandHandler('test',               cmd_test))
    app.add_handler(CommandHandler('commentator_status', cmd_commentator_status))
    app.add_handler(CommandHandler('commentator_test',   cmd_commentator_test))
    app.add_handler(MessageHandler(filters.IS_AUTOMATIC_FORWARD, handle_auto_forward))
    app.add_handler(CallbackQueryHandler(
        callback_post_action,
        pattern='^(pn|p30|p1h|p4h|p8h|sv|rj)_',
    ))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
