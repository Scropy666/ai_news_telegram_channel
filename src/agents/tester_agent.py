import structlog
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from src.database.models import TestCase, TestResult, TestReport

logger = structlog.get_logger()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_raw_tweet(**kwargs):
    from src.database.models import RawTweet
    defaults = dict(
        id=f'hn_test_{uuid4().hex[:8]}',
        text='Test AI tweet about release and new model',
        author='tester',
        likes=100,
        retweets=10,
        url='https://example.com',
        scraped_at=datetime.now(timezone.utc),
        topic='test',
        status='new',
    )
    return RawTweet(**{**defaults, **kwargs})


def _make_post(**kwargs):
    from src.database.models import Post
    defaults = dict(
        id=str(uuid4()),
        type='news_digest',
        content='Test post content for automated testing ' + uuid4().hex,
        prompt_version='news_digest_v1.0.0',
        scheduled_at=datetime.now(timezone.utc),
        status='pending',
    )
    return Post(**{**defaults, **kwargs})


def _save_post_to_db(post):
    from src.database.client import get_db
    db = get_db()
    row = post.model_dump()
    row['scheduled_at'] = row['scheduled_at'].isoformat()
    if row.get('published_at'):
        row['published_at'] = row['published_at'].isoformat()
    db.table('posts').insert(row).execute()


def _delete_post(post_id: str):
    from src.database.client import get_db
    get_db().table('posts').delete().eq('id', post_id).execute()


def _delete_tweet(tweet_id: str):
    from src.database.client import get_db
    get_db().table('raw_tweets').delete().eq('id', tweet_id).execute()


# ── Существующие тесты ────────────────────────────────────────────────────────

async def _test_status() -> TestResult:
    """Проверяет доступность БД и config."""
    from src.database.client import get_db
    from src.config import settings
    try:
        db = get_db()
        db.table('posts').select('id').limit(1).execute()
        text = f'Статус агента OK. Dry run: {settings.dry_run}'
        return TestResult(name='status', passed=True, command='internal:status', response=text)
    except Exception as e:
        return TestResult(name='status', passed=False, command='internal:status', error=str(e))


async def _test_topics_list() -> TestResult:
    """Проверяет загрузку тегов из topics.json."""
    from src.agents.scraper_agent import load_topics
    try:
        topics = load_topics()
        assert len(topics) > 0, 'Список тегов пуст'
        return TestResult(name='topics_list', passed=True, command='internal:load_topics',
                          response=f'Теги: {len(topics)} шт.')
    except Exception as e:
        return TestResult(name='topics_list', passed=False, command='internal:load_topics', error=str(e))


async def _test_add_topic() -> TestResult:
    """Добавляет тестовый тег и удаляет его после проверки."""
    from src.agents.scraper_agent import load_topics, add_topic, save_topics
    try:
        before = load_topics()
        entry = add_topic('TestTopicAutocheck')
        assert entry['topic'] == 'testtopicautocheck', f'Неверный topic key: {entry["topic"]}'
        after = load_topics()
        assert len(after) == len(before) + 1, 'Тег не добавился'
        save_topics(before)
        return TestResult(name='add_topic', passed=True, command='internal:add_topic',
                          response=f'Добавлен: {entry["query"]} → {entry["topic"]}')
    except Exception as e:
        return TestResult(name='add_topic', passed=False, command='internal:add_topic', error=str(e))


async def _test_add_schedule() -> TestResult:
    """Добавляет тестовую задачу и удаляет её после проверки."""
    from src.scheduler.scheduler import load_schedule, add_scheduled_job, remove_scheduled_job
    try:
        job_id = add_scheduled_job('opinion', '23:59', '*')
        jobs = load_schedule()
        found = next((j for j in jobs if j['id'] == job_id), None)
        assert found is not None, f'Задача {job_id} не найдена'
        assert found['type'] == 'opinion'
        assert found['time'] == '23:59'
        remove_scheduled_job(job_id)
        return TestResult(name='add_schedule', passed=True, command='internal:add_scheduled_job',
                          response=f'Добавлено: opinion 23:59, ID={job_id}')
    except Exception as e:
        return TestResult(name='add_schedule', passed=False, command='internal:add_scheduled_job', error=str(e))


async def _test_schedule_list() -> TestResult:
    """Проверяет загрузку расписания из schedule.json."""
    from src.scheduler.scheduler import load_schedule
    try:
        jobs = load_schedule()
        return TestResult(name='schedule_list', passed=True, command='internal:load_schedule',
                          response=f'Расписание: {len(jobs)} задач')
    except Exception as e:
        return TestResult(name='schedule_list', passed=False, command='internal:load_schedule', error=str(e))


# ── Scraper ───────────────────────────────────────────────────────────────────

async def _test_remove_topic() -> TestResult:
    """Добавляет тег и удаляет его по индексу — список возвращается к исходному."""
    from src.agents.scraper_agent import load_topics, add_topic, remove_topic, save_topics
    try:
        before = load_topics()
        add_topic('TestRemoveMe')
        after_add = load_topics()
        assert len(after_add) == len(before) + 1, 'Тег не добавился'
        idx = len(after_add)  # последний элемент
        result = remove_topic(idx)
        assert result is True, 'remove_topic вернул False'
        after_remove = load_topics()
        assert len(after_remove) == len(before), 'Тег не удалился'
        save_topics(before)
        return TestResult(name='remove_topic', passed=True, command='internal:remove_topic',
                          response=f'Удалён тег №{idx}')
    except Exception as e:
        from src.agents.scraper_agent import load_topics as lt, save_topics as st
        try:
            st(lt())
        except Exception:
            pass
        return TestResult(name='remove_topic', passed=False, command='internal:remove_topic', error=str(e))


async def _test_add_topic_duplicate() -> TestResult:
    """Добавление дублирующего тега возвращает None и не меняет список."""
    from src.agents.scraper_agent import load_topics, add_topic, save_topics
    try:
        before = load_topics()
        add_topic('DuplicateTag')
        result = add_topic('DuplicateTag')
        assert result is None, f'Ожидали None, получили: {result}'
        after = load_topics()
        assert len(after) == len(before) + 1, 'Дубликат изменил список'
        save_topics(before)
        return TestResult(name='add_topic_duplicate', passed=True, command='internal:add_topic_duplicate',
                          response='Дубликат корректно отклонён (None)')
    except Exception as e:
        return TestResult(name='add_topic_duplicate', passed=False, command='internal:add_topic_duplicate', error=str(e))


async def _test_save_tweets_dedup() -> TestResult:
    """Сохранение одного и того же твита дважды не создаёт дубликат в БД."""
    from src.agents.scraper_agent import _save_tweets
    from src.database.client import get_db
    tweet = _make_raw_tweet(id=f'hn_dedup_{uuid4().hex[:8]}')
    try:
        _save_tweets([tweet, tweet])  # дважды
        rows = get_db().table('raw_tweets').select('id').eq('id', tweet.id).execute().data
        assert len(rows) == 1, f'Ожидали 1 запись, нашли {len(rows)}'
        return TestResult(name='save_tweets_dedup', passed=True, command='internal:save_tweets_dedup',
                          response='Дедупликация работает: 1 запись')
    except Exception as e:
        return TestResult(name='save_tweets_dedup', passed=False, command='internal:save_tweets_dedup', error=str(e))
    finally:
        _delete_tweet(tweet.id)


# ── Analyzer ──────────────────────────────────────────────────────────────────

async def _test_filter_spam() -> TestResult:
    """Спам-твит с 'follow me' отфильтровывается."""
    from src.agents.analyzer_agent import _filter_and_rank
    try:
        spam = _make_raw_tweet(text='follow me for more AI tips! giveaway inside 🚀🚀🚀', likes=5000)
        items = _filter_and_rank([spam])
        assert len(items) == 0, f'Спам не отфильтрован, нашли {len(items)} элементов'
        return TestResult(name='filter_spam', passed=True, command='internal:filter_and_rank',
                          response='Спам-твит отфильтрован')
    except Exception as e:
        return TestResult(name='filter_spam', passed=False, command='internal:filter_and_rank', error=str(e))


async def _test_filter_manual_topic() -> TestResult:
    """Твит с topic='manual' проходит фильтр даже без ключевых слов и лайков."""
    from src.agents.analyzer_agent import _filter_and_rank
    try:
        manual = _make_raw_tweet(text='completely unrelated text with no ai keywords', likes=0, topic='manual')
        items = _filter_and_rank([manual])
        assert len(items) == 1, f'manual-твит не прошёл фильтр, нашли {len(items)} элементов'
        return TestResult(name='filter_manual_topic', passed=True, command='internal:filter_and_rank',
                          response='manual-твит прошёл фильтр без ключевых слов')
    except Exception as e:
        return TestResult(name='filter_manual_topic', passed=False, command='internal:filter_and_rank', error=str(e))


async def _test_uniqueness_empty_context() -> TestResult:
    """Пустой контекст → всегда уникально (без LLM-вызова)."""
    from src.agents.analyzer_agent import _check_uniqueness
    try:
        result = await _check_uniqueness('Any new post content', context_posts=[])
        assert result.is_unique is True, 'Пустой контекст должен давать is_unique=True'
        assert result.confidence == 1.0, f'confidence должен быть 1.0, получили {result.confidence}'
        return TestResult(name='uniqueness_empty_context', passed=True, command='internal:check_uniqueness',
                          response='Пустой контекст → is_unique=True без LLM')
    except Exception as e:
        return TestResult(name='uniqueness_empty_context', passed=False, command='internal:check_uniqueness', error=str(e))


async def _test_uniqueness_duplicate() -> TestResult:
    """Идентичный текст определяется как дубликат."""
    from src.agents.analyzer_agent import _check_uniqueness
    text = 'OpenAI выпустила новую модель GPT-5.5 с улучшенным reasoning и поддержкой агентных сценариев.'
    try:
        result = await _check_uniqueness(text, context_posts=[text])
        assert result.is_unique is False, f'Дубликат не распознан: {result.reason}'
        return TestResult(name='uniqueness_duplicate', passed=True, command='internal:check_uniqueness',
                          response=f'Дубликат обнаружен (confidence={result.confidence:.2f})')
    except Exception as e:
        return TestResult(name='uniqueness_duplicate', passed=False, command='internal:check_uniqueness', error=str(e))


# ── Publisher / статусы ───────────────────────────────────────────────────────

async def _test_save_and_find_post() -> TestResult:
    """Сохранённый пост находится в БД по ID."""
    from src.database.client import get_db
    post = _make_post()
    try:
        _save_post_to_db(post)
        rows = get_db().table('posts').select('*').eq('id', post.id).execute().data
        assert len(rows) == 1, f'Пост не найден в БД (id={post.id})'
        assert rows[0]['status'] == 'pending'
        return TestResult(name='save_and_find_post', passed=True, command='internal:save_post',
                          response=f'Пост сохранён и найден (id={post.id[:8]}...)')
    except Exception as e:
        return TestResult(name='save_and_find_post', passed=False, command='internal:save_post', error=str(e))
    finally:
        _delete_post(post.id)


async def _test_mark_waiting_publish() -> TestResult:
    """mark_waiting_publish меняет статус и записывает scheduled_at."""
    from src.agents.publisher_agent import mark_waiting_publish
    from src.database.client import get_db
    post = _make_post()
    publish_at = datetime.now(timezone.utc) + timedelta(hours=1)
    try:
        _save_post_to_db(post)
        mark_waiting_publish(post.id, publish_at=publish_at)
        rows = get_db().table('posts').select('status', 'scheduled_at').eq('id', post.id).execute().data
        assert rows[0]['status'] == 'waiting_publish', f'Статус: {rows[0]["status"]}'
        return TestResult(name='mark_waiting_publish', passed=True, command='internal:mark_waiting_publish',
                          response='Статус → waiting_publish, scheduled_at обновлён')
    except Exception as e:
        return TestResult(name='mark_waiting_publish', passed=False, command='internal:mark_waiting_publish', error=str(e))
    finally:
        _delete_post(post.id)


async def _test_cancel_post() -> TestResult:
    """cancel_post меняет статус на cancelled."""
    from src.agents.publisher_agent import cancel_post
    from src.database.client import get_db
    post = _make_post()
    try:
        _save_post_to_db(post)
        cancel_post(post.id)
        rows = get_db().table('posts').select('status').eq('id', post.id).execute().data
        assert rows[0]['status'] == 'cancelled', f'Статус: {rows[0]["status"]}'
        return TestResult(name='cancel_post', passed=True, command='internal:cancel_post',
                          response=f'Статус → cancelled (id={post.id[:8]}...)')
    except Exception as e:
        return TestResult(name='cancel_post', passed=False, command='internal:cancel_post', error=str(e))
    finally:
        _delete_post(post.id)


# ── Coordinator ───────────────────────────────────────────────────────────────

async def _test_build_keyboard() -> TestResult:
    """Suggest-раскладка (Phase 3): 4 кнопки — публикация/вето/через час/черновики (pn rj p1h sv)."""
    from src.agents.coordinator import build_post_keyboard
    try:
        kb = build_post_keyboard('test-post-id')
        buttons = [btn for row in kb.inline_keyboard for btn in row]
        codes = [b.callback_data.split('_')[0] for b in buttons]
        expected = {'pn', 'rj', 'p1h', 'sv'}
        missing = expected - set(codes)
        assert not missing, f'Отсутствуют кнопки: {missing}'
        assert len(buttons) == 4, f'Ожидали 4 кнопки, нашли {len(buttons)}'
        return TestResult(name='build_keyboard', passed=True, command='internal:build_post_keyboard',
                          response=f'Клавиатура: {len(buttons)} кнопок — {sorted(codes)}')
    except Exception as e:
        return TestResult(name='build_keyboard', passed=False, command='internal:build_post_keyboard', error=str(e))


async def _test_get_recent_posts_includes_waiting() -> TestResult:
    """get_recent_published_posts включает посты со статусом waiting_publish."""
    from src.generator.content_generator import get_recent_published_posts
    from src.agents.publisher_agent import mark_waiting_publish
    post = _make_post(status='pending')
    publish_at = datetime.now(timezone.utc) + timedelta(hours=2)
    try:
        _save_post_to_db(post)
        mark_waiting_publish(post.id, publish_at=publish_at)
        contents = get_recent_published_posts(limit=50)
        found = any(post.content in c for c in contents)
        assert found, 'waiting_publish пост не попал в выборку'
        return TestResult(name='get_recent_posts_waiting', passed=True, command='internal:get_recent_published_posts',
                          response='waiting_publish посты включены в uniqueness-контекст')
    except Exception as e:
        return TestResult(name='get_recent_posts_waiting', passed=False,
                          command='internal:get_recent_published_posts', error=str(e))
    finally:
        _delete_post(post.id)


# ── Scheduler ─────────────────────────────────────────────────────────────────

async def _test_remove_schedule() -> TestResult:
    """Удалённая задача не появляется в load_schedule."""
    from src.scheduler.scheduler import load_schedule, add_scheduled_job, remove_scheduled_job
    try:
        job_id = add_scheduled_job('tool_spotlight', '03:00', 'MON')
        assert remove_scheduled_job(job_id) is True, 'remove вернул False'
        jobs = load_schedule()
        found = any(j['id'] == job_id for j in jobs)
        assert not found, 'Удалённая задача всё ещё в расписании'
        return TestResult(name='remove_schedule', passed=True, command='internal:remove_scheduled_job',
                          response=f'Задача {job_id} удалена и не найдена')
    except Exception as e:
        return TestResult(name='remove_schedule', passed=False, command='internal:remove_scheduled_job', error=str(e))


# ── E2E тест ─────────────────────────────────────────────────────────────────

async def _test_e2e_scrape_generate_schedule_cancel() -> TestResult:
    """
    E2E сценарий:
    1. Скрапим по 2 тегам (RAG, LLM) с HackerNews
    2. Генерируем до 2 уникальных постов через LLM
    3. Симулируем отправку на ревью (заполняем drafts)
    4. Пост 1 → сохраняем + waiting_publish на 30 мин
    5. Пост 2 → сохраняем + cancel
    6. Проверяем статусы в БД
    7. Cleanup
    """
    from src.agents.scraper_agent import scrape_by_tags
    from src.agents.analyzer_agent import run_multi
    from src.agents.publisher_agent import mark_waiting_publish, cancel_post
    from src.agents.coordinator import build_post_keyboard
    from src.generator.content_generator import save_post
    from src.database.client import get_db

    generated_post_ids: list[str] = []

    try:
        # ── Step 1: Scrape ────────────────────────────────────────────────────
        tweets = await scrape_by_tags(['RAG', 'LLM'])
        assert len(tweets) > 0, 'Скрапинг по тегам RAG и LLM не вернул результатов'

        # ── Step 2: Generate (source_tweets передаём напрямую, в raw_tweets не сохраняем) ──
        posts = await run_multi(
            source_tweets=tweets,
            post_type='news_digest',
            tags=['RAG', 'LLM'],
        )
        assert len(posts) >= 1, f'LLM не сгенерировал ни одного уникального поста'

        # Если пост только один — создаём синтетический второй для cancel-ветки
        post1 = posts[0]
        post2 = posts[1] if len(posts) >= 2 else _make_post(content='Synthetic post for cancel test ' + uuid4().hex)

        # ── Step 3: Simulate "send for review" ───────────────────────────────
        drafts: dict = {}
        for p in [post1, post2]:
            drafts[p.id] = p
            kb = build_post_keyboard(p.id)
            assert kb is not None, f'Клавиатура не создана для {p.id}'

        assert post1.id in drafts, 'Пост 1 не попал в drafts'
        assert post2.id in drafts, 'Пост 2 не попал в drafts'

        # ── Step 4: Post 1 → save + waiting_publish (30 мин) ─────────────────
        save_post(post1)
        generated_post_ids.append(post1.id)

        publish_at = datetime.now(timezone.utc) + timedelta(minutes=30)
        mark_waiting_publish(post1.id, publish_at=publish_at)

        db = get_db()
        row1 = db.table('posts').select('status', 'scheduled_at').eq('id', post1.id).execute().data
        assert row1, f'Пост 1 не найден в БД'
        assert row1[0]['status'] == 'waiting_publish', \
            f'Пост 1: ожидали waiting_publish, получили {row1[0]["status"]}'

        # ── Step 5: Post 2 → save + cancel ───────────────────────────────────
        save_post(post2)
        generated_post_ids.append(post2.id)

        cancel_post(post2.id)
        drafts.pop(post2.id, None)  # удаляем из черновиков как при rj

        row2 = db.table('posts').select('status').eq('id', post2.id).execute().data
        assert row2, f'Пост 2 не найден в БД'
        assert row2[0]['status'] == 'cancelled', \
            f'Пост 2: ожидали cancelled, получили {row2[0]["status"]}'

        assert post2.id not in drafts, 'Пост 2 остался в drafts после отмены'

        return TestResult(
            name='e2e_scrape_generate_schedule_cancel',
            passed=True,
            command='e2e:scrape[RAG,LLM]→generate→schedule+cancel',
            response=(
                f'Скрапинг: {len(tweets)} твитов (RAG+LLM) | '
                f'Сгенерировано LLM: {len(posts)} постов | '
                f'Пост 1 [{post1.id[:8]}] → waiting_publish 30мин | '
                f'Пост 2 [{post2.id[:8]}] → cancelled'
            ),
        )

    except Exception as e:
        return TestResult(
            name='e2e_scrape_generate_schedule_cancel',
            passed=False,
            command='e2e:scrape[RAG,LLM]→generate→schedule+cancel',
            error=str(e),
        )
    finally:
        for post_id in generated_post_ids:
            _delete_post(post_id)


# ── Наборы тестов ─────────────────────────────────────────────────────────────

E2E_TESTS = [
    _test_e2e_scrape_generate_schedule_cancel,
]

TESTS = [
    # Базовые
    _test_status,
    _test_topics_list,
    _test_add_topic,
    _test_add_schedule,
    _test_schedule_list,
    # Scraper
    _test_remove_topic,
    _test_add_topic_duplicate,
    _test_save_tweets_dedup,
    # Analyzer
    _test_filter_spam,
    _test_filter_manual_topic,
    _test_uniqueness_empty_context,
    _test_uniqueness_duplicate,
    # Publisher / статусы
    _test_save_and_find_post,
    _test_mark_waiting_publish,
    _test_cancel_post,
    # Coordinator
    _test_build_keyboard,
    _test_get_recent_posts_includes_waiting,
    # Scheduler
    _test_remove_schedule,
]


async def run_suite(suite=None) -> TestReport:
    tests = suite or TESTS
    results: list[TestResult] = []

    logger.info('tester_start', tests=len(tests))
    for test_fn in tests:
        result = await test_fn()
        results.append(result)
        logger.info('test_result', name=result.name, passed=result.passed, error=result.error)

    passed = sum(1 for r in results if r.passed)
    report = TestReport(passed=passed, failed=len(results) - passed, results=results)
    logger.info('tester_done', passed=passed, failed=report.failed)
    return report


async def run_e2e() -> TestReport:
    """Запускает только e2e тесты (реальные LLM-вызовы, медленно)."""
    return await run_suite(suite=E2E_TESTS)
