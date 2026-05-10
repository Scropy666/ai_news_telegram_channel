from datetime import datetime
from uuid import uuid4
from groq import AsyncGroq
import structlog

from src.config import settings
from src.database.models import Poll, PollOption
from src.database.client import get_db
from src.generator.prompt_registry import get_prompt

logger = structlog.get_logger()

client = AsyncGroq(api_key=settings.groq_api_key)
MODEL = 'llama-3.3-70b-versatile'

POLL_MAX_QUESTION_LEN = 300
POLL_MAX_OPTION_LEN = 100
POLL_MIN_OPTIONS = 2
POLL_MAX_OPTIONS = 10


async def generate_poll(topic: str, scheduled_at: datetime) -> Poll:
    prompt_template, _ = get_prompt('poll_generator')
    full_prompt = prompt_template.replace('{{TOPIC}}', topic)

    response = await client.chat.completions.create(
        model=MODEL,
        messages=[{'role': 'user', 'content': full_prompt}],
        max_tokens=256,
        temperature=0.9,
    )

    raw = response.choices[0].message.content.strip()
    question, options = _parse_poll_response(raw)

    poll = Poll(
        id=str(uuid4()),
        question=question[:POLL_MAX_QUESTION_LEN],
        options=[PollOption(text=o[:POLL_MAX_OPTION_LEN]) for o in options],
        scheduled_at=scheduled_at,
        status='pending',
        topic=topic,
    )

    _save_poll(poll)
    logger.info('poll_generated', poll_id=poll.id, question=question)
    return poll


def _parse_poll_response(raw: str) -> tuple[str, list[str]]:
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    question = lines[0].lstrip('Q: ').strip()
    options = []
    for line in lines[1:]:
        opt = line.lstrip('0123456789.-) ').strip()
        if opt and len(options) < POLL_MAX_OPTIONS:
            options.append(opt)
    if len(options) < POLL_MIN_OPTIONS:
        options = ['Да', 'Нет', 'Не определился']
    return question, options


def _save_poll(poll: Poll) -> None:
    db = get_db()
    row = poll.model_dump()
    row['scheduled_at'] = row['scheduled_at'].isoformat()
    if row.get('published_at'):
        row['published_at'] = row['published_at'].isoformat()
    row['options'] = [o if isinstance(o, dict) else o.model_dump() for o in poll.options]
    db.table('polls').insert(row).execute()
