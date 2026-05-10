import asyncio
import time
from datetime import datetime, timezone

import httpx
import structlog

from src.config import settings
from src.database.models import RawTweet

logger = structlog.get_logger()

# ── OAuth2 token cache ────────────────────────────────────────────────────────

_token: str | None = None
_token_expires_at: float = 0.0


def _user_agent() -> str:
    return f'python:ai-news-bot:1.0 (by /u/{settings.reddit_username})'


async def _get_token() -> str | None:
    """Получить OAuth2 access token (client credentials). Кэшируем на время жизни."""
    global _token, _token_expires_at

    if not settings.reddit_client_id or not settings.reddit_client_secret:
        return None

    if _token and time.time() < _token_expires_at - 60:
        return _token

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                'https://www.reddit.com/api/v1/access_token',
                auth=(settings.reddit_client_id, settings.reddit_client_secret),
                data={'grant_type': 'client_credentials'},
                headers={'User-Agent': _user_agent()},
            )
            r.raise_for_status()
            data = r.json()

        _token = data['access_token']
        _token_expires_at = time.time() + data.get('expires_in', 3600)
        logger.info('reddit_token_obtained', expires_in=data.get('expires_in'))
        return _token
    except Exception as e:
        logger.warning('reddit_token_failed', error=str(e))
        return None


# ── Fetch ─────────────────────────────────────────────────────────────────────

async def fetch(
    subreddit: str,
    topic: str,
    limit: int = 50,
) -> list[RawTweet]:
    token = await _get_token()
    if not token:
        logger.info('reddit_skipped', reason='no credentials configured')
        return []

    url = f'https://oauth.reddit.com/r/{subreddit}/new?limit={limit}'
    headers = {
        'User-Agent': _user_agent(),
        'Authorization': f'Bearer {token}',
    }
    try:
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            r = await client.get(url)
            if r.status_code == 429:
                logger.warning('reddit_rate_limited', subreddit=subreddit)
                return []
            r.raise_for_status()
            children = r.json().get('data', {}).get('children', [])

        tweets = []
        for child in children:
            d = child.get('data', {})
            score = d.get('score', 0)
            if score < settings.reddit_min_score:
                continue
            post_id = str(d.get('id', ''))
            url_val = d.get('url') or f'https://www.reddit.com{d.get("permalink", "")}'
            tweets.append(RawTweet(
                id=f'rd_{post_id}',
                text=d.get('title', ''),
                author=d.get('author', 'reddit'),
                likes=score,
                retweets=d.get('num_comments', 0),
                url=url_val,
                scraped_at=datetime.now(timezone.utc),
                topic=topic,
                status='new',
                source='reddit',
            ))
        logger.info('reddit_fetched', subreddit=subreddit, found=len(tweets))
        return tweets
    except Exception as e:
        logger.warning('reddit_error', subreddit=subreddit, error=str(e))
        return []


async def fetch_all(subreddits: list[str], topic: str) -> list[RawTweet]:
    # Проверяем credentials до итерации — не тратим sleep попусту
    token = await _get_token()
    if not token:
        return []
    all_tweets: list[RawTweet] = []
    for sub in subreddits:
        tweets = await fetch(sub, topic)
        all_tweets.extend(tweets)
        await asyncio.sleep(1.0)  # respect rate limit (only when actually fetching)
    return all_tweets
