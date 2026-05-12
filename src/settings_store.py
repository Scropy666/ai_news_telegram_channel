import json
from pathlib import Path

_PATH = Path(__file__).parent.parent / 'config' / 'bot_settings.json'
_DEFAULTS: dict = {'post_language': 'en'}

LANGUAGE_LABELS = {'en': 'English', 'ru': 'русский'}
SUPPORTED_LANGUAGES = list(LANGUAGE_LABELS.keys())


def _load() -> dict:
    try:
        return json.loads(_PATH.read_text(encoding='utf-8'))
    except Exception:
        return dict(_DEFAULTS)


def _save(data: dict) -> None:
    _PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def get_post_language() -> str:
    return _load().get('post_language', _DEFAULTS['post_language'])


def set_post_language(lang: str) -> None:
    data = _load()
    data['post_language'] = lang
    _save(data)
