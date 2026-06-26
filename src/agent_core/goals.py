import json
from pathlib import Path
from functools import lru_cache

GOALS_PATH = Path(__file__).parent.parent.parent / 'config' / 'agent_goals.json'


@lru_cache(maxsize=1)
def load_goals() -> dict:
    return json.loads(GOALS_PATH.read_text(encoding='utf-8'))


def guardrail(key: str, default=None):
    return load_goals().get('guardrails', {}).get(key, default)
