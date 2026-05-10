import hashlib
import io
from urllib.parse import quote

import httpx
import structlog
from groq import AsyncGroq

from src.config import settings
from src.generator.prompt_registry import get_prompt

logger = structlog.get_logger()

_groq = AsyncGroq(api_key=settings.groq_api_key)
_MODEL = 'llama-3.3-70b-versatile'


def make_seed(post_id: str) -> int:
    return int(hashlib.md5(post_id.encode()).hexdigest(), 16) % (2 ** 31)


def build_image_url(prompt: str, seed: int) -> str:
    encoded = quote(prompt, safe='')
    return (
        f'{settings.pollinations_base_url}/{encoded}'
        f'?width={settings.pollinations_width}'
        f'&height={settings.pollinations_height}'
        f'&nologo=true&seed={seed}'
    )


async def generate_image_prompt(post_content: str) -> str:
    prompt_template, _ = get_prompt('image_prompt')
    full_prompt = prompt_template.replace('{{POST_CONTENT}}', post_content[:1200])
    try:
        response = await _groq.chat.completions.create(
            model=_MODEL,
            messages=[{'role': 'user', 'content': full_prompt}],
            max_tokens=80,
            temperature=0.7,
        )
        result = response.choices[0].message.content.strip().strip('"')
        logger.info('image_prompt_generated', prompt=result[:80])
        return result
    except Exception as e:
        logger.warning('image_prompt_failed', error=str(e))
        return 'minimalist flat illustration, artificial intelligence network, soft pastel colors, dark background'


async def fetch_image_bytes(prompt: str, seed: int) -> bytes | None:
    url = build_image_url(prompt, seed)
    for attempt in range(1, 3):
        try:
            async with httpx.AsyncClient(timeout=settings.pollinations_timeout_s) as client:
                r = await client.get(url, follow_redirects=True)
                r.raise_for_status()
                if 'image' in r.headers.get('content-type', ''):
                    logger.info('pollinations_ok', seed=seed)
                    return r.content
                logger.warning('pollinations_bad_content_type', ct=r.headers.get('content-type'))
        except Exception as e:
            logger.warning('pollinations_attempt_failed', attempt=attempt, error=str(e))
    logger.warning('pollinations_all_failed', prompt=prompt[:60])
    return None


def make_fallback_image(title: str) -> bytes:
    """Генерировать минималистичную текстовую карточку через Pillow."""
    from PIL import Image, ImageDraw, ImageFont

    w, h = settings.pollinations_width, settings.pollinations_height
    img = Image.new('RGB', (w, h), color='#0d1117')
    draw = ImageDraw.Draw(img)

    # Акцентная полоса
    draw.rectangle([(64, h // 2 - 72), (w - 64, h // 2 - 68)], fill='#58a6ff')

    # Шрифт — пробуем системные, фоллбек на дефолтный
    font_size = 34
    font = _load_font(font_size)
    small_font = _load_font(18)

    # Текст — переносим по словам
    text = title[:150]
    lines = _wrap_text(draw, text, font, max_width=w - 128)[:3]

    y = h // 2 - 56
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        x = (w - (bbox[2] - bbox[0])) // 2
        draw.text((x, y), line, font=font, fill='#e6edf3')
        y += font_size + 10

    # Метка канала
    label = 'AI News'
    bbox = draw.textbbox((0, 0), label, font=small_font)
    draw.text(((w - (bbox[2] - bbox[0])) // 2, h - 52), label, font=small_font, fill='#58a6ff')

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    logger.info('pillow_fallback_generated', title=title[:60])
    return buf.getvalue()


def _load_font(size: int):
    from PIL import ImageFont
    for name in ('arial.ttf', 'Arial.ttf', 'DejaVuSans.ttf', 'LiberationSans-Regular.ttf'):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _wrap_text(draw, text: str, font, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ''
    for word in words:
        candidate = (current + ' ' + word).strip()
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] > max_width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines
