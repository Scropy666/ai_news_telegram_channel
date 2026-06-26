from datetime import datetime, timezone, timedelta

import httpx
import structlog

from src.config import settings
from src.database.models import RawTweet

logger = structlog.get_logger()


async def fetch(
    query: str,
    topic: str,
    hours: int = 24,
    min_points: int | None = None,
) -> list[RawTweet]:
    min_pts = min_points if min_points is not None else settings.min_engagement_likes
    since_ts = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp())
    # Algolia HN API requires the ARRAY form of numericFilters (each filter a
    # separate repeated param). The comma-joined string form is rejected with
    # 400 — verified against the live API. Passing a list lets httpx encode it
    # correctly as numericFilters=...&numericFilters=...
    params = {
        'query': query,
        'tags': 'story',
        'hitsPerPage': 15,
        'numericFilters': [f'created_at_i>{since_ts}', f'points>{min_pts}'],
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get('https://hn.algolia.com/api/v1/search', params=params)
            r.raise_for_status()
            hits = r.json().get('hits', [])

        tweets = []
        for h in hits:
            story_id = str(h.get('objectID', ''))
            url_val = h.get('url') or f'https://news.ycombinator.com/item?id={story_id}'
            created_i = h.get('created_at_i')
            created_dt = datetime.fromtimestamp(created_i, tz=timezone.utc) if created_i else None
            tweets.append(RawTweet(
                id=f'hn_{story_id}',
                text=h.get('title', ''),
                author=h.get('author', 'HN'),
                likes=h.get('points', 0),
                retweets=h.get('num_comments', 0),
                url=url_val,
                scraped_at=datetime.now(timezone.utc),
                topic=topic,
                status='new',
                source='hackernews',
                created_at_source=created_dt,
            ))
        logger.info('hn_fetched', query=query, found=len(tweets))
        return tweets
    except Exception as e:
        logger.warning('hn_error', query=query, error=str(e))
        return []
