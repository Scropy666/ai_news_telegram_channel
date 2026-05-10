import json
import random
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = structlog.get_logger()

SCHEDULE_FILE = Path(__file__).parent.parent.parent / 'config' / 'schedule.json'

VALID_TYPES = {'news_digest', 'deep_dive', 'tool_spotlight', 'opinion', 'poll'}

TYPE_LABELS = {
    'news_digest':    '📰 Дайджест новостей',
    'deep_dive':      '🔬 Глубокий разбор',
    'tool_spotlight': '🛠 Обзор инструмента',
    'opinion':        '💬 Авторское мнение',
    'poll':           '📊 Опрос',
}

DAY_MAP = {
    'MON': 'mon', 'TUE': 'tue', 'WED': 'wed',
    'THU': 'thu', 'FRI': 'fri', 'SAT': 'sat', 'SUN': 'sun',
}

POLL_TOPICS = [
    'AI агенты vs традиционная разработка',
    'Лучший LLM провайдер',
    'Open source vs Closed source AI',
    'Автоматизация и рынок труда',
    'Следующий прорыв в AI',
]


# ── Schedule persistence ──────────────────────────────────────────────────────

def load_schedule() -> list[dict]:
    if SCHEDULE_FILE.exists():
        with open(SCHEDULE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f).get('jobs', [])
    return []


def save_schedule(jobs: list[dict]) -> None:
    with open(SCHEDULE_FILE, 'w', encoding='utf-8') as f:
        json.dump({'jobs': jobs}, f, ensure_ascii=False, indent=2)


def add_scheduled_job(post_type: str, time_str: str, days: str) -> str:
    jobs = load_schedule()
    job_id = f'j{uuid4().hex[:6]}'
    jobs.append({'id': job_id, 'type': post_type, 'time': time_str, 'days': days})
    save_schedule(jobs)
    return job_id


def remove_scheduled_job(job_id: str) -> bool:
    jobs = load_schedule()
    new_jobs = [j for j in jobs if j['id'] != job_id]
    if len(new_jobs) == len(jobs):
        return False
    save_schedule(new_jobs)
    return True


# ── Helpers ───────────────────────────────────────────────────────────────────

def _days_to_cron(days: str) -> str:
    if days == '*':
        return '*'
    return ','.join(DAY_MAP[d.strip().upper()] for d in days.split(',') if d.strip().upper() in DAY_MAP)


def _parse_time(time_str: str) -> tuple[int, int]:
    h, m = time_str.split(':')
    return int(h), int(m)


def random_poll_topic() -> str:
    return random.choice(POLL_TOPICS)


# ── Build & sync ──────────────────────────────────────────────────────────────

def _register_jobs(scheduler: AsyncIOScheduler, jobs: list[dict], pipeline_fn, poll_fn) -> None:
    scheduler.add_job(
        _scrape_job_wrapper,
        CronTrigger(hour='*/2'),
        id='scrape',
        replace_existing=True,
    )

    for job in jobs:
        h, m = _parse_time(job['time'])
        dow = _days_to_cron(job['days'])
        jtype = job['type']

        if jtype == 'poll':
            fn = poll_fn
            kwargs = {'topic': random_poll_topic()}
        else:
            fn = pipeline_fn
            kwargs = {'post_type': jtype}

        scheduler.add_job(
            fn,
            CronTrigger(day_of_week=dow, hour=h, minute=m),
            id=job['id'],
            kwargs=kwargs,
            replace_existing=True,
        )


async def _scrape_job_wrapper():
    from src.agents.scraper_agent import run as scraper_run
    from src.agents import coordinator
    result = await scraper_run()
    logger.info('scheduled_scrape_done', **result.model_dump())
    if result.errors:
        await coordinator.notify_admin(f'⚠️ Scraper errors: {result.errors}')


def build_scheduler(pipeline_fn, poll_fn) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone='Europe/Moscow')
    jobs = load_schedule()
    _register_jobs(scheduler, jobs, pipeline_fn, poll_fn)
    return scheduler


def sync_scheduler(scheduler: AsyncIOScheduler, pipeline_fn, poll_fn) -> None:
    jobs = load_schedule()
    existing_ids = {j.id for j in scheduler.get_jobs()}
    config_ids = {j['id'] for j in jobs} | {'scrape'}
    for old_id in existing_ids - config_ids:
        scheduler.remove_job(old_id)
    _register_jobs(scheduler, jobs, pipeline_fn, poll_fn)


def get_schedule_status(scheduler: AsyncIOScheduler) -> str:
    jobs = load_schedule()
    if not jobs:
        return '📅 Расписание пустое\n\nДобавь задачу:\n/add\_schedule news\_digest 09:00'
    lines = ['📅 *Расписание публикаций*\n']
    for job in jobs:
        label = TYPE_LABELS.get(job['type'], job['type'])
        days_str = 'каждый день' if job['days'] == '*' else job['days']
        apj = scheduler.get_job(job['id'])
        next_run = apj.next_run_time.strftime('%d.%m %H:%M') if apj and apj.next_run_time else '—'
        lines.append(f'`{job["id"]}` {label}\n⏰ {job["time"]} | {days_str} | след: {next_run}\n')
    lines.append('\n/add\_schedule <тип> <время> \[дни\] — добавить')
    lines.append('/remove\_schedule <id> — удалить')
    return '\n'.join(lines)
