import re
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
from uuid import uuid4

import structlog

from src.database.models import RawTweet

logger = structlog.get_logger()

# Слова, которые слишком часты и не помогают определить уникальность темы
_STOP_WORDS = {
    'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
    'of', 'with', 'by', 'from', 'is', 'are', 'was', 'be', 'as', 'it',
    'ai', 'new', 'how', 'what', 'why', 'who', 'use', 'using',
}


def _canonical_url(url: str) -> str:
    """Нормализовать URL: убрать utm_*, www, trailing slash, lowercase scheme/host."""
    try:
        p = urlparse(url.strip())
        host = p.netloc.lower().removeprefix('www.')
        path = p.path.rstrip('/')
        # Фильтруем UTM и tracking-параметры
        qs = parse_qs(p.query)
        filtered = {k: v for k, v in qs.items() if not k.startswith('utm_')}
        query = urlencode(filtered, doseq=True)
        return urlunparse((p.scheme.lower(), host, path, '', query, ''))
    except Exception:
        return url.lower().strip()


def _word_shingles(text: str, n: int = 3) -> set[str]:
    """Разбить текст на n-граммы слов, убрав стоп-слова."""
    words = [
        w for w in re.findall(r'[a-zA-Zа-яА-Я0-9]+', text.lower())
        if w not in _STOP_WORDS and len(w) > 2
    ]
    if len(words) < n:
        return set(words)
    return {' '.join(words[i:i + n]) for i in range(len(words) - n + 1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def merge_duplicates(tweets: list[RawTweet], threshold: float = 0.6) -> list[RawTweet]:
    """
    Проставить merge_group_id для дублирующихся записей.
    Возвращает тот же список с заполненным полем merge_group_id.
    Не изменяет записи с уже существующим merge_group_id.
    """
    if not tweets:
        return tweets

    # Индекс: canonical_url → group_id
    url_to_group: dict[str, str] = {}
    # Индекс: group_id → шинглы (объединённые для fuzzy-матчинга)
    group_shingles: dict[str, set] = {}

    result = list(tweets)

    for i, tweet in enumerate(result):
        if tweet.merge_group_id:
            continue  # уже сгруппирован

        canon = _canonical_url(tweet.url)
        shingles = _word_shingles(tweet.text)

        # Stage 1: точный URL-матч
        if canon and canon in url_to_group:
            result[i] = tweet.model_copy(update={'merge_group_id': url_to_group[canon]})
            group_shingles[url_to_group[canon]] |= shingles
            continue

        # Stage 2: fuzzy по заголовку
        best_group: str | None = None
        best_score = 0.0
        for gid, gshingles in group_shingles.items():
            score = _jaccard(shingles, gshingles)
            if score > best_score:
                best_score = score
                best_group = gid

        if best_group and best_score >= threshold:
            result[i] = tweet.model_copy(update={'merge_group_id': best_group})
            group_shingles[best_group] |= shingles
            if canon:
                url_to_group[canon] = best_group
        else:
            # Новая группа (одиночная запись тоже получает group_id для консистентности)
            gid = str(uuid4())
            result[i] = tweet.model_copy(update={'merge_group_id': gid})
            group_shingles[gid] = shingles
            if canon:
                url_to_group[canon] = gid

    merged_count = sum(1 for t in result if t.merge_group_id)
    unique_groups = len({t.merge_group_id for t in result if t.merge_group_id})
    cross_source = unique_groups - len(result)  # отрицательное = сколько объединили
    logger.info('merger_done', total=len(result), groups=unique_groups, cross_source_merged=len(result) - unique_groups)

    return result
