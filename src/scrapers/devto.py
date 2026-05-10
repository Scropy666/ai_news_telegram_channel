import asyncio
from datetime import datetime, timezone

import httpx
import structlog

from src.config import settings
from src.database.models import RawTweet

logger = structlog.get_logger()


async def fetch(
    tag: str,
    topic: str,
    per_page: int = 30,
) -> list[RawTweet]:
    url = f'https://dev.to/api/articles?tag={tag}&per_page={per_page}'
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url)
            r.raise_for_status()
            articles = r.json()

        tweets = []
        for a in articles:
            reactions = a.get('positive_reactions_count', 0)
            if reactions < settings.devto_min_reactions:
                continue
            article_id = str(a.get('id', ''))
            tweets.append(RawTweet(
                id=f'dv_{article_id}',
                text=a.get('title', ''),
                author=a.get('user', {}).get('username', 'devto'),
                likes=reactions,
                retweets=a.get('comments_count', 0),
                url=a.get('url', ''),
                scraped_at=datetime.now(timezone.utc),
                topic=topic,
                status='new',
                source='devto',
            ))
        logger.info('devto_fetched', tag=tag, found=len(tweets))
        return tweets
    except Exception as e:
        logger.warning('devto_error', tag=tag, error=str(e))
        return []


async def fetch_all(tags: list[str], topic: str) -> list[RawTweet]:
    all_tweets: list[RawTweet] = []
    for tag in tags:
        tweets = await fetch(tag, topic)
        all_tweets.extend(tweets)
        await asyncio.sleep(0.5)
    return all_tweets
