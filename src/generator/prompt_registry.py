import yaml
from pathlib import Path
from functools import lru_cache

PROMPTS_DIR = Path(__file__).parent.parent.parent / 'config' / 'prompts'
REGISTRY_PATH = Path(__file__).parent.parent.parent / 'config' / 'registry.yaml'


@lru_cache(maxsize=None)
def load_registry() -> dict:
    with open(REGISTRY_PATH, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def reload_registry() -> None:
    """Сбросить кэш реестра (нужно после обновления registry.yaml без перезапуска)."""
    load_registry.cache_clear()


def get_prompt(name: str, version: str | None = None) -> tuple[str, str]:
    registry = load_registry()
    prompt_config = registry['prompts'].get(name)
    if not prompt_config:
        raise ValueError(f'Unknown prompt: {name}')

    resolved_version = version or prompt_config['default_version']
    file_path = PROMPTS_DIR / f'{name}_v{resolved_version}.md'

    if not file_path.exists():
        raise FileNotFoundError(f'Prompt file not found: {file_path}')

    return file_path.read_text(encoding='utf-8'), resolved_version
