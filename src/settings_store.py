from src.database.client import get_db

LANGUAGE_LABELS = {'en': 'English', 'ru': 'русский'}
SUPPORTED_LANGUAGES = list(LANGUAGE_LABELS.keys())

_DEFAULTS = {'post_language': 'en'}


def _get(key: str) -> str | None:
    try:
        rows = get_db().table('bot_settings').select('value').eq('key', key).execute().data
        return rows[0]['value'] if rows else None
    except Exception:
        return None


def _set(key: str, value: str) -> None:
    get_db().table('bot_settings').upsert({'key': key, 'value': value}).execute()


def get_post_language() -> str:
    return _get('post_language') or _DEFAULTS['post_language']


def set_post_language(lang: str) -> None:
    _set('post_language', lang)
