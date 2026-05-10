# Claude Skills для проекта ai_news_telegram_channel

Эта папка содержит skill-файлы для Claude Code, специфичные для данного проекта.

## Скиллы в папке:

### Безопасность и защита
| Файл | Назначение | Когда использовать |
|------|-----------|-------------------|
| `safety-guardrails.md` | Dangerous operations blacklist | Проверка команд перед выполнением, защита от data loss |

### Архитектура агентов
| Файл | Назначение | Когда использовать |
|------|-----------|-------------------|
| `agent-designer.md` | Multi-agent architecture | Проектирование Coordinator + Sub-agents, паттерны коммуникации |
| `agent-workflow-designer.md` | Agent workflow orchestration | Цепочка Scraper→Analyzer→Publisher, handoff-контракты |
| `spec-driven-workflow.md` | Spec-first development | Написание спецификации агента до кода |

### Контент и данные
| Файл | Назначение | Когда использовать |
|------|-----------|-------------------|
| `rag-architect.md` | RAG pipeline design | Content retrieval, генерация постов, проверка уникальности |
| `prompt-governance.md` | Prompt management | Управление промптами для генерации, versioning |
| `behuman.md` | Human-like writing | Telegram посты, человечный tone of voice |

### База данных
| Файл | Назначение | Когда использовать |
|------|-----------|-------------------|
| `database-designer.md` | Database architecture | Supabase схема, миграции, индексы |
| `database-schema-designer.md` | Schema modeling | ERD, RLS policies |
| `sql-database-assistant.md` | SQL operations | Запросы, оптимизация |

### API и качество кода
| Файл | Назначение | Когда использовать |
|------|-----------|-------------------|
| `api-design-reviewer.md` | API review | Telegram Bot API, webhooks |
| `autoresearch-agent.md` | Content optimization | A/B тесты постов, CTR optimization |
| `focused-fix.md` | Systematic debugging | Исправление багов, 5-фазный протокол |

## Как использовать:

```bash
# В Claude Code используй:
/skill name=rag-architect
/skill name=prompt-governance
# и т.д.
```

Или читай напрямую:
```bash
/read .claude/skills/rag-architect.md
```

## Оригинальные источники:

Скиллы взяты из репозитория `alirezarezvani/claude-skills` (engineering-advanced-skills v2.3.0)

## Лицензия:

MIT License (как у оригинальных скиллов)
