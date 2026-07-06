# Публікація: як отримати креденшели для кожної платформи

Ця сторінка — практична інструкція для поля `credentials` акаунта (`Account.credentials_encrypted`,
шифрується Fernet, див. `src/clipfactory/crypto.py`). При створенні акаунта через UI/CLI ви вставляєте
JSON саме в тому форматі, що описаний нижче для відповідної платформи (`Account.platform`).

Якщо `DRY_RUN=true` в `.env` — жоден з публішерів нижче не робить жодного мережевого виклику: у лог
пишеться `[DRY RUN] would publish ...` і повертається фейковий результат. Корисно для перевірки всього
конвеєра без реальних облікових записів.

## local — експорт у папку

Нічого отримувати не потрібно, працює одразу «з коробки». Кліп копіюється в
`{EXPORT_DIR}/{name}/`, поряд кладеться JSON-сайдкар з метаданими.

```json
{}
```

або, щоб перевизначити папку/підпапку акаунта:

```json
{ "export_dir": "/home/user/my-exports", "name": "test-account" }
```

## youtube — YouTube Shorts (YouTube Data API v3)

1. Створіть проєкт у [Google Cloud Console](https://console.cloud.google.com/).
2. Увімкніть **YouTube Data API v3** (APIs & Services → Library).
3. Налаштуйте **OAuth consent screen** (тип — External, якщо це не Google Workspace; додайте себе
   як Test user, поки застосунок не пройшов верифікацію).
4. Створіть OAuth-клієнт типу **Desktop app** (Credentials → Create Credentials → OAuth client ID).
   Отримаєте `client_id` і `client_secret`.
5. Отримайте `refresh_token` — найпростіше одноразовим скриптом на `google-auth-oauthlib`
   (пакет уже є серед залежностей проєкту):

   ```python
   from google_auth_oauthlib.flow import InstalledAppFlow

   flow = InstalledAppFlow.from_client_config(
       {
           "installed": {
               "client_id": "ВАШ_CLIENT_ID",
               "client_secret": "ВАШ_CLIENT_SECRET",
               "auth_uri": "https://accounts.google.com/o/oauth2/auth",
               "token_uri": "https://oauth2.googleapis.com/token",
               "redirect_uris": ["http://localhost"],
           }
       },
       scopes=["https://www.googleapis.com/auth/youtube.upload"],
   )
   creds = flow.run_local_server(port=0)
   print("refresh_token:", creds.refresh_token)
   ```

   Альтернатива — [OAuth 2.0 Playground](https://developers.google.com/oauthplayground): вкажіть
   власні `client_id`/`client_secret` в налаштуваннях (шестерня праворуч), у Step 1 виберіть scope
   `https://www.googleapis.com/auth/youtube.upload`, авторизуйтесь, на Step 2 натисніть
   «Exchange authorization code for tokens» — отримаєте `refresh_token`.

6. Креденшели акаунта:

   ```json
   {
     "client_id": "xxxxx.apps.googleusercontent.com",
     "client_secret": "xxxxxxxxxxxxxxxxxxxxxxxx",
     "refresh_token": "1//xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
     "privacy_status": "public"
   }
   ```

   `privacy_status` необов'язковий (`public` за замовчуванням; можна `unlisted`/`private` для тестів).

**Увага:** `refresh_token` для застосунку в режимі Testing (не пройшов Google-верифікацію) стає
недійсним через 7 днів — доведеться повторити крок 5. Для довготривалого продакшн-використання
відправте застосунок на верифікацію Google.

## instagram — Instagram Reels (Meta Graph API)

1. Instagram-акаунт має бути **Business** або **Creator** і прив'язаний до Facebook-сторінки.
2. Створіть застосунок у [Meta for Developers](https://developers.facebook.com/apps/) (тип —
   Business), додайте продукт **Instagram Graph API**.
3. Запросіть дозвіл (permission) **`instagram_content_publish`** (у режимі розробки він доступний
   для акаунтів-тестерів одразу; для продакшену — App Review).
4. Отримайте User Access Token з потрібними правами (`instagram_basic`,
   `instagram_content_publish`, `pages_show_list`) через Graph API Explorer, потім обміняйте його на
   **довгоживучий (long-lived) токен** (~60 днів):

   ```
   GET https://graph.facebook.com/v21.0/oauth/access_token
       ?grant_type=fb_exchange_token
       &client_id={app-id}
       &client_secret={app-secret}
       &fb_exchange_token={короткоживучий_токен}
   ```

5. Знайдіть `ig_user_id` (Instagram Business Account ID), прив'язаний до вашої FB-сторінки:

   ```
   GET https://graph.facebook.com/v21.0/{page-id}?fields=instagram_business_account&access_token={token}
   ```

6. **Обов'язково:** в `.env` має бути заданий `PUBLIC_BASE_URL` (напр. публічний домен або
   тунель на кшталт ngrok/Cloudflare Tunnel до локального `clipfactory run`). Instagram не приймає
   файл напряму — він сам завантажує відео за URL. Кліп має бути доступний за адресою
   `{PUBLIC_BASE_URL}/media/clips/file/{ім'я_файлу_кліпу}` (цей маршрут віддає модуль `api`).

7. Креденшели акаунта:

   ```json
   {
     "access_token": "EAAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
     "ig_user_id": "17841400000000000"
   }
   ```

**Увага:** навіть довгоживучий токен спливає приблизно через 60 днів — оновлюйте його заздалегідь
(є окремий endpoint для рефрешу довгоживучого токена, поки він ще не прострочений), інакше публікації
почнуть падати з `retryable=False` (401/403).

## tiktok — TikTok Content Posting API

1. Створіть застосунок у [TikTok for Developers](https://developers.tiktok.com/), додайте продукт
   **Content Posting API**.
2. Пройдіть OAuth (`user.info.basic`, `video.publish`), отримайте `access_token` (і `refresh_token`
   для довготривалого використання — TikTok access token живе ~24 години).
3. **Важливо:** доки застосунок не пройшов аудит TikTok (Content Posting API audit), публікація
   можлива лише з `privacy_level = "SELF_ONLY"` — тобто відео буде приватним, видимим лише вам.
   Це обмеження TikTok, не цього коду. Після проходження аудиту можна публікувати з `"privacy_level":
   "PUBLIC_TO_EVERYONE"`.

4. Креденшели акаунта:

   ```json
   {
     "access_token": "act.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
     "privacy_level": "SELF_ONLY"
   }
   ```

**Увага:** `access_token` короткоживучий (~24 год) — для регулярної автопублікації потрібен окремий
процес оновлення через `refresh_token` (наразі не автоматизовано в цьому модулі; за потреби —
розширення `tiktok.py`).

## Загальне попередження про токени

Усі три мережеві платформи рано чи пізно "відкликають" токен: строк дії сплив, права відкликано
вручну, змінено пароль тощо. У такому разі `PublishError` буде з `retryable=False` (401/403) —
`Post` перейде в `FAILED` без нескінченних ретраїв, і потрібно оновити `credentials` акаунта вручну.
