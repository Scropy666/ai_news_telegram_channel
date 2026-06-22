from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode, quote

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
    params = urlencode({'query': query, 'tags': 'story', 'hitsPerPage': 15})
    # numericFilters requires literal > — httpx encodes > to %3E which Algolia rejects
    numeric = quote(f'created_at_i>{since_ts},points>{min_pts}', safe='>,')
    url = f'https://hn.algolia.com/api/v1/search?{params}&numericFilters={numeric}'
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url)
            r.raise_for_status()
            hits = r.json().get('hits', [])

        tweets = []
        for h in hits:
            story_id = str(h.get('objectID', ''))
            url_val = h.get('url') or f'https://news.ycombinator.com/item?id={story_id}'
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
            ))
        logger.info('hn_fetched', query=query, found=len(tweets))
        return tweets
    except Exception as e:
        logger.warning('hn_error', query=query, error=str(e))
        return []
