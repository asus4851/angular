# Архітектура ClipFactory

## 1. Загальний огляд

ClipFactory — **модульний моноліт** на Python: один процес, чіткі внутрішні межі модулів, конвеєр оформлений як стейт-машина над чергою задач у БД. Це свідомий вибір проти мікросервісів/Celery/Redis: система однокористувацька, IO-bound, і головна цінність — простота запуску (`clipfactory run`) і зрозумілість. Кожен модуль ізольований за інтерфейсом, тому шлях масштабування (винести воркер в окремі процеси, замінити чергу на Redis/Celery, SQLite на Postgres) не потребує переписування бізнес-логіки.

```
                     ┌────────────────────────────────────────────┐
                     │                 clipfactory run            │
                     │                                            │
 ┌─────────┐  HTTP   │  ┌─────────┐   ┌───────────┐  ┌─────────┐  │
 │ Браузер ├─────────┼─▶│ FastAPI │   │ Scheduler │  │ Worker  │  │
 │ / CLI   │         │  │ + UI    │   │(APSchedul)│  │ (loop)  │  │
 └─────────┘         │  └────┬────┘   └─────┬─────┘  └────┬────┘  │
                     │       │              │enqueue      │claim  │
                     │       ▼              ▼             ▼       │
                     │  ┌──────────────────────────────────────┐  │
                     │  │        SQLite (SQLAlchemy 2.0)       │  │
                     │  │  channels accounts routes videos     │  │
                     │  │  candidates clips posts jobs         │  │
                     │  └──────────────────────────────────────┘  │
                     └────────────────────────────────────────────┘
                              │                    │
              ┌───────────────┼────────────────────┼──────────────┐
              ▼               ▼                    ▼              ▼
        YouTube (RSS,   Anthropic API        ffmpeg (нарізка,  Соцмережі
        yt-dlp, субтитри) (аналіз віральності) 9:16, субтитри)  (публікація)
```

## 2. Структура пакета

```
src/clipfactory/
├── config.py          # pydantic-settings: все з .env, один об'єкт Settings
├── db.py              # engine, session_scope(), init_db()
├── models.py          # SQLAlchemy ORM: усі сутності + статуси (єдине джерело істини)
├── schemas.py         # Pydantic DTO: контракти між модулями (Moment, RenderPreset, ...)
├── crypto.py          # Fernet-шифрування креденшелів акаунтів
├── ingest/            # виявлення нових відео
│   └── youtube.py     #   RSS-фід каналу (без ключів) + yt-dlp fallback, resolve URL каналу
├── transcripts/       # отримання субтитрів
│   └── youtube.py     #   youtube-transcript-api → fallback yt-dlp auto-subs
├── analysis/          # пошук вірусних моментів
│   ├── base.py        #   Protocol Analyzer
│   ├── claude.py      #   Anthropic tool-use зі structured output, чанкінг довгих транскриптів
│   └── heuristic.py   #   безкоштовний fallback без API-ключа (для демо і тестів)
├── media/             # робота з відео
│   ├── downloader.py  #   yt-dlp: завантаження сегмента джерела
│   ├── captions.py    #   транскрипт → ASS-субтитри (стиль, переноси, таймінг)
│   └── renderer.py    #   ffmpeg: cut + 9:16 (crop / blur-pad) + burn-in субтитрів
├── publish/           # публікація
│   ├── base.py        #   Protocol Publisher + реєстр платформ
│   ├── local.py       #   експорт у папку (працює без будь-чого)
│   ├── youtube.py     #   YouTube Data API v3 → Shorts
│   ├── instagram.py   #   Instagram Graph API → Reels
│   └── tiktok.py      #   TikTok Content Posting API
├── pipeline/          # оркестрація
│   ├── stages.py      #   обробники задач: poll → transcript → analyze → render → publish
│   ├── worker.py      #   цикл: взяти job з БД, виконати, retry з backoff
│   └── scheduler.py   #   APScheduler: періодичний enqueue poll_channel
├── api/               # HTTP-інтерфейс
│   ├── main.py        #   створення FastAPI app, статика, lifespan
│   ├── routers/       #   channels, accounts, routes, videos, candidates, clips, posts, media
│   └── templates/     #   Jinja2-дашборд
└── cli.py             # Typer CLI: init, run, demo, poll, channel/account/route, status
```

## 3. Модель даних

```
Channel 1──* Route *──1 Account          (маршрутизація: канал → акаунти)
Channel 1──* Video 1──1 Transcript
Video   1──* ClipCandidate 1──0..1 Clip  (рендер один на кандидата)
Clip    1──* Post *──1 Route             (публікація кліпа по кожному маршруту)
Job                                      (черга задач конвеєра)
```

| Сутність | Призначення | Ключові поля |
|---|---|---|
| `Channel` | джерело контенту | `yt_channel_id`, `enabled`, `auto_approve`, `check_interval_min`, `min_score`, `max_clips_per_video`, `render_preset` (JSON) |
| `Account` | акаунт соцмережі | `platform` (youtube/instagram/tiktok/local), `credentials_encrypted`, `enabled` |
| `Route` | «відео каналу → в цей акаунт» | `channel_id`, `account_id`, `enabled`, шаблони `title_template`/`description_template`, `extra_hashtags` |
| `Video` | виявлене відео | `yt_video_id`, `status`: NEW → TRANSCRIBED → ANALYZED / SKIPPED / FAILED |
| `Transcript` | субтитри | `segments` JSON `[{start, end, text}]`, `language`, `source` |
| `ClipCandidate` | момент від AI | `start_sec`, `end_sec`, `score` 0..100, `title`, `hook`, `description`, `hashtags`, `reason`, `status`: PENDING → APPROVED/REJECTED |
| `Clip` | відрендерений файл | `file_path`, `status`: QUEUED → RENDERING → RENDERED / FAILED |
| `Post` | публікація | `clip_id`, `route_id`, `status`: PENDING → UPLOADING → PUBLISHED / FAILED, `external_url` |
| `Job` | задача черги | `type`, `payload` JSON, `status`, `attempts`, `run_at`, `last_error` |

Статуси — «дешева» стейт-машина: кожен перехід виконується лише обробником відповідної задачі, тому стан завжди відновлюваний після падіння процесу.

## 4. Конвеєр (типи задач)

```
scheduler ──▶ poll_channel(channel_id)
                │  нові відео з RSS-фіда каналу
                ▼
            fetch_transcript(video_id)
                │  youtube-transcript-api → yt-dlp fallback; немає субтитрів → Video=SKIPPED
                ▼
            analyze_video(video_id)
                │  Analyzer → топ-N кандидатів зі score ≥ channel.min_score
                │  channel.auto_approve ? одразу APPROVED : чекає модерації в UI
                ▼
            render_clip(candidate_id)          (enqueue при APPROVED)
                │  yt-dlp секція джерела → ffmpeg 9:16 + ASS-субтитри
                ▼
            create Post на кожен enabled Route каналу
                ▼
            publish_post(post_id)
                │  Publisher платформи; retry з exp. backoff
                ▼
            PUBLISHED (external_url) / FAILED (last_error видно в UI)
```

**Черга** — таблиця `jobs` у SQLite. Воркер атомарно бере задачу (`UPDATE ... WHERE status='queued'` під транзакцією), виконує, при помилці — `attempts+1`, `run_at = now + 2^attempts * base`; після `max_attempts` — FAILED. Ідемпотентність: кожен обробник спочатку перевіряє поточний стан сутності й безпечно виходить, якщо робота вже зроблена (задачі можуть дублюватись при рестарті).

## 5. Ключові рішення та компроміси

| Рішення | Чому | Компроміс / шлях зростання |
|---|---|---|
| Python | найкращі інструменти саме цієї задачі: yt-dlp, ffmpeg-обв'язки, youtube-transcript-api, anthropic SDK | — |
| SQLite + черга в БД | нуль інфраструктури, транзакційність задача+стан в одній БД | один воркер-процес; далі — Postgres + Celery, інтерфейси не змінюються |
| RSS-фід каналу для polling | без API-ключа YouTube, без квот | тільки ~15 останніх відео; бекфіл старих — через yt-dlp |
| Субтитри YouTube, не Whisper | миттєво і безкоштовно | авто-субтитри бувають неточні; Whisper — опційний апгрейд (інтерфейс `TranscriptProvider` це дозволяє) |
| Claude tool-use structured output | модель повертає строго типізований JSON моментів, без парсингу тексту | чанкінг довгих транскриптів з перекриттям, злиття результатів |
| `HeuristicAnalyzer` fallback | система працює без API-ключа; демо і тести офлайн | якість гірша за Claude — тільки fallback |
| yt-dlp `--download-sections` | завантажуємо лише потрібний фрагмент, а не годинне відео | ключові кадри: ріжемо з запасом і точно тримаємо ffmpeg-ом |
| ASS-субтитри burn-in | повний контроль стилю (шрифт, обводка, позиція в safe-zone 9:16) | — |
| Рендер один на кандидата, метадані — на маршрут | дешево: 1 ffmpeg-прогін на N платформ | окремий пресет на маршрут — майбутнє розширення |
| Fernet-шифрування креденшелів | токени соцмереж не лежать плейнтекстом у БД | `SECRET_KEY` треба берегти |
| `local` publisher за замовчуванням | увесь конвеєр можна запустити й перевірити без жодного облікового запису | — |
| Модерація (`auto_approve=false` за замовчуванням) | захист від публікації сміття; людина в циклі | повний автопілот — один прапорець |

## 6. Конфігурація

Все через `.env` (pydantic-settings), див. `.env.example`:
`ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `DATABASE_URL`, `DATA_DIR`, `EXPORT_DIR`, `SECRET_KEY`, `PUBLIC_BASE_URL` (потрібен Instagram), `HOST`/`PORT`, `POLL_INTERVAL_MIN`, `DRY_RUN` (публікації логуються, але не виконуються).

## 7. Безпека і межі

- Опційний `API_KEY` захищає **всю** HTTP-поверхню (API, дашборд, медіа за id): заголовок `X-API-Key`, cookie `cf_key` або одноразовий `?key=` (ставить cookie). Виняток — публічний `/media/clips/file/{name}`: цей контракт потрібен Instagram Graph API для завантаження відео.
- Креденшели — тільки шифровані (Fernet); значення токенів редагуються (`***`) з усіх текстів помилок публікації перед збереженням у БД.
- `DRY_RUN=true` — безпечний режим для перевірки всього, крім реальних публікацій.
- `POST /api/videos` приймає лише URL, що розпізнаються як YouTube-відео (захист від передачі довільних URL у yt-dlp).
- Юридична межа: інструмент для **власного** контенту або контенту з дозволом; це відповідальність користувача.

## 8. Стійкість конвеєра (після ревью)

- Статуси помилок (`Clip.FAILED`, `Video.FAILED`, `Post.FAILED`) персистяться окремим commit-ом до re-raise, тож відкат транзакції воркера їх не стирає; причина завжди видна в UI.
- Довгі операції (рендер, аплоад) виконуються **поза** транзакцією запису: статус `RENDERING`/`UPLOADING` комітиться одразу, і SQLite-лок не тримається хвилинами.
- Зомбі-задачі `RUNNING` (краш процесу посеред виконання) автоматично повертаються в чергу (`requeue_stale_running`, >30 хв) при старті воркера і кожен тик планувальника.
- Публікація: ретраї рахуються на пості (`Post.attempts`); після вичерпання пост стає `FAILED` і його можна перезапустити з UI. Кліп із `FAILED` рендером — кнопка «Повторити рендер».
- Свіжі відео без субтитрів (авто-капшени ще не згенеровані) не скіпаються назавжди: до 3 повторних спроб кожні 2 години протягом доби.
- Локальні джерела: файл `data/sources/local/{yt_video_id}.mp4`, якщо існує, використовується замість завантаження (механізм демо і ручних джерел).
