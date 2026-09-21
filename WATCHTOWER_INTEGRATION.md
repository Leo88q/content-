---
app_id: trafficgen
app_name: TalkChart Traffic Generator & Audience Layer
status: complete
api_prefix: /watchtower
integration_type: direct_adapter
source_type: offchain
data_quality: complete
auth_model: bearer
parser_version: trafficgen-v1
last_synced_at: 2026-09-21T14:50:00.000Z
---

# Games Watchtower Integration Passport: TalkChart Traffic Generator (`trafficgen`)

Данный документ описывает контракт интеграции приложения-генератора трафика TalkChart с системой аналитики и мониторинга **Games Watchtower**.

TalkChart выступает внешним off-chain генератором аудитории и адаптером телеметрии для 4 ончейн-игр студии. Адаптер работает в **строгом режиме Read-Only**: не предоставляет методов управления кампаниями, не изменяет состояние внешних систем, гарантирует полную изоляцию от персональных данных (PII) и предоставляет канонический поток событий для сквозного аудита воронки привлечения игроков.

---

## 1. Архитектура интеграции

Генератор трафика TalkChart решает задачу привлечения реальных пользователей Web3 и криптовалютных трейдеров в экосистему 4 крипто-игр студии через несколько органических каналов (SEO/GEO, X/Twitter, вирусные Solana Blinks, 15-секундные вертикальные видео, интерактивный китовый радар и TipLink-онбординг без сид-фраз через Google).

```
[Пользователи / Веб / X / GEO] 
             │
             ▼
   [TalkChart Terminal & SEO Pages]
             │
             ├─────────────── (Сквозные промо-слоты игр + TipLink) ───────────────┐
             │                                                                   │
             ▼                                                                   ▼
   [Client Telemetry: /api/track]                                      [4 Игры Студии / LPs]
             │
             ▼
   ┌─────────────────────────────────────────────────────────┐
   │ Watchtower Exporter Adapter (site/factory/watchtower_exporter.py)│
   │  - PII Scrubbing (удаление IP, email, fingerprint)      │
   │  - Canonical Identity Builder                           │
   │  - ACID Event Store (SQLite)                            │
   │  - Sequence Gap Detection                               │
   │  - Daily Aggregates & Funnel Engine                     │
   └─────────────────────────────────────────────────────────┘
             │
             ├── GET /watchtower/* (Канонические REST API)
             └── GET /watchtower/metrics (Prometheus Scrape)
             │
             ▼
      [Games Watchtower]
```

### Принципы архитектуры:
- **Strict Read-Only**: Все пишущие HTTP-методы (`POST`, `PUT`, `DELETE`, `PATCH`) к пространству `/watchtower/*` возвращают `405 Method Not Allowed`. Изменение конфигурации кампаний через API невозможно.
- **Client Ingestion**: Клиентская телеметрия принимается изолированным шлюзом `POST /api/track` с немедленным удалением PII до записи на диск.
- **Двухуровневая идентификация**: Идемпотентность по уникальному `eventId` и детерминированному `identity` (`offchain:trafficgen:<campaignId>:<pageId>:<sessionId>:<seq>`).
- **Автоматический Gap Detection**: Непрерывный мониторинг монотонного счетчика `seq` по каждой сессии с немедленной генерацией системных алертов `DataGapDetected`.

---

## 2. Каталог кампаний и источников трафика

### Кампании (`GET /watchtower/campaigns`)

| ID кампании | Название | Тип канала | Целевая аудитория | Посадочные страницы |
|---|---|---|---|---|
| `talkchart_seo` | Программный SEO и GEO поиск | `organic_search` | Криптотрейдеры, ресерчеры мем-коинов, AI-поисковики (Perplexity, ChatGPT Search) | `/pools/*.html`, `/gainers/latest.html`, `/vs/latest.html` |
| `talkchart_social_x` | Дайджесты и Solana Blinks в X | `social_distribution` | Solana-сообщество, крипто-твиттер, деген-трейдеры | `/index.html`, `/pools/*.html` |
| `talkchart_video_reels` | 15с вертикальные видео (Shorts/TikTok) | `short_video` | Широкая мобильная аудитория коротких видео | `/index.html` |
| `talkchart_interactive_radar` | Терминал: радар китов и прогнозы 1ч | `retention_loop` | Активные трейдеры DEX, аналитики ончейн-ликвидности | `/index.html` |
| `tiplink_welcome_drop` | TipLink Onboarding (вход через Google) | `game_onboarding_funnel` | Казуальные геймеры Web2/Web3 без крипто-кошельков | `/index.html#games`, TipLink Voucher Claim |

### Источники (`GET /watchtower/sources`)

1. `x_twitter` — социальный трафик из ленты X (Twitter) и интерактивных карточек Solana Blinks.
2. `perplexity_ai` — GEO (Generative Engine Optimization) ответы и цитирования поисковика Perplexity AI.
3. `chatgpt_search` — цитирования поискового ассистента OpenAI ChatGPT Search.
4. `google_search` — органический поисковый трафик Google.
5. `short_video` — переходы из био профилей TikTok, YouTube Shorts, Reels.
6. `tiplink_referral` — прямой онбординг через промо-ссылки TipLink с предустановленным стартовым балансом.
7. `direct_web` — прямые заходы на веб-терминал.
8. `factory_pipeline` — внутренний автоматический сборщик конвейера (`bot`).

---

## 3. Целевые страницы и 4 крипто-игры студии (`GET /watchtower/pages`)

В генераторе трафика зарегистрированы 4 крипто-игры студии (динамически синхронизируемые с `site/games.js` и интерактивной панелью `site/admin.html`):

| ID страницы | Название | Роль в экосистеме | URL назначения | Категория |
|---|---|---|---|---|
| `target_terminal` | TalkChart Live Terminal | `acquisition_hub` | `https://leo88q.github.io/content-/index.html` | utility |
| `target_sixsec` | SixSec (Игра #1) | `studio_game` | Динамический из `site/games.js` | game_1 |
| `target_duel` | CandleDuel (Игра #2) | `studio_game` | Динамический из `site/games.js` | game_2 |
| `target_crash` | MemeCrash (Игра #3) | `studio_game` | Динамический из `site/games.js` | game_3 |
| `target_quest` | WhaleQuest (Игра #4) | `studio_game` | Динамический из `site/games.js` | game_4 |
| `target_tiplink_claim` | TipLink Starter Pass | `onboarding_bridge` | `https://tiplink.io/campaign/talkchart-starter` | voucher |

Интеграция поддерживает мгновенное обновление ссылок на игры прямо через панель управления без перезапуска сервисов.

---

## 4. Спецификация API-эндпоинтов

Все ответы оборачиваются в канонический конверт Games Watchtower:
```json
{
  "data": { ... },
  "generatedAt": "2026-09-21T14:50:00.000Z",
  "period": "7d UTC",
  "source": "trafficgen-exporter",
  "dataQuality": "complete",
  "confidence": 1.0,
  "parserVersion": "trafficgen-v1"
}
```

### 1. `GET /watchtower/health`
- **Назначение**: Проверка работоспособности сервиса.
- **Ответ 200 OK**:
  ```json
  {
    "data": {
      "status": "ok",
      "app": "trafficgen",
      "version": "trafficgen-v1",
      "uptimeSeconds": 1820.5,
      "timestamp": "2026-09-21T14:50:00.000Z"
    },
    "generatedAt": "2026-09-21T14:50:00.000Z",
    "period": "7d UTC",
    "source": "trafficgen-exporter",
    "dataQuality": "complete",
    "confidence": 1.0,
    "parserVersion": "trafficgen-v1"
  }
  ```

### 2. `GET /watchtower/readyz`
- **Назначение**: Проверка готовности локальных хранилищ данных (SQLite и snapshot.json).
- **Ответ 200 OK**:
  ```json
  {
    "data": {
      "ready": true,
      "checks": {
        "database": "ok",
        "snapshot": "ok",
        "exporter": "ok"
      }
    },
    "generatedAt": "2026-09-21T14:50:00.000Z",
    "period": "7d UTC",
    "source": "trafficgen-exporter",
    "dataQuality": "complete",
    "confidence": 1.0,
    "parserVersion": "trafficgen-v1"
  }
  ```

### 3. `GET /watchtower/config`
- **Назначение**: Метаданные адаптера и параметры интеграции.
- **Ответ 200 OK**:
  ```json
  {
    "data": {
      "appId": "trafficgen",
      "displayName": "TalkChart Traffic Generator & Audience Layer",
      "kind": "traffic-generator",
      "stage": "live",
      "deploymentUrl": "https://leo88q.github.io/content-",
      "techStack": "python3, vanilla-js, github-actions, sqlite3",
      "trafficType": "hybrid",
      "campaignStore": "config",
      "eventStore": "database",
      "eventRetention": "30d",
      "auth": "none",
      "timeReference": "utc",
      "parserVersion": "trafficgen-v1",
      "readOnly": true
    },
    "generatedAt": "2026-09-21T14:50:00.000Z",
    "dataQuality": "complete",
    "confidence": 1.0,
    "parserVersion": "trafficgen-v1"
  }
  ```

### 4. `GET /watchtower/campaigns` и `GET /watchtower/campaigns/:id`
- **Назначение**: Список всех активных кампаний генератора или детали конкретной кампании.

### 5. `GET /watchtower/sources`
- **Назначение**: Справочник каналов и источников трафика.

### 6. `GET /watchtower/pages`
- **Назначение**: Реестр целевых страниц и 4 крипто-игр студии с их URL и назначением.

### 7. `GET /watchtower/events?cursor=&limit=&eventType=&campaignId=&sourceType=&since=`
- **Назначение**: Потоковое чтение канонических событий с поддержкой курсорной пагинации, фильтрации и replay.
- **Параметры**:
  - `cursor`: непрозрачный токен курсора (base64 `cursor:<lastId>`).
  - `limit`: число записей (от 1 до 500, по умолчанию 50).
  - `eventType`, `campaignId`, `sourceType`, `since`: опциональные фильтры.
- **Ответ 200 OK**:
  ```json
  {
    "data": {
      "events": [
        {
          "eventId": "ev_abc123456789",
          "identity": "offchain:trafficgen:talkchart_interactive_radar:terminal:sess_xyz:1",
          "chain": "offchain",
          "source": "trafficgen",
          "app": "trafficgen",
          "eventType": "CTAClicked",
          "timestamp": "2026-09-21T14:45:00.120Z",
          "observedAt": "2026-09-21T14:45:00.125Z",
          "campaignId": "talkchart_interactive_radar",
          "sourceId": "direct_web",
          "sourceType": "real",
          "pageId": "terminal",
          "sessionId": "sess_xyz",
          "seq": 1,
          "payload": { "target": "game1" },
          "parserVersion": "trafficgen-v1",
          "dataQuality": "complete"
        }
      ],
      "count": 1,
      "nextCursor": "Y3Vyc29yOjE=",
      "hasMore": false
    },
    "generatedAt": "2026-09-21T14:50:00.000Z",
    "period": "7d UTC",
    "source": "trafficgen-exporter",
    "dataQuality": "complete",
    "confidence": 1.0,
    "parserVersion": "trafficgen-v1"
  }
  ```

### 8. `GET /watchtower/metrics/daily?period=7d`
- **Назначение**: Расчет дневных агрегатов трафика за указанный период (в днях).
- **Показатели**: `totalEvents`, `pageViews`, `sessions`, `uniquePseudoVisitors`, `avgSessionDurationSeconds`, `bounceRate`, `ctaClickRate`, `landingReachedRate`, раскладки по кампаниям, источникам и типам трафика, статистика дубликатов и сбоев.

### 9. `GET /watchtower/funnels`
- **Назначение**: Этапы сквозной воронки конверсии: `CampaignStarted` -> `SessionStarted` -> `PageView` -> `CTAClicked` -> `LandingReached`.

### 10. `GET /watchtower/alerts`
- **Назначение**: Реестр аномалий и сбоев, включая инциденты `DataGapDetected`.

### 11. `GET /watchtower/forecast`
- **Назначение**: Заглушка прогнозной модели. Согласно требованиям спецификации, модель машинного прогнозирования трафика не выдумывает цифры и честно возвращает статус `dataQuality: unavailable`, `confidence: 0.0`.

### 12. `POST /watchtower/*`
- **Запрет записи**: Возвращает `405 Method Not Allowed`.

---

## 5. Спецификация событий (Канонический конверт)

Каждое событие строго соответствует следующей схеме:

```json
{
  "eventId": "string (уникальный идентификатор события)",
  "identity": "offchain:trafficgen:<campaignId>:<pageId>:<sessionId>:<seq>",
  "chain": "offchain",
  "source": "trafficgen",
  "app": "trafficgen",
  "eventType": "CampaignStarted | SessionStarted | PageView | CTAClicked | LandingReached | DataGapDetected",
  "timestamp": "ISO 8601 UTC",
  "observedAt": "ISO 8601 UTC",
  "campaignId": "string",
  "sourceId": "string",
  "sourceType": "real | bot | hybrid",
  "pageId": "string",
  "sessionId": "string (псевдонимный хеш)",
  "seq": "integer (монотонный счетчик в рамках сессии >= 1)",
  "payload": { ... },
  "parserVersion": "trafficgen-v1",
  "dataQuality": "complete | partial | unavailable"
}
```

### События жизненного цикла:
1. **`SessionStarted`**: Создание новой пользовательской сессии. Payload: `{ referrer: string }`.
2. **`PageView`**: Просмотр страницы или мем-пула. Payload: `{ path: string, pool: string }`.
3. **`CTAClicked`**: Клик по промо-слоту игры, TipLink-ссылке или Solana Blink. Payload: `{ target: string, gameId: string }`.
4. **`LandingReached`**: Подтверждение перехода на целевую страницу игры. Payload: `{ targetUrl: string }`.
5. **`DataGapDetected`**: Системное событие при обнаружении пропуска sequence-номеров. Payload: `{ expectedSeq: number, receivedSeq: number, missingCount: number }`.

---

## 6. Дедупликация, Gap Detection и Data Quality

### Дедупликация (Idempotency)
- База данных SQLite обеспечивает ACID-транзакции с `UNIQUE(event_id)` и `UNIQUE(identity)`.
- Повторная отправка идентичного события фиксируется как дубликат, инкрементирует счетчик `trafficgen_events_duplicate_total` и не приводит к повторному учету в агрегатах.

### Gap Detection (Контроль целостности)
- Таблица `session_sequences` отслеживает текущий `last_seq` по каждому `sessionId`.
- При поступлении `seq > last_seq + 1` фиксируется разрыв данных, инкрементируется метрика `trafficgen_data_gaps_total` и в поток событий автоматически записывается событие `DataGapDetected`.

### Replay & Sync Cursor
- Курсор пагинации `cursor` базируется на автоинкрементном первичном ключе `id`.
- Сторонняя система Games Watchtower может сохранять `nextCursor` и в любой момент возобновлять инкрементальную вычитку или перезапускать чтение с нулевого курсора для полного перерасчета (Replay).

---

## 7. Метрики (Prometheus & Daily Aggregates)

Эндпоинт `/watchtower/metrics` (а также `/metrics`) отдает метрики в стандартном формате Prometheus v0.0.4:

| Метрика Prometheus | Тип | Описание |
|---|---|---|
| `trafficgen_events_total{source_type="real\|bot\|hybrid"}` | counter | Суммарное количество принятых событий по типам |
| `trafficgen_events_duplicate_total` | counter | Количество отфильтрованных дубликатов |
| `trafficgen_events_rejected_total` | counter | Количество некорректных / отклоненных событий |
| `trafficgen_delivery_failures_total` | counter | Ошибки сетевой доставки |
| `trafficgen_exporter_errors_total` | counter | Внутренние ошибки экспортера |
| `trafficgen_buffer_depth` | gauge | Глубина очереди буфера памяти |
| `trafficgen_data_gaps_total` | counter | Количество зафиксированных разрывов последовательности |

---

## 8. Политика безопасности, Read-Only гарантии и PII Scrub

1. **Строгий Read-Only режим**:
   - Никаких POST/PUT/DELETE методов к эндпоинтам `/watchtower/*`.
   - Любые попытки вызова пишущих методов возвращают HTTP 405.
2. **Очистка персональных данных (PII Scrubbing)**:
   - Функция `strip_pii()` принудительно удаляет поля `ip`, `email`, `fingerprint`, `device_id`, `user_agent`, `cookie`, `auth_token`, `secret`, `private_key` как из корневого объекта, так и из вложенного `payload`.
   - Сессии идентифицируются случайным псевдонимом `sess_<random>`.
3. **Безопасность репозитория**:
   - В репозитории отсутствуют секретные токены, приватные ключи кошельков и учетные данные.
   - Для опционального ограничения чтения поддерживается переменная окружения `WATCHTOWER_READ_TOKEN` (Bearer token).

---

## 9. Инструкция по локальному запуску и верификации

### Запуск тестов контракта и схемы
```bash
npm test
# или напрямую: python3 scripts/test_watchtower.py
```

### Запуск сквозного Smoke-теста
```bash
npm run smoke
# или напрямую: python3 scripts/smoke_watchtower.py
```

### Запуск экспортера и веб-сервера
```bash
npm start
# или напрямую: python3 site/factory/watchtower_exporter.py
```
Сервер запускается на `http://0.0.0.0:8000`, обеспечивая как раздачу терминала TalkChart, так и обслуживание всех эндпоинтов Games Watchtower по адресу `http://localhost:8000/watchtower/health`.
