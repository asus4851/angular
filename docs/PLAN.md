# План реалізації ClipFactory

Реалізація розбита на фази з чіткими межами: спочатку «ядро-контракти» (моделі, схеми, конфіг), від яких залежать усі модулі; потім незалежні модулі паралельно; потім оркестрація та інтерфейси поверх них; в кінці — інтеграція, наскрізне демо і ревью.

## Фаза 0 — Очищення і фундамент ✅
- Видалити legacy-код (angular-phonecat).
- `pyproject.toml`, venv, залежності, `.gitignore`, `.env.example`.
- Документація: README, ARCHITECTURE, PLAN.

## Фаза 1 — Ядро (контракти) ✅
- `config.py` — Settings з `.env`.
- `db.py` — engine, `session_scope()`, `init_db()`.
- `models.py` — усі ORM-сутності та enum-статуси (див. ARCHITECTURE §3).
- `schemas.py` — DTO: `TranscriptSegment`, `Moment`, `RenderPreset`, `PostMetadata`, `PublishResult`, API-схеми.
- `crypto.py` — Fernet encrypt/decrypt для креденшелів.

**Це — точка синхронізації**: усі наступні модулі програмуються строго проти цих типів.

## Фаза 2 — Незалежні модулі (паралельно, 4 потоки)

### 2A. `ingest/` + `transcripts/`
- Резолв URL каналу → `channel_id` + метадані (yt-dlp).
- Полінг нових відео через RSS-фід `https://www.youtube.com/feeds/videos.xml?channel_id=...` (без квот), фільтр за датою/відомими id.
- Субтитри: `youtube-transcript-api` (пріоритет: мова каналу → en → перші доступні), fallback — авто-субтитри через yt-dlp (json3). Нормалізація в `list[TranscriptSegment]`.

### 2B. `analysis/`
- `Analyzer` protocol: `find_moments(segments, video, config) -> list[Moment]`.
- `ClaudeAnalyzer`: Anthropic tool-use зі строгою JSON-схемою моментів; чанкінг транскриптів >N токенів з перекриттям; злиття/дедуплікація моментів між чанками; валідація таймкодів проти транскрипта; обрізання до `max_clips_per_video` за score.
- `HeuristicAnalyzer`: працює офлайн — вікна 20–60 с, скоринг за щільністю «хук»-маркерів (питання, числа, емоційні слова), для демо/тестів.
- Фабрика `get_analyzer(settings)`: є ключ → Claude, нема → евристика.

### 2C. `media/`
- `downloader.py`: yt-dlp download-sections (start-5s..end+5s, запас на keyframes), mp4 ≤1080p, кеш у `DATA_DIR/sources`.
- `captions.py`: сегменти транскрипта → ASS: розбивка на рядки ≤N символів, тайминг по сегментах, стиль (великий білий шрифт, чорна обводка, позиція ~78% висоти — safe zone для UI платформ).
- `renderer.py`: побудова ffmpeg-команди: точний trim → scale+crop 1080×1920 (режими `crop` / `blur-pad`) → `ass=` burn-in → H.264+AAC, faststart. Юніт-тести на побудову команди без запуску ffmpeg + один інтеграційний з реальним ffmpeg на синтетичному відео.

### 2D. `publish/`
- `base.py`: `Publisher` protocol (`publish(clip_path, metadata, credentials) -> PublishResult`), реєстр `get_publisher(platform)`, помилки `PublishError(retryable=bool)`.
- `local.py`: копія mp4 + `metadata.json` в `EXPORT_DIR/<account>/`.
- `youtube.py`: videos.insert (categoryId, made-for-kids=false), OAuth refresh-token flow.
- `instagram.py`: Graph API — create container (video_url з `PUBLIC_BASE_URL`) → poll status → publish.
- `tiktok.py`: Content Posting API — init upload → chunk upload → publish.
- `DRY_RUN` — усі мережеві publisher-и лише логують.
- `docs/PUBLISHERS.md` — покрокове отримання креденшелів кожної платформи.

## Фаза 3 — Оркестрація та інтерфейси (паралельно, 2 потоки)

### 3A. `pipeline/` + `cli.py`
- `stages.py`: обробники всіх типів задач (ARCHITECTURE §4), ідемпотентні, з переходами статусів.
- `worker.py`: цикл claim→execute→retry (exp. backoff, max_attempts), graceful shutdown.
- `scheduler.py`: APScheduler enqueue `poll_channel` за `check_interval_min`.
- `cli.py`: `init`, `run` (uvicorn + worker + scheduler в одному процесі), `demo` (офлайн e2e: синтетичне відео + евристичний аналіз + local publish), `poll`, `channel|account|route add/list/...`, `status`.

### 3B. `api/`
- REST-роутери: CRUD каналів/акаунтів/маршрутів, стрічка кандидатів, approve/reject, ретрай постів, virість медіа (`/media/clips/{id}` — також потрібно Instagram).
- Дашборд (Jinja2 + вбудований CSS, без node-збірки): огляд конвеєра, канали, акаунти (форма креденшелів), маршрути, кандидати з прев'ю кліпів і кнопками Approve/Reject, стрічка публікацій зі статусами й помилками.

## Фаза 3.5 — Розширення за вимогами користувача ✅
- Імпорт відео за посиланням (`POST /api/videos`) з параметрами аналізу на запуск.
- Каталог відео каналу (`GET /api/channels/{id}/catalog`) з вибірковим імпортом.
- Повторний аналіз відео з новими параметрами.
- Ad-hoc публікація: `Post.account_id` — кліп у вибрані акаунти без маршруту.

## Фаза 4 — Інтеграція, демо, ревью ✅ критерій готовності
- `clipfactory demo`: ffmpeg-синтезоване відео (testsrc + тон) + синтетичний транскрипт → повний конвеєр до `exports/` **без інтернету і ключів**.
- `pytest` зелений: юніти всіх модулів + інтеграційний тест конвеєра з fake-провайдерами.
- Ревью всього коду (Fable): коректність, ідемпотентність задач, обробка помилок, безпека креденшелів.
- Фінальні README/доки, коміт, пуш.

## Що свідомо відкладено (backlog)
- Whisper-транскрипція відео без субтитрів.
- Окремий рендер-пресет на маршрут; A/B заголовків.
- Аналіз «облич/сцен» для розумного кропу (зараз — центр/blur-pad).
- Планування часу публікації (slots), статистика перегляду через API платформ.
- Multi-user, авторизація в дашборді, Postgres/Celery.
