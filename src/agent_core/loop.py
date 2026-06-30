"""
src/agent_core/loop.py — Phase 4 autonomous agent loop (OODA-style).

Drives the LLM through function-calling steps using TOOL_SCHEMAS until
ctx.finished is True or max_steps exhausted. MUST NOT import coordinator.
"""
from __future__ import annotations

import json
import structlog

from src.agent_core.tools import AgentContext, TOOL_SCHEMAS, dispatch
from src.agent_core.goals import load_goals, guardrail
from src.generator.prompt_registry import get_prompt

logger = structlog.get_logger()

MODEL = 'llama-3.3-70b-versatile'


async def run_agent_cycle(ctx: AgentContext, max_steps: int = 8) -> None:
    """Запустить агентный цикл tool-calling до завершения или исчерпания шагов.

    Мутирует ctx.to_review, ctx.submitted_count, ctx.finished, ctx.summary.
    Частичный ctx.to_review сохраняется даже при ошибке — coordinator его использует.
    """
    from groq import AsyncGroq
    from src.config import settings

    client = AsyncGroq(api_key=settings.groq_api_key)

    system_text, _ = get_prompt('agent_system')
    goals = load_goals()
    system_prompt = (
        system_text
        .replace('{{OBJECTIVE}}', goals.get('objective', ''))
        .replace('{{MAX_PER_RUN}}', str(guardrail('max_posts_per_run', 5)))
    )

    messages: list[dict] = [
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': 'Начни рабочий цикл. Сначала запроси кандидатов.'},
    ]

    steps = 0
    for step in range(max_steps):
        try:
            resp = await client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=TOOL_SCHEMAS,
                tool_choice='auto',
                temperature=0.3,
                max_tokens=1024,
            )
            msg = resp.choices[0].message

            if msg.tool_calls:
                # Append assistant message WITH tool_calls to history
                messages.append({
                    'role': 'assistant',
                    'content': msg.content or '',
                    'tool_calls': [
                        {
                            'id': tc.id,
                            'type': 'function',
                            'function': {
                                'name': tc.function.name,
                                'arguments': tc.function.arguments,
                            },
                        }
                        for tc in msg.tool_calls
                    ],
                })

                # Execute each tool call
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments or '{}')
                    except Exception:
                        args = {}
                    # Groq может вернуть arguments='null' (→ None) для беспараметровых
                    # тулов; гарантируем dict, иначе args.get(...) в dispatch упадёт.
                    if not isinstance(args, dict):
                        args = {}

                    result = await dispatch(tc.function.name, args, ctx)

                    messages.append({
                        'role': 'tool',
                        'tool_call_id': tc.id,
                        'content': json.dumps(result, ensure_ascii=False, default=str),
                    })
                    logger.info('agent_step', step=step, tool=tc.function.name)

                steps = step + 1
                if ctx.finished:
                    break

            else:
                # No tool calls — model finished its turn
                messages.append({'role': 'assistant', 'content': msg.content or ''})
                steps = step + 1
                break

        except Exception as e:
            logger.warning('agent_cycle_error', error=str(e))
            break

    logger.info(
        'agent_cycle_done',
        steps=steps,
        submitted=ctx.submitted_count,
        finished=ctx.finished,
    )
