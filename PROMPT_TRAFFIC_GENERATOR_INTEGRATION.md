# MASTER PROMPT — Интеграция генератора трафика с Games Watchtower (`trafficgen`)

> **Как использовать этот файл.** Вставьте его **целиком**, одним сообщением, в AI-агента,
> который работает в репозитории генератора трафика. Файл самодостаточен: агенту не нужен
> доступ к нашему репозиторию, к этому документу в исходном виде или к устному контексту —
> все требования, схемы, JSON-примеры, критерии приёмки и формат отчёта находятся ниже.
>
> **Кому адресовано:** команде/агенту, владеющему репозиторием генератора трафика
> (TalkChart Traffic Generator & Audience Layer, `app_id: trafficgen`).
> **Кто заказчик:** Games Watchtower — единая read-only витрина телеметрии всех приложений
> студии (ончейн-игры + оффчейн-генераторы аудитории).
> **Версия спецификации:** `trafficgen-v1`, дата выдачи промта — 2026-09-22.

---

## 0. Роль, цель и рамка задачи

Ты — инженер интеграций в репозитории генератора трафика. Твоя задача: привести систему к
контракту Games Watchtower и сдать **восемь артефактов** (разделы 3–10) плюс **тесты и отчёт**
(разделы 11–13).

Цель контракта одна: Watchtower должен уметь **без нашего участия, по read-only API,
восстановить полную картину привлечения трафика** — какие кампании живут, какие источники
подключены, на какие страницы они ведут, какие события произошли, где воронка теряет людей,
где данные порваны и где их нет вовсе.

Три принципа, которые важнее всех остальных пунктов:

1. **Read-only наружу.** Watchtower только читает. Никакой записи через `/watchtower/*`.
2. **Честность данных важнее красивых данных.** Если метрика не считается — она
   `unavailable`, а не «примерно 1200». См. раздел 1.
3. **PII не существует.** Ни IP, ни email, ни fingerprint, ни user-agent не покидают
   периметр и не попадают в БД/логи/ответы.

### 1. Жёсткие правила (нарушение любого = работа не принята)

| № | Правило |
|---|---|
| R1 | **Не выдумывать.** Запрещено синтезировать события, метрики, прогнозы, даты, counts «для правдоподобия». Каждое число в ответе API должно прослеживаться до строки в хранилище событий. |
| R2 | Если данных нет → `"dataQuality": "unavailable"`, `"confidence": 0.0`, значение `null`, плюс поле `"reason"` с человеческим объяснением. Пустой ответ или выдуманное число одинаково неприемлемы. |
| R3 | **Запрещённый антипаттерн:** подставлять количество кампаний/страниц/источников из конфига вместо числа реальных событий (например `max(count(CampaignStarted), len(CAMPAIGNS))`). Конфиг ≠ событие. Если `CampaignStarted` реально не эмитился — в воронке `count: 0` и пометка `stageUnavailable: true`. |
| R4 | Секреты (токены, ключи, сид-фразы, приватные ключи) — никогда в коде, конфигах, тестах, документации, логах, commit message. В документации указывается **только имя** env-переменной. |
| R5 | Никаких force-push, переписывания истории git, деплоя, удаления данных и ротации ключей **без явного подтверждения человека**. Подготовь план и остановись. |
| R6 | Любое утверждение «готово» должно сопровождаться выводом реально запущенных тестов/команд. «Должно работать» ≠ «работает». |
| R7 | Если требование технически невыполнимо в вашем стеке — не имитируй его. Напиши в отчёте: что именно, почему, какой компромисс предлагаешь. |
| R8 | Обратная совместимость: исторические события не мутируются. Изменение схемы = новый `parserVersion` (раздел 7.4), а не правка старых строк. |

---

## 2. Контекст: что уже известно про вашу систему

Эти факты зафиксированы заказчиком и **не подлежат переосмыслению** — они идут в паспорт как есть.
Если по какому-то пункту реальность расходится с таблицей — сообщи об этом в отчёте
(раздел 13, блок «Расхождения»), но паспорт заполняй по реальности, а не по таблице.

**Идентичность приложения**

- `app_id: trafficgen`
- `display_name: TalkChart Traffic Generator & Audience Layer`
- `kind: traffic-generator`
- `traffic_type: hybrid` (реальный трафик + внутренняя bot-фабрика `factory_pipeline`)
- `parser_version: trafficgen-v1`

**Источники трафика (8, `source_systems`)**

| `sourceId` | Канал | `sourceType` |
|---|---|---|
| `x_twitter` | лента X/Twitter, Solana Blinks | `real` |
| `perplexity_ai` | GEO-цитирования Perplexity | `real` |
| `chatgpt_search` | GEO-цитирования ChatGPT Search | `real` |
| `google_search` | органика Google | `real` |
| `short_video` | TikTok / YouTube Shorts / Reels | `real` |
| `tiplink_referral` | онбординг-ссылки TipLink | `real` |
| `direct_web` | прямые заходы | `real` |
| `factory_pipeline` | внутренний конвейер генерации контента (GitHub Actions / краулер) | **`bot` — всегда** |

**Целевые страницы (6, `target_pages`)**

`target_terminal`, `target_sixsec`, `target_duel`, `target_crash`, `target_quest`,
`target_tiplink_claim`

**Хранилища**

- `campaign_store: config | database | api` — выберите фактическое и укажите одно.
- `event_store: sqlite3`, retention **30 дней**, `UNIQUE(eventId)`, `UNIQUE(identity)`.
  Допустим эквивалент (Postgres/ClickHouse) **только** если он даёт те же гарантии:
  идемпотентность по двум ключам, курсорная пагинация по монотонному id, replay с нуля,
  retention-прайнинг. Обоснование — в отчёте.
- `auth: none | api-key` (Bearer `WATCHTOWER_READ_TOKEN`).

**Что реально фиксируется сегодня (6 событий)**

`CampaignStarted`, `SessionStarted`, `PageView`, `CTAClicked`, `LandingReached`,
`DataGapDetected`.

Всё остальное из каталога (раздел 6) сейчас **не эмитится**. Их нужно объявить в каталоге со
статусом `unavailable` и причиной — и подключать по мере появления реального сигнала.
**Выдумывать их нельзя.**

---

## 3. Артефакт 1 — Паспорт `WATCHTOWER_INTEGRATION.md`

Создай (или обнови) в корне репозитория файл `WATCHTOWER_INTEGRATION.md`. Он состоит из
**YAML front matter** (машиночитаемо, парсится Watchtower автоматически) и **человеческой
части** (архитектура, каталоги, API, события, безопасность, запуск).

### 3.1 Обязательный YAML front matter

```yaml
---
app_id: trafficgen
display_name: TalkChart Traffic Generator & Audience Layer
kind: traffic-generator
stage: prototype | alpha | beta | live          # одно значение, честное
traffic_type: hybrid                            # real + bot (factory_pipeline)
source_systems:
  - x_twitter
  - perplexity_ai
  - chatgpt_search
  - google_search
  - short_video
  - tiplink_referral
  - direct_web
  - factory_pipeline
target_pages:
  - target_terminal
  - target_sixsec
  - target_duel
  - target_crash
  - target_quest
  - target_tiplink_claim
campaign_store: config | database | api         # одно значение
event_store: sqlite3                            # retention 30d, UNIQUE(eventId), UNIQUE(identity)
event_retention: 30d
auth: none | api-key                            # api-key => Bearer WATCHTOWER_READ_TOKEN (имя, не значение!)
parser_version: trafficgen-v1
api_prefix: /watchtower
integration_type: direct_adapter
source_type: offchain
data_quality: complete | partial | unavailable
read_only: true
last_synced_at: 2026-09-22T00:00:00.000Z        # ISO 8601 UTC, реальная дата последней сверки
implemented_events: [CampaignStarted, SessionStarted, PageView, CTAClicked, LandingReached, DataGapDetected]
unavailable_events: [CampaignCreated, CampaignStopped, CampaignUpdated, SourceConnected, SourceDisconnected, SourceHealthChanged, PageAssigned, PageRemoved, Click, SessionEnded, Abandoned, NavigationCompleted, DeliveryFailed, RetryScheduled, RateLimited, TrafficError, ExporterHealth, DataGapHealed, BotFlagged, AnomalyDetected, AbuseBlocked, ConfigUpdated, EmergencyPause]
---
```

Правила заполнения:

- `stage` — по факту. Если система не в проде, `live` ставить нельзя.
- `data_quality` — `complete`, только если все заявленные `implemented_events` реально пишутся
  и проходят тесты; иначе `partial`.
- `implemented_events` / `unavailable_events` — исчерпывающие списки из каталога (раздел 6).
  Это контракт честности: Watchtower строит UI по этим спискам и не показывает то, чего нет.
- Значение токена в паспорте не появляется **никогда** — только имя переменной окружения.

### 3.2 Обязательные разделы человеческой части

1. Архитектура интеграции (схема поток → scrub → store → API → Watchtower).
2. Каталог кампаний (`id`, название, тип канала, аудитория, посадочные, статус, `startedAt`).
3. Каталог источников (8 штук, с `sourceType`).
4. Каталог целевых страниц (6 штук, `role`, `category`, реальный URL).
5. Спецификация API (раздел 4 этого промта).
6. Спецификация события и канонического конверта (раздел 5).
7. Каталог событий со статусами (раздел 6) — таблица «implemented / unavailable + причина».
8. Дедупликация, gap detection, replay/backfill, retention (раздел 7).
9. Метрики и воронка (раздел 8), наблюдаемость Prometheus (раздел 9).
10. Безопасность, read-only гарантии, PII, consent/opt-out (раздел 10).
11. Инструкция по локальному запуску и верификации (команды, которые реально выполняются).

---

## 4. Артефакт 2 — Read-only API `/watchtower/*`

### 4.1 Единый конверт ответа

**Каждый** JSON-ответ (включая ошибки 4xx/5xx) оборачивается в конверт:

```json
{
  "data": { },
  "generatedAt": "2026-09-22T12:00:00.000Z",
  "period": "7d UTC",
  "source": "trafficgen-exporter",
  "dataQuality": "complete",
  "confidence": 1.0,
  "parserVersion": "trafficgen-v1"
}
```

| Поле | Правила |
|---|---|
| `data` | Полезная нагрузка эндпоинта. Для ошибок — `{"error": "...", "code": "..."}`. |
| `generatedAt` | ISO 8601 UTC с миллисекундами, время формирования ответа. |
| `period` | Окно данных, к которому относится `data` (`"7d UTC"`, `"30d UTC"`, `"snapshot"`). Для справочников — `"snapshot"`. |
| `source` | Константа `trafficgen-exporter`. |
| `dataQuality` | `complete` \| `partial` \| `unavailable`. |
| `confidence` | `1.0` для complete, `0.0–0.99` для partial (обосновать), **всегда `0.0` для unavailable**. |
| `parserVersion` | `trafficgen-v1` (или текущая версия парсера, раздел 7.4). |

### 4.2 Маршруты

Все маршруты — `GET`, все обязательны. (В исходном ТЗ фигурировала формулировка
«8 read-only endpoints»; фактически маршрутов больше — реализуй **все перечисленные**,
это не противоречие, а уточнение.)

```
GET /watchtower/health
GET /watchtower/readyz
GET /watchtower/config
GET /watchtower/campaigns
GET /watchtower/campaigns/:id
GET /watchtower/sources
GET /watchtower/pages
GET /watchtower/events?cursor=&limit=&eventType=&campaignId=&sourceType=&since=
GET /watchtower/metrics/daily?period=
GET /watchtower/funnels
GET /watchtower/alerts
GET /watchtower/forecast
GET /watchtower/metrics            # Prometheus text format 0.0.4 (не JSON!)
POST|PUT|DELETE|PATCH /watchtower/*  -> 405 Method Not Allowed + заголовок Allow: GET, OPTIONS
```

### 4.3 Контракты эндпоинтов

**`/watchtower/health`** — живость процесса.

```json
{"data": {"status": "ok", "app": "trafficgen", "version": "trafficgen-v1",
          "uptimeSeconds": 1820.5, "timestamp": "2026-09-22T12:00:00.000Z"}}
```

**`/watchtower/readyz`** — готовность зависимостей. `200`, если все проверки `ok`;
иначе `503` и `dataQuality: "partial"`.

```json
{"data": {"ready": true, "checks": {"database": "ok", "snapshot": "ok", "exporter": "ok"}}}
```

**`/watchtower/config`** — паспорт в JSON (зеркалит YAML front matter).

```json
{"data": {
  "appId": "trafficgen",
  "displayName": "TalkChart Traffic Generator & Audience Layer",
  "kind": "traffic-generator",
  "stage": "live",
  "deploymentUrl": "https://…",
  "techStack": "python3, sqlite3, github-actions",
  "trafficType": "hybrid",
  "campaignStore": "config",
  "eventStore": "database",
  "eventRetention": "30d",
  "auth": "none",
  "timeReference": "utc",
  "parserVersion": "trafficgen-v1",
  "readOnly": true,
  "implementedEvents": ["CampaignStarted", "SessionStarted", "PageView", "CTAClicked", "LandingReached", "DataGapDetected"],
  "unavailableEvents": ["CampaignCreated", "…"],
  "sourceSystems": ["x_twitter", "…", "factory_pipeline"],
  "targetPages": ["target_terminal", "…", "target_tiplink_claim"]
}}
```

`auth` обязан отражать реальность: `"api-key"`, если `WATCHTOWER_READ_TOKEN` установлен и
проверяется, иначе `"none"`.

**`/watchtower/campaigns`** — `{"total": N, "campaigns": [ … ]}`.
Поля кампании: `id`, `name`, `type`, `status` (`active|paused|stopped`), `targetAudience`,
`landingPages[]`, `trafficTypes[]`, `startedAt`, `stoppedAt?`.

**`/watchtower/campaigns/:id`** — объект кампании. Не найдена → `404`,
`dataQuality: "unavailable"`, `confidence: 0.0`.

**`/watchtower/sources`** — `{"total": 8, "sources": [ … ]}`.
Поля: `id`, `name`, `channel`, `sourceType` (`real|bot`), `status`
(`connected|disconnected|degraded`). `factory_pipeline` → `sourceType: "bot"`, всегда.

**`/watchtower/pages`** — `{"total": 6, "pages": [ … ]}`.
Поля: `id` (из `target_pages`), `name`, `url` (реальный, не `example.com`), `role`
(`acquisition_hub|studio_game|onboarding_bridge`), `category`.

**`/watchtower/events`** — главный поток. Параметры:

| Параметр | Правила |
|---|---|
| `cursor` | Непрозрачный токен, base64 от `cursor:<lastId>`. Отсутствует → чтение с начала (replay). |
| `limit` | 1..500, default 50. Выход за границы → clamp, не ошибка. |
| `eventType`, `campaignId`, `sourceType`, `since` | Опциональные фильтры, комбинируются по AND. `since` — ISO 8601 UTC. |

Ответ:

```json
{"data": {
  "events": [ { "eventId": "…", "identity": "offchain:trafficgen:…", "chain": "offchain",
                "source": "trafficgen", "app": "trafficgen", "eventType": "PageView",
                "timestamp": "2026-09-22T11:59:59.120Z", "observedAt": "2026-09-22T11:59:59.125Z",
                "campaignId": "talkchart_seo", "sourceId": "x_twitter", "sourceType": "real",
                "pageId": "target_terminal", "sessionId": "sess_9f3ab1", "seq": 1,
                "payload": {}, "parserVersion": "trafficgen-v1", "dataQuality": "complete" } ],
  "count": 1,
  "nextCursor": "Y3Vyc29yOjEyMw==",
  "hasMore": false
}}
```

Требования: сортировка строго по возрастанию внутреннего монотонного `id`;
`hasMore` считается честно (запрос `limit+1`); невалидный `cursor` → `400` с
`"code": "invalid_cursor"` (не тихий перезапуск с нуля — это ломает replay у потребителя).

**`/watchtower/metrics/daily?period=7d`** — см. раздел 8.1. `period` принимает `Nd`
(1..90, default 7).

**`/watchtower/funnels`** — см. раздел 8.2.

**`/watchtower/alerts`** — реестр аномалий.

```json
{"data": {"alerts": [
  {"id": "gap_sess_9f3ab1_4", "alertType": "DataGapDetected", "severity": "warning",
   "status": "active|resolved", "campaignId": "talkchart_seo", "sessionId": "sess_9f3ab1",
   "detectedAt": "…", "resolvedAt": null,
   "details": {"expectedSeq": 4, "receivedSeq": 7, "missingCount": 2}}
], "activeCount": 1}}
```

Алерты порождаются только реальными инцидентами (gap, delivery failure, rate limit, abuse).
Пустой список — нормальный ответ.

**`/watchtower/forecast`** — заглушка **без выдуманных чисел**, всегда, пока не появится
реальная модель с измеряемой точностью:

```json
{"data": {"forecast": null, "model": null,
          "reason": "Модель прогнозирования трафика не развёрнута; прогнозы не выдумываются."},
 "dataQuality": "unavailable", "confidence": 0.0}
```

HTTP-статус при этом `200` (это валидный честный ответ, не ошибка).

**`/watchtower/metrics`** — Prometheus text exposition format `0.0.4`,
`Content-Type: text/plain; version=0.0.4; charset=utf-8`, **без** JSON-конверта. См. раздел 9.

### 4.4 Аутентификация

- Если `WATCHTOWER_READ_TOKEN` установлен: все `GET /watchtower/*` требуют
  `Authorization: Bearer <token>`; при отсутствии/несовпадении → `401` с конвертом
  (`dataQuality: "unavailable"`, `confidence: 0.0`). Сообщение об ошибке не раскрывает
  ожидаемое значение.
- Если не установлен: `auth: none`, эндпоинты публичны на чтение; это должно быть явно
  отражено в `/watchtower/config` и паспорте.
- Токен сравнивается константным временем. Токен не логируется и не возвращается в ответах.

---

## 5. Артефакт 3 — Off-chain конверт события и каноническая идентичность

Единственная допустимая форма события:

```json
{
  "eventId": "uuid",
  "identity": "offchain:trafficgen:<campaignId>:<pageId>:<sessionId>:<seq>",
  "chain": "offchain",
  "source": "trafficgen",
  "app": "trafficgen",
  "eventType": "PageView",
  "timestamp": "2026-09-22T11:59:59.120Z",
  "observedAt": "2026-09-22T11:59:59.125Z",
  "campaignId": "talkchart_seo",
  "sourceId": "x_twitter",
  "sourceType": "real",
  "pageId": "target_terminal",
  "sessionId": "sess_<random>",
  "seq": 1,
  "payload": {},
  "parserVersion": "trafficgen-v1"
}
```

Правила по полям:

- `eventId` — UUID (или `ev_<uuid-hex>`), глобально уникален, генерируется **один раз** на
  стороне источника. Повторная доставка с тем же `eventId` = дубликат.
- `identity` — детерминированная каноническая строка
  `offchain:trafficgen:<campaignId>:<pageId>:<sessionId>:<seq>`. Собирается по одному и тому
  же алгоритму всегда (порядок сегментов фиксирован, значения — как в самом событии).
  Системные события (gap) используют расширяющий суффикс, например
  `offchain:trafficgen:<campaignId>:<pageId>:<sessionId>:gap:<from>-<to>` — он не должен
  коллизировать с обычными `seq`.
- `chain` — всегда `"offchain"`; `source` и `app` — всегда `"trafficgen"`.
- `timestamp` — момент наступления события (UTC, миллисекунды). `observedAt` — момент приёма
  экспортёром. При backfill они расходятся, и это нормально: **сортировка и агрегаты идут по
  `timestamp`, курсор — по внутреннему `id`**.
- `sourceType` — `real` | `bot` | `hybrid`. `hybrid` допустим только для агрегированных
  записей, где реально смешаны оба типа; по умолчанию — `real`, для `factory_pipeline` — `bot`.
- `sessionId` — только псевдоним `sess_<random>` (≥ 8 hex-символов). Никаких cookie-id,
  device-id, хешей email/телефона/кошелька.
- `seq` — монотонный счётчик в рамках сессии, начинается с 1, без пропусков.
- `pageId` — из `target_pages` (раздел 2). Значения вроде `terminal` вместо
  `target_terminal` недопустимы: Watchtower джойнит страницы по id.
- `payload` — объект, специфичный для типа события (раздел 6). До записи проходит
  `strip_pii()` (раздел 10.3).
- `parserVersion` — версия парсера, который нормализовал событие.
- `dataQuality` — `complete` для нормального события; `partial`, если часть полей
  восстановить не удалось (тогда в `payload.reason` — что именно потеряно).

**Единственный путь записи** — внутренний шлюз приёма телеметрии (`POST /api/track`),
который живёт **вне** пространства `/watchtower/*`. Watchtower о нём знает, но писать в него
не может.

---

## 6. Артефакт 4 — Каталог событий

Все типы ниже **обязательны к объявлению** в каталоге (паспорт + `/watchtower/config` +
`GET /watchtower/events` не должен падать на неизвестном `eventType`). Эмитировать нужно
только те, у которых есть **реальный источник сигнала** в коде. Остальные — статус
`unavailable` с причиной; их отсутствие в потоке не считается дефектом.

### 6.1 Жизненный цикл кампаний

| eventType | Когда эмитить | payload | Статус сейчас |
|---|---|---|---|
| `CampaignCreated` | кампания заведена в конфиге/БД | `{name, type, createdBy}` | `unavailable` |
| `CampaignStarted` | кампания переведена в `active` | `{startedAt, channels[]}` | **implemented** |
| `CampaignStopped` | кампания остановлена | `{stoppedAt, reason}` | `unavailable` |
| `CampaignUpdated` | изменены параметры кампании | `{changedFields[], previous{}}` | `unavailable` |

### 6.2 Источники

| eventType | Когда эмитить | payload | Статус сейчас |
|---|---|---|---|
| `SourceConnected` | канал подключён/авторизован | `{sourceId, channel}` | `unavailable` |
| `SourceDisconnected` | канал отвалился/отозван | `{sourceId, reason}` | `unavailable` |
| `SourceHealthChanged` | смена статуса `connected→degraded→disconnected` | `{sourceId, from, to, check}` | `unavailable` |

### 6.3 Страницы

| eventType | Когда эмитить | payload | Статус сейчас |
|---|---|---|---|
| `PageAssigned` | страница привязана к кампании | `{pageId, campaignId, url}` | `unavailable` |
| `PageRemoved` | привязка снята | `{pageId, campaignId, reason}` | `unavailable` |

### 6.4 Сессия и воронка

| eventType | Когда эмитить | payload | Статус сейчас |
|---|---|---|---|
| `SessionStarted` | новая сессия | `{referrerSource}` (без URL с PII, только класс источника) | **implemented** |
| `PageView` | просмотр страницы/пула | `{path, pageId, pool?}` | **implemented** |
| `Click` | любой клик по интерактиву | `{target, elementId}` | `unavailable` |
| `CTAClicked` | клик по промо-слоту игры / TipLink / Blink | `{target, gameId, ctaId}` | **implemented** |
| `LandingReached` | подтверждённый переход на целевую | `{targetUrl, pageId}` | **implemented** |
| `SessionEnded` | явное завершение сессии | `{durationSeconds, eventCount}` | `unavailable` |
| `Abandoned` | сессия брошена (таймаут без `SessionEnded`) | `{lastEventAt, idleSeconds, lastStage}` | `unavailable` |
| `NavigationCompleted` | переход между разделами внутри терминала | `{from, to}` | `unavailable` |

### 6.5 Доставка, надёжность, целостность данных

| eventType | Когда эмитить | payload | Статус сейчас |
|---|---|---|---|
| `DeliveryFailed` | не удалось доставить/записать событие | `{target, errorCode, attempt}` | `unavailable` |
| `RetryScheduled` | поставлен ретрай | `{attempt, nextAttemptAt, backoffSeconds}` | `unavailable` |
| `RateLimited` | получен 429 / сработал собственный лимит | `{sourceId, retryAfterSeconds}` | `unavailable` |
| `TrafficError` | ошибка конвейера трафика | `{stage, errorCode}` | `unavailable` |
| `ExporterHealth` | периодический self-check экспортёра | `{checks{}, degraded}` | `unavailable` |
| `DataGapDetected` | обнаружен пропуск `seq` | `{expectedSeq, receivedSeq, missingCount}` | **implemented** |
| `DataGapHealed` | пропуск закрыт backfill'ом | `{healedSeq[], gapRef, healedAt}` | `unavailable` |

### 6.6 Безопасность и управление

| eventType | Когда эмитить | payload | Статус сейчас |
|---|---|---|---|
| `BotFlagged` | сессия/источник помечен как бот | `{sessionId, signal, action}` | `unavailable` |
| `AnomalyDetected` | статистический выброс по метрике | `{metric, expected, observed, window}` | `unavailable` |
| `AbuseBlocked` | заблокирована abusive-активность | `{reason, scope}` | `unavailable` |
| `ConfigUpdated` | изменена конфигурация экспортёра/кампаний | `{changedKeys[]}` (без значений секретов) | `unavailable` |
| `EmergencyPause` | аварийная остановка генерации | `{reason, triggeredBy}` | `unavailable` |

### 6.7 Правила работы с каталогом

1. Статус каждого типа — `implemented` или `unavailable` — обязан совпадать в трёх местах:
   YAML паспорта, `/watchtower/config`, и фактическом поведении кода (проверяется тестом).
2. `unavailable`-событие нельзя эмитировать «для красоты». Если сигнал появился — переводите
   тип в `implemented`, добавляйте тест и обновляйте паспорт в том же PR.
3. Неизвестный `eventType` на входе не отбрасывается молча: он сохраняется с
   `dataQuality: "partial"` и увеличивает `trafficgen_events_rejected_total` **только** если
   нарушена схема; иначе — инкремент отдельного счётчика неизвестных типов (см. 9).
4. Системные события (`DataGap*`, `ExporterHealth`, `AnomalyDetected`, …) эмитируются самой
   системой, у них `sessionId` может быть служебным (`sess_system_<random>`), но формат
   идентичности сохраняется.

---

## 7. Артефакт 5 — Хранилище, идемпотентность, gap detection, replay/backfill

### 7.1 SQLite-схема (обязательный минимум)

```sql
CREATE TABLE IF NOT EXISTS events (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,  -- основа курсора, монотонная
  event_id       TEXT UNIQUE,                        -- UNIQUE(eventId)
  identity       TEXT UNIQUE,                        -- UNIQUE(identity)
  chain TEXT, source TEXT, app TEXT,
  event_type     TEXT NOT NULL,
  timestamp      TEXT NOT NULL,                      -- ISO 8601 UTC, момент события
  observed_at    TEXT NOT NULL,                      -- ISO 8601 UTC, момент приёма
  campaign_id TEXT, source_id TEXT, source_type TEXT,
  page_id TEXT, session_id TEXT,
  seq            INTEGER,
  payload        TEXT,                               -- JSON, уже после strip_pii()
  parser_version TEXT,
  data_quality   TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts       ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_type      ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_campaign  ON events(campaign_id);
CREATE INDEX IF NOT EXISTS idx_events_session   ON events(session_id, seq);

CREATE TABLE IF NOT EXISTS session_sequences (
  session_id TEXT PRIMARY KEY,
  last_seq   INTEGER,
  gap_open   INTEGER DEFAULT 0,   -- 1, если есть незакрытый разрыв
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS aggregates_daily (date TEXT PRIMARY KEY, metrics TEXT, generated_at TEXT);
CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY KEY, alert_type TEXT, severity TEXT, status TEXT, data TEXT, created_at TEXT, resolved_at TEXT);
CREATE TABLE IF NOT EXISTS sync_cursors (source TEXT PRIMARY KEY, cursor TEXT, updated_at TEXT);
```

Режим: `PRAGMA journal_mode=WAL`, `PRAGMA synchronous=NORMAL` (или FULL), все записи в
транзакции. Один процесс-писатель, чтение конкурентное.

### 7.2 Идемпотентность и дедупликация

- Перед вставкой: проверка по `eventId` **ИЛИ** `identity`. Совпадение → запись не создаётся,
  `trafficgen_events_duplicate_total += 1`, источник получает идемпотентный ответ
  `{"status": "duplicate", "eventId": …, "identity": …}` с HTTP 200 (дубликат — не ошибка).
- Гонки закрываются на уровне БД: `INSERT` с обработкой нарушения UNIQUE, а не только
  предварительным `SELECT`.
- Нарушение схемы (нет `eventId`/`eventType`/`timestamp`, не-UTC время, `seq < 1`,
  неизвестный `sourceType`, PII в payload после scrub'а) → событие **отклоняется**,
  `trafficgen_events_rejected_total += 1`, ответ `422`/`400` с причиной. Отклонённое событие
  в `events` не попадает и в агрегатах не учитывается.

### 7.3 Gap detection: `DataGapDetected` и `DataGapHealed`

Алгоритм (обязателен к реализации именно так, чтобы результаты были воспроизводимы):

1. Для каждого `sessionId` хранится `last_seq`.
2. Пришло событие с `seq`:
   - `seq <= last_seq` → не новый (возможный дубликат/поздний backfill), см. шаг 4.
   - `seq == last_seq + 1` → норма, `last_seq = seq`.
   - `seq > last_seq + 1` → **разрыв**: эмитируется системное событие `DataGapDetected` с
     `payload = {expectedSeq: last_seq+1, receivedSeq: seq, missingCount: seq-last_seq-1}`,
     `session_sequences.gap_open = 1`, создаётся алерт `status: "active"`,
     `trafficgen_data_gaps_total += 1`. `last_seq` обновляется до `seq`.
3. Разрыв не «лечится» сам: он закрывается только когда все `seq` из диапазона
   `[expectedSeq, receivedSeq-1]` реально появились в хранилище.
4. При приёме события, закрывающего открытый диапазон (backfill), проверяется полнота.
   Когда диапазон заполнен → эмитируется `DataGapHealed` с
   `payload = {healedSeq: […], gapRef: <identity события DataGapDetected>, healedAt}`,
   алерт переводится в `status: "resolved"` с `resolvedAt`,
   `trafficgen_data_gaps_healed_total += 1`, `gap_open = 0`.
5. Запрещено считать разрыв закрытым по таймауту или по частичному заполнению.
   Частичное заполнение → `partial`, алерт остаётся `active`.

### 7.4 Версионирование парсера

- Текущая версия: `trafficgen-v1`. Версия пишется в каждое событие (`parserVersion`).
- Любое **ломающее** изменение схемы/семантики (новые обязательные поля, изменение формата
  `identity`, переименование `eventType`) → `trafficgen-v2`. Старые события не переписываются:
  они остаются с `parserVersion: trafficgen-v1`, а API отдаёт их как есть.
- В переходный период поддерживаются оба парсера; в паспорте указывается список
  поддерживаемых версий. Депрекация версии — только с явного согласия Watchtower.
- Косметические изменения (новые опциональные поля) версию не повышают, но фиксируются в
  `CHANGELOG` интеграции.

### 7.5 Курсор, replay, backfill

- **Курсор**: base64 от строки `cursor:<lastId>`, где `lastId` — внутренний
  автоинкрементный `id`. Курсор непрозрачен для потребителя: не добавляйте в него семантику,
  которую нельзя сохранить стабильной.
- **Replay**: `GET /watchtower/events` без `cursor` всегда читает с самого начала доступного
  окна (retention 30d). Потребитель может в любой момент пересчитать всё с нуля.
- **Backfill**: приём исторических событий с оригинальным `timestamp` разрешён и
  идемпотентен. Правила: (а) `observedAt` ставится текущим; (б) порядок выдачи определяется
  `id`, а не `timestamp`, и это задокументировано; (в) backfill закрывает gap'ы по 7.3;
  (г) события старше retention-окна принимаются, но помечаются и попадают под прайнинг —
  предупредите об этом в паспорте.
- **Retention 30d**: регулярный прайнинг (cron/GitHub Actions/фоновый тик) удаляет события
  старше 30 дней; агрегаты `aggregates_daily` хранятся дольше (минимум 180 дней), чтобы
  длинные тренды не пропадали. Факт прайнинга и его окно — в паспорте.

---

## 8. Артефакт 6 — Метрики и воронка

### 8.1 `GET /watchtower/metrics/daily?period=7d`

```json
{"data": {
  "periodDays": 7,
  "window": {"from": "2026-09-15", "to": "2026-09-22"},
  "days": [
    {"date": "2026-09-21",
     "pageViews": 0, "sessions": 0, "uniquePseudoVisitors": 0,
     "sessionDurationSeconds": {"avg": 0.0, "p50": 0.0, "p95": 0.0},
     "bounceRate": 0.0, "ctaClickRate": 0.0, "landingReachedRate": 0.0,
     "events": {"total": 0, "byType": {}},
     "trafficType": {"real": 0, "bot": 0},
     "errors": {"deliveryFailures": 0, "trafficErrors": 0, "exporterErrors": 0},
     "integrity": {"duplicates": 0, "rejected": 0, "dataGaps": 0, "dataGapsHealed": 0}}
  ],
  "totals": { "…та же структура, агрегированная за окно…" },
  "breakdowns": {
    "byCampaign": {"talkchart_seo": {"pageViews": 0, "sessions": 0, "ctaClicks": 0, "landingReached": 0}},
    "bySource":   {"x_twitter": {"pageViews": 0, "sessions": 0}, "factory_pipeline": {"pageViews": 0, "sourceType": "bot"}},
    "byPage":     {"target_terminal": {"pageViews": 0, "ctaClicks": 0}}
  },
  "unavailableMetrics": [
    {"metric": "forecast", "reason": "модель не развёрнута"},
    {"metric": "avgSessionDurationSeconds", "reason": "нет событий SessionEnded/Abandoned — длительность считается по первому и последнему событию сессии и помечена как оценка", "estimate": true}
  ]
}}
```

Определения (фиксируются в паспорте, чтобы Watchtower и вы считали одинаково):

- `uniquePseudoVisitors` — число различных `sessionId` (`sess_<random>`) за день. Это
  **псевдонимы**, не люди и не устройства; так и назвать в документации.
- `bounceRate` — доля сессий ровно с одним событием (или без `CTAClicked`, если выберете это
  определение — но тогда напишите его явно).
- `ctaClickRate` = `CTAClicked / PageView` (0, если `PageView = 0`).
- `landingReachedRate` = `LandingReached / CTAClicked` (0, если кликов не было).
- `p50`/`p95` — перцентили длительности сессии в секундах; считаются по реальным
  `timestamp`. Если длительность не измеряется (нет `SessionEnded`) — вернуть оценку и
  пометить её в `unavailableMetrics` с `"estimate": true`.
- `bot` vs `real` — раздельный учёт **везде**: в днях, в totals, в breakdowns. Смешивать
  нельзя; `hybrid` допустим только как отдельная строка с явным обоснованием.
- Дни без данных присутствуют в массиве с нулями (серия непрерывна), а не пропускаются.

### 8.2 `GET /watchtower/funnels`

Воронка: `CampaignStarted → SessionStarted → PageView → CTAClicked → LandingReached`.

```json
{"data": {
  "funnelId": "trafficgen_overall",
  "funnelName": "TalkChart Acquisition & Game Conversion Funnel",
  "chain": "offchain",
  "window": {"period": "7d UTC"},
  "steps": [
    {"stage": "CampaignStarted", "step": "campaignstarted", "count": 0, "stageUnavailable": true,
     "conversionFromPrev": null, "conversionFromFirst": null, "dropOffRate": null},
    {"stage": "SessionStarted",  "count": 0, "conversionFromPrev": null, "dropOffRate": null},
    {"stage": "PageView",        "count": 0, "conversionFromPrev": null, "dropOffRate": null},
    {"stage": "CTAClicked",      "count": 0, "conversionFromPrev": null, "dropOffRate": null},
    {"stage": "LandingReached",  "count": 0, "conversionFromPrev": null, "dropOffRate": null}
  ],
  "byCampaign": {"talkchart_seo": {"steps": ["…"]}},
  "bySource":   {"x_twitter": {"steps": ["…"]}},
  "trafficQuality": "hybrid"
}}
```

Правила честности для воронки:

- `count` — только реальные события соответствующего типа за окно. Никаких подстановок из
  конфига (R3).
- Если предыдущая ступень = 0, конверсия — `null` (не `1.0`, не `0.0`): делить не на что.
  `null` означает «не вычислимо», и Watchtower отрисуёт это как прочерк.
- Если ступень не эмитируется вовсе (`unavailable` в каталоге) → `stageUnavailable: true`
  и `count: 0`.
- `dropOffRate = 1 - conversionFromPrev`, только когда конверсия вычислима.
- Разрезы `byCampaign` и `bySource` обязательны; `byPage` — если есть данные.

---

## 9. Артефакт 7 — Наблюдаемость (Prometheus)

`GET /watchtower/metrics` (и дубль `GET /metrics`) — text format `0.0.4`, с `# HELP` и
`# TYPE` для каждой метрики, без JSON-конверта.

| Метрика | Тип |_Labels | Смысл |
|---|---|---|---|
| `trafficgen_events_total` | counter | `source_type="real\|bot\|hybrid"` | принятые события |
| `trafficgen_events_duplicate_total` | counter | — | отфильтрованные дубликаты |
| `trafficgen_events_rejected_total` | counter | `reason="schema\|pii\|unknown_type"` | отклонённые события |
| `trafficgen_delivery_failures_total` | counter | `target` | сбои доставки |
| `trafficgen_exporter_errors_total` | counter | — | внутренние ошибки экспортёра |
| `trafficgen_buffer_depth` | gauge | — | глубина очереди/буфера |
| `trafficgen_data_gaps_total` | counter | — | обнаруженные разрывы `seq` |
| `trafficgen_data_gaps_healed_total` | counter | — | закрытые разрывы (рекомендуется) |
| `trafficgen_events_unknown_type_total` | counter | `event_type` | неизвестные типы на входе (рекомендуется) |

Требования:

- Счётчики переживают перезапуск: при старте они восстанавливаются из БД (агрегирующие
  запросы по `events`), а не начинаются с нуля.
- `trafficgen_buffer_depth` должен отражать **реальную** глубину очереди. Захардкоженный `0`
  при наличии буфера — это ложная метрика: либо уберите буфер, либо меряйте его. Если буфера
  нет по архитектуре, верните `0` и напишите в паспорте «буфер отсутствует, метрика
  константна по проекту».
- Метки не должны содержать PII: никаких `session_id`, `ip`, `email`, `user_agent` в labels.
- cardinality под контролем: `event_type` и `reason` — ограниченные справочники.
- Плюс `/watchtower/health` (liveness) и `/watchtower/readyz` (readiness, 503 при деградации).

---

## 10. Артефакт 8 — Безопасность, PII, consent

### 10.1 Секреты в git

1. Прогони скан истории: `gitleaks detect --source . --log-opts="--all"` и/или
   `trufflehog git file://. --since-commit <root>`. Отчёт приложи к ответу (вывод команд).
2. Если найдены секреты: **сначала ротация** (токен, выпущенный в историю, считается
   скомпрометированным независимо от того, удалили вы его или нет), затем удаление из
   рабочего дерева, `.env.example` с пустыми значениями, `.gitignore` на `.env*`,
   `*-keypair.json`, `*.pem`, `credentials*`, `secrets/`.
3. Переписывание истории (`git filter-repo` / BFG) и force-push — **только после письменного
   подтверждения человека**: это ломает чужие клоны и CI. Подготовь точный план команд, список
   затронутых файлов/коммитов и порядок действий — и остановись.
4. Добавь secret-scanning в CI и pre-commit hook, чтобы это не повторилось.

### 10.2 Read-only ключ для Watchtower

- Имя переменной: `WATCHTOWER_READ_TOKEN`. В репозитории встречаются **только имя** —
  в `.env.example` (пустое значение), в паспорте, в CI-конфиге как `${{ secrets.… }}`.
- Значение — только в секрет-менеджере (GitHub Secrets / vault / env рантайма).
- Ключ read-only: он не даёт права писать, менять конфиг или удалять данные. Отдельного
  «admin»-ключа для Watchtower не существует и появляться не должно.
- Ротация: поддержите два валидных токена одновременно (`WATCHTOWER_READ_TOKEN`,
  `WATCHTOWER_READ_TOKEN_PREVIOUS`) на период бесшовной ротации.

### 10.3 PII scrubbing

Функция `strip_pii()` вызывается **до** записи в БД и **до** любого логирования, рекурсивно
по всему объекту, включая вложенные `payload`, массивы и строковые JSON-вкрапления.

Запрещённые ключи (минимум): `ip`, `ipAddress`, `x_forwarded_for`, `email`, `e_mail`,
`phone`, `fingerprint`, `deviceId`, `device_id`, `userAgent`, `user_agent`, `cookie`,
`cookies`, `auth`, `auth_token`, `token`, `secret`, `private_key`, `privateKey`,
`seed_phrase`, `mnemonic`, `walletAddress`, `signature`, `latitude`, `longitude`,
`geo`, `location`, `name`, `username`, `firstName`, `lastName`.

Дополнительно:

- Значения-кандидаты вычищаются и по содержанию: IPv4/IPv6-литералы, email-паттерны,
  base58/hex-адреса кошельков длиной 32–44 символа — заменяются на `"[redacted]"`.
- URL в `payload` очищаются от query-параметров, которые могут нести идентификаторы
  (`utm_*` разрешено, `token`, `sig`, `email`, `phone`, `fbclid`, `gclid` — нет).
- Референс источника хранится как **класс** (`x_twitter`, `google_search`, …), а не как
  полный URL с параметрами.
- Логи: уровень `INFO` и выше не содержит payload целиком; при `DEBUG` payload проходит
  через тот же scrub.
- Тест обязателен: событие, содержащее все запрещённые ключи, после `strip_pii()` и записи в
  БД не содержит ни одного из них — ни в колонках, ни в `payload`, ни в логе.

### 10.4 Псевдонимизация и consent/opt-out

- Идентификатор посетителя — только `sess_<random>` (случайный, не производный от PII,
  не стойкий между устройствами). Никаких cross-device графиков.
- Opt-out механизмы (минимум два): (а) уважение `Sec-GPC` / `DNT`; (б) явный переключатель
  в UI и/или параметр `?notrack=1`, который останавливает отправку телеметрии немедленно.
- Opt-out не ретроактивен по умолчанию: уже собранные псевдонимные события остаются, но в
  паспорте и в privacy-заметке это описано явно. Если у вас юридически требуется удаление —
  реализуйте `DELETE`-путь по `sessionId` **внутренним** API (не через `/watchtower/*`).
- В репозитории лежит короткий документ `PRIVACY.md` (или раздел паспорта): что собирается,
  что не собирается, срок хранения (30 дней для событий), как отключиться.

### 10.5 Read-only гарантии и bot-маркировка

- `POST|PUT|DELETE|PATCH` на любой `/watchtower/*` → `405 Method Not Allowed` с заголовком
  `Allow: GET, OPTIONS` и JSON-конвертом ошибки. Это проверяется тестом для **каждого**
  маршрута, а не для одного.
- `OPTIONS` → `204` с CORS-заголовками; CORS не должен разрешать credentials для
  анонимного источника (`Access-Control-Allow-Origin: *` — без `Allow-Credentials: true`).
- Rate limit на `POST /api/track` (например 30 req/s на процесс) с ответом `429` и
  `Retry-After`; факт — счётчиком `RateLimited`, если событие реализовано.
- **Bot-маркировка**: всё, что порождено `factory_pipeline` (автоматическая фабрика,
  краулеры, CI-прогоны, синтетические проверки), обязано иметь `sourceType: "bot"` и
  `sourceId: "factory_pipeline"`. Запрещено: выдавать bot-трафик за `real`, смешивать его в
  `real`-агрегатах, использовать bot-сессии в `uniquePseudoVisitors` без разреза
  `bot`/`real`. Синтетические smoke-тесты помечаются дополнительно
  `payload.synthetic: true` и исключаются из продуктовых агрегатов.

---

## 11. Артефакт 9 — Тесты и приёмка

### 11.1 Обязательные автотесты (contract tests)

Назови их так, чтобы запуск был одной командой (`npm test` / `make test` /
`python3 scripts/test_watchtower.py`). Минимальный набор:

1. **Конверт**: каждый из 12 GET-эндпоинтов возвращает `data`, `generatedAt`, `period`,
   `source`, `dataQuality`, `confidence`, `parserVersion`.
2. **Идентичность**: `identity` собирается как
   `offchain:trafficgen:<campaignId>:<pageId>:<sessionId>:<seq>`; детерминированно —
   одинаковый вход даёт одинаковый `identity`.
3. **Идемпотентность**: повторная отправка того же `eventId` → `duplicate`, одна строка в БД,
   `trafficgen_events_duplicate_total` вырос на 1. То же для совпадающего `identity` при
   другом `eventId`.
4. **Rejected**: событие без `eventType` / с `seq=0` / с не-UTC `timestamp` → отклонено,
   счётчик вырос, в БД не попало.
5. **Курсор и replay**: 1200 событий, `limit=500` → 3 страницы, `nextCursor`/`hasMore`
   корректны, объединение страниц = полный набор без дублей и пропусков; чтение без курсора
   после полного прохода даёт тот же набор; невалидный курсор → `400 invalid_cursor`.
6. **Фильтры**: `eventType`, `campaignId`, `sourceType`, `since` работают по отдельности и
   вместе.
7. **Gap detection**: `seq` 1,2,5 → `DataGapDetected` с `expectedSeq=3, receivedSeq=5,
   missingCount=2`, алерт `active`; затем backfill 3 и 4 → `DataGapHealed`, алерт `resolved`,
   `trafficgen_data_gaps_healed_total` вырос.
8. **PII**: событие с `ip`, `email`, `fingerprint`, `user_agent`, `cookie`, `private_key`,
   `walletAddress` в корне и в `payload` → после записи и в ответе API их нет; `sessionId`
   имеет форму `sess_<random>`.
9. **Read-only**: `POST`/`PUT`/`DELETE`/`PATCH` на каждый маршрут `/watchtower/*` → `405` +
   `Allow: GET, OPTIONS`.
10. **Auth**: при установленном `WATCHTOWER_READ_TOKEN` запрос без токена → `401`, с токеном
    → `200`; в ответах и логах значение токена не встречается.
11. **Метрики**: `metrics/daily` содержит непрерывную серию дней, `avg`/`p50`/`p95`,
    breakdowns по campaign/source/page, разрез `real`/`bot`.
12. **Воронка**: на синтетическом наборе (5/4/3/2/1) ступени, конверсии и drop-off считаются
    верно; при нулевой первой ступени конверсии = `null`, а не выдуманное число.
13. **Forecast**: всегда `dataQuality: "unavailable"`, `confidence: 0.0`, `forecast: null`.
14. **Каталог событий**: `implementedEvents ∪ unavailableEvents` = полный каталог раздела 6,
    пересечение пусто; каждый `implemented` реально эмитируется (есть тест-свидетель).
15. **Prometheus**: формат парсится (HELP/TYPE на каждую метрику), присутствуют все 7
    обязательных метрик, после перезапуска процесса счётчики не обнуляются.
16. **Bot-маркировка**: событие от `factory_pipeline` с `sourceType: "real"` на входе
    нормализуется в `"bot"` (или отклоняется — выберите и зафиксируйте поведение в тесте).

### 11.2 Смоук на живом процессе

Поднять сервер, прогнать curl-обход и приложить **сырой вывод** к отчёту:

```bash
BASE=http://127.0.0.1:8000
curl -sS -o /dev/null -w '%{http_code} %{url_effective}\n' \
  $BASE/watchtower/health $BASE/watchtower/readyz $BASE/watchtower/config \
  $BASE/watchtower/campaigns $BASE/watchtower/campaigns/talkchart_seo \
  $BASE/watchtower/sources $BASE/watchtower/pages \
  "$BASE/watchtower/events?limit=5" "$BASE/watchtower/metrics/daily?period=7d" \
  $BASE/watchtower/funnels $BASE/watchtower/alerts $BASE/watchtower/forecast
curl -sS -o /dev/null -w 'POST=%{http_code}\n' -X POST $BASE/watchtower/events
curl -sS -X POST $BASE/api/track -H 'Content-Type: application/json' \
  -d '{"eventType":"PageView","campaignId":"talkchart_seo","sourceId":"x_twitter",
       "pageId":"target_terminal","sessionId":"sess_smoke01","seq":1,
       "payload":{"ip":"1.2.3.4","email":"a@b.c"}}'
curl -sS "$BASE/watchtower/events?limit=1" | grep -Ei 'ip|email' && echo "PII LEAK" || echo "PII clean"
curl -sS $BASE/watchtower/metrics | head -40
```

Ожидаемо: все GET → `200` (кроме `readyz` при деградации → `503`), `POST /watchtower/*` →
`405`, в выданном событии нет `ip`/`email`, `grep` печатает `PII clean`.

### 11.3 Что приложить к отчёту

- вывод `git log --oneline -n 10` и `git status`;
- вывод прогона тестов (полный, не «все зелёные»);
- вывод смоука из 11.2;
- вывод secret-сканера;
- список добавленных/изменённых файлов.

---

## 12. Definition of Done (проверь каждый пункт перед сдачей)

- [ ] `WATCHTOWER_INTEGRATION.md` создан/обновлён: YAML front matter полностью соответствует
      разделу 3.1, `last_synced_at` — реальная дата, списки `implemented_events` /
      `unavailable_events` совпадают с кодом.
- [ ] Все 12 GET-маршрутов `/watchtower/*` + `/watchtower/metrics` отвечают в едином конверте.
- [ ] `POST/PUT/DELETE/PATCH /watchtower/*` → `405` + `Allow`.
- [ ] Конверт события и формат `identity` — точно как в разделе 5; `pageId` из `target_pages`,
      `sourceId` из `source_systems`.
- [ ] Идемпотентность по `eventId` **и** `identity`, дубликаты считаются, отклонения считаются.
- [ ] Курсор base64 `cursor:<lastId>`, replay с нуля, backfill исторических событий,
      `DataGapDetected` **и** `DataGapHealed`, алерты active/resolved.
- [ ] `parserVersion: trafficgen-v1` в каждом событии и в каждом конверте; политика bump'а
      задокументирована.
- [ ] `strip_pii()` работает до записи и до логов; тест на утечку проходит; `sess_<random>`.
- [ ] retention 30d + прайнинг; `UNIQUE(eventId)`, `UNIQUE(identity)` в схеме.
- [ ] `metrics/daily`: avg/p50/p95, bounce/CTA/landing rate, breakdowns по campaign/source/page,
      errors, duplicates/rejected, разрез bot vs real, непрерывная серия дней.
- [ ] `funnels`: 5 ступеней, conversion + drop-off, `null` вместо выдуманных значений при
      нулевом знаменателе, `stageUnavailable` для нереализованных ступеней.
- [ ] Prometheus: все 7 обязательных метрик, HELP/TYPE, восстановление счётчиков после
      рестарта, `buffer_depth` — реальная глубина (или задокументированная константа).
- [ ] `forecast` — `unavailable`/`confidence 0.0`, без чисел.
- [ ] Секреты: скан истории выполнен, найденное — ротировано и удалено, `.env.example`,
      `.gitignore`, secret-scan в CI; план переписывания истории согласован с человеком.
- [ ] `WATCHTOWER_READ_TOKEN`: в репозитории только имя; поддержка ротации через `_PREVIOUS`.
- [ ] consent/opt-out реализованы и описаны в `PRIVACY.md`/паспорте.
- [ ] `factory_pipeline` всегда `sourceType: "bot"`; синтетика помечена и исключена из
      продуктовых агрегатов.
- [ ] Все 16 групп тестов из 11.1 проходят; вывод приложен.
- [ ] Отчёт по форме раздела 13 заполнен, включая всё, что **не** сделано.

---

## 13. Формат отчёта обратно (обязателен)

Ответь строго по этой структуре, без маркетинговых формулировок:

```markdown
## 1. Статус
<готово | частично | заблокировано> — одной фразой.

## 2. Паспорт
- Путь к WATCHTOWER_INTEGRATION.md, итоговый YAML front matter (как есть).
- stage / traffic_type / campaign_store / event_store / auth / parser_version — финальные значения.

## 3. Эндпоинты
| Маршрут | Статус | Тест | Примечание |
|---|---|---|---|

## 4. События
| eventType | implemented / unavailable | Источник сигнала в коде | Причина unavailable |
|---|---|---|---|

## 5. Хранилище и целостность
Схема, идемпотентность, cursor/replay/backfill, gap detection (Detected + Healed),
retention, parser versioning — что именно сделано, как проверялось.

## 6. Метрики и воронка
Что считается реально, что помечено unavailable/estimate и почему.

## 7. Наблюдаемость
Вывод /watchtower/metrics (сырой) + как счётчики переживают рестарт.

## 8. Безопасность и PII
Вывод secret-сканера, что найдено/ротировано, strip_pii(), consent/opt-out, 405-гарантии,
bot-маркировка. Требуется ли переписывание истории git (план — отдельно, на согласование).

## 9. Тесты
Полный вывод прогона + вывод смоука из 11.2.

## 10. Расхождения и риски
Где реальность расходится со спецификацией (раздел 2), что не сделано, что сделано иначе и
почему, какие вопросы нужно закрыть заказчику.

## 11. Изменённые файлы
Список + `git log --oneline -n 10`.
```

Правила отчёта:

- Пункты 9 и 10 — самые важные. Пустой «рисков нет» воспринимается как непроверенность.
- Каждый «сделано» подтверждается командой и её выводом.
- Каждый «не сделано» сопровождается причиной и оценкой трудозатрат.
- Не прикладывай диффы целиком — только список файлов и ключевые фрагменты.

---

## 14. Порядок работы (рекомендуемый)

1. **Аудит (не писать код).** Прочитать существующий экспортёр/телеметрию, составить таблицу
   «требование → текущее состояние → gap». Приложить её к отчёту как пункт 10.
2. **Хранилище и конверт.** Схема SQLite, `strip_pii()`, `canonicalize_event()`,
   идемпотентность, rejected-логика.
3. **Gap detection.** `DataGapDetected` → затем `DataGapHealed` и lifecycle алертов.
4. **API.** Конверт, 12 GET-маршрутов, курсор/replay/backfill, 405, auth.
5. **Аналитика.** `metrics/daily` (перцентили, breakdowns, bot/real), `funnels`.
6. **Наблюдаемость.** Prometheus + восстановление счётчиков.
7. **Безопасность.** Секреты, ротация, consent/opt-out, bot-маркировка, rate limit.
8. **Тесты, паспорт, отчёт.**
9. **Стоп-точки, где нужно спросить человека:** переписывание git-истории, ротация боевых
   ключей, любой деплой, удаление данных, изменение retention, повышение `parserVersion`.

Если стек отличается от `python3 + sqlite3` (например Node/TS или Go) — контракт сохраняется
дословно, меняются только инструменты; обоснование отличий — в отчёте, пункт 10.

---

## Приложение A. Шаблон ответа `GET /watchtower/events` для самотестирования

```json
{
  "data": {
    "events": [
      {
        "eventId": "6f1c1c9e-6a3f-4b7a-9d21-2b1c9a4f0a11",
        "identity": "offchain:trafficgen:talkchart_seo:target_terminal:sess_9f3ab1c2:1",
        "chain": "offchain",
        "source": "trafficgen",
        "app": "trafficgen",
        "eventType": "SessionStarted",
        "timestamp": "2026-09-22T11:59:59.120Z",
        "observedAt": "2026-09-22T11:59:59.125Z",
        "campaignId": "talkchart_seo",
        "sourceId": "x_twitter",
        "sourceType": "real",
        "pageId": "target_terminal",
        "sessionId": "sess_9f3ab1c2",
        "seq": 1,
        "payload": {"referrerSource": "x_twitter"},
        "parserVersion": "trafficgen-v1",
        "dataQuality": "complete"
      },
      {
        "eventId": "b71a0a10-2f7d-4a19-8f66-0d5c1c2f7a55",
        "identity": "offchain:trafficgen:talkchart_seo:target_terminal:sess_9f3ab1c2:gap:3-4",
        "chain": "offchain",
        "source": "trafficgen",
        "app": "trafficgen",
        "eventType": "DataGapDetected",
        "timestamp": "2026-09-22T12:00:05.000Z",
        "observedAt": "2026-09-22T12:00:05.004Z",
        "campaignId": "talkchart_seo",
        "sourceId": "x_twitter",
        "sourceType": "real",
        "pageId": "target_terminal",
        "sessionId": "sess_9f3ab1c2",
        "seq": 3,
        "payload": {"expectedSeq": 3, "receivedSeq": 5, "missingCount": 2},
        "parserVersion": "trafficgen-v1",
        "dataQuality": "complete"
      }
    ],
    "count": 2,
    "nextCursor": "Y3Vyc29yOjI=",
    "hasMore": false
  },
  "generatedAt": "2026-09-22T12:00:10.000Z",
  "period": "7d UTC",
  "source": "trafficgen-exporter",
  "dataQuality": "complete",
  "confidence": 1.0,
  "parserVersion": "trafficgen-v1"
}
```

## Приложение B. Быстрый словарь

| Термин | Значение |
|---|---|
| Games Watchtower | единая read-only витрина телеметрии приложений студии; потребитель вашего API |
| `trafficgen` | ваш app_id в контракте |
| Канонический конверт | обёртка `{data, generatedAt, period, source, dataQuality, confidence, parserVersion}` |
| `identity` | детерминированный ключ `offchain:trafficgen:<campaignId>:<pageId>:<sessionId>:<seq>` |
| Replay | полное перечитывание потока с нулевого курсора |
| Backfill | дозагрузка исторических событий с оригинальным `timestamp` |
| Gap | пропуск в монотонной последовательности `seq` внутри сессии |
| `unavailable` | честная пометка «данных/метрики не существует», всегда с `confidence: 0.0` и `reason` |
| bot-трафик | события, порождённые `factory_pipeline` и иной автоматикой; `sourceType: "bot"` |

---

**Конец мастер-промпта.** Начни с раздела 14, шаг 1 (аудит), и не переходи к коду, пока не
составлена таблица gap'ов. Помни: честные нули и `unavailable` принимаются; выдуманные
цифры — нет.
