---
app_id: trafficgen
display_name: TalkChart Traffic Generator & Audience Layer
kind: traffic-generator
stage: live
traffic_type: hybrid
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
campaign_store: config
event_store: sqlite3
event_retention: 30d
auth: none
parser_version: trafficgen-v1
api_prefix: /watchtower
integration_type: direct_adapter
source_type: offchain
data_quality: partial
read_only: true
last_synced_at: 2026-09-24T15:00:00.000Z  # списки событий генерируются из EVENTS_CATALOG
implemented_events:
  - AbuseBlocked
  - AnomalyDetected
  - BotFlagged
  - CTAClicked
  - CampaignCreated
  - CampaignStarted
  - CampaignStopped
  - CampaignUpdated
  - Click
  - DataGapDetected
  - DataGapHealed
  - EmergencyPause
  - ExporterHealth
  - LandingReached
  - NavigationCompleted
  - PageAssigned
  - PageRemoved
  - PageView
  - RateLimited
  - SessionAbandoned
  - SessionEnded
  - SessionStarted
  - SourceConnected
  - SourceDisconnected
  - SourceHealthChanged
  - TrafficError
unavailable_events:
  - ConfigUpdated
  - DeliveryFailed
  - RetryScheduled
---

# Games Watchtower Integration Passport: TalkChart Traffic Generator (`trafficgen`)

Документ описывает контракт интеграции генератора трафика TalkChart с системой аналитики
и мониторинга **Games Watchtower**. Нормативная спецификация требований —
`PROMPT_TRAFFIC_GENERATOR_INTEGRATION.md`; этот паспорт фиксирует фактическое
состояние реализации (по коду, не по декларациям).

`data_quality: partial` — честная оценка: часть каталога событий (12 типов) не имеет
реальных эмиттеров и помечена `unavailable`; прогноз не реализован. Всё заявленное
`implemented` имеет эмиттер и покрыто тестами.

---

## 1. Архитектура интеграции

```
[Пользователи / Веб / X / GEO]
             │
             ▼
   [TalkChart Terminal & SEO Pages]  (site/app.js — клиентская телеметрия)
             │  consent: ?notrack / DNT / GPC
             ├─────────────── (промо-слоты игр + TipLink) ─────────────┐
             │                                                          ▼
             │  POST /api/track (rate limit, scrub PII до записи)  [4 игры студии / LPs]
             ▼
   ┌─────────────────────────────────────────────────────────┐
   │ Watchtower Exporter (site/factory/watchtower_exporter.py)│
   │  - validate (schema-rejected) -> canonicalize            │
   │  - PII scrub (ключи, IPv4/IPv6, email, wallet, query)    │
   │  - sessionId псевдонимизируется до sess_<...>            │
   │  - dedup UNIQUE(eventId)+UNIQUE(identity) [ACID, SQLite] │
   │  - Gap detect DataGapDetected + heal DataGapHealed       │
   │  - catalog reconciler (lifecycle конфига при старте)     │
   │  - daily metrics + честная воронка + Prometheus          │
   └─────────────────────────────────────────────────────────┘
             │
             ├── GET  /watchtower/*          (канонический REST API, read-only)
             ├── GET  /watchtower/metrics    (Prometheus text 0.0.4)
             └── POST /api/track             (единственный пишущий шлюз, вне /watchtower/*)
             ▼
      [Games Watchtower]
```

Принципы:

- **Strict Read-Only**: `POST|PUT|DELETE|PATCH` к любому `/watchtower/*` →
  `405 Method Not Allowed` + заголовок `Allow: GET, OPTIONS` + JSON-конверт ошибки.
  Бывший alias `POST /watchtower/ingest` **удалён** (теперь тоже 405, смок-проверено).
- **Единственный путь записи** — `POST /api/track` (клиентская телеметрия).
- **Replay/Backfill**: чтение без `cursor` всегда с начала окна retention; исторические
  события с оригинальным `timestamp` принимаются идемпотентно; порядок выдачи — по
  внутреннему монотонному `id`.

---

## 2. Каталоги кампаний, источников, страниц

### Кампании (`GET /watchtower/campaigns`)

| ID | Название | Тип канала | Аудитория | Посадочные |
|---|---|---|---|---|
| `talkchart_seo` | Программный SEO и GEO поиск | `organic_search` | Криптотрейдеры, AI-поисковики | `/pools/*.html`, `/gainers/latest.html`, `/vs/latest.html` |
| `talkchart_social_x` | Дайджесты и Solana Blinks в X | `social_distribution` | Solana X-сообщество | `/index.html`, `/pools/*.html` |
| `talkchart_video_reels` | 15с вертикальные видео | `short_video` | Мобильная аудитория | `/index.html` |
| `talkchart_interactive_radar` | Терминал: китовый радар, прогнозы 1ч | `retention_loop` | Активные трейдеры DEX | `/index.html` |
| `tiplink_welcome_drop` | TipLink Onboarding | `game_onboarding_funnel` | Казуальные геймеры Web2/Web3 | `/index.html#games`, TipLink claim |

### Источники (`GET /watchtower/sources`, 8)

`x_twitter`, `perplexity_ai`, `chatgpt_search`, `google_search`, `short_video`,
`tiplink_referral`, `direct_web` — все `sourceType: real`;
`factory_pipeline` — внутренняя автоматика, **всегда `sourceType: bot`**
(принудительная нормализация при приёме, проверено тестом).

### Целевые страницы (`GET /watchtower/pages`, 6)

| ID | Роль | URL |
|---|---|---|
| `target_terminal` | `acquisition_hub` | `{SITE_URL}/index.html` |
| `target_sixsec` | `studio_game` | динамически из `site/games.js` |
| `target_duel` | `studio_game` | динамически из `site/games.js` |
| `target_crash` | `studio_game` | динамически из `site/games.js` |
| `target_quest` | `studio_game` | динамически из `site/games.js` |
| `target_tiplink_claim` | `onboarding_bridge` | tiplink.io/campaign/talkchart-starter |

`pageId` нормализуется на приёме: легаси `terminal` → `target_terminal`
(исторические строки не переписываются).

---

## 3. Спецификация API

Единый конверт ({data, generatedAt, period, source, dataQuality, confidence, parserVersion})
на каждом ответе, включая ошибки 4xx/5xx и 405.

```
GET /watchtower/health                      живость (period="live")
GET /watchtower/readyz                      готовность (503 при деградации, quality=partial)
GET /watchtower/config                      паспорт в JSON (+ implemented/unavailable events, bufferNote)
GET /watchtower/campaigns                   {total, campaigns}
GET /watchtower/campaigns/:id               кампания | 404 not_found
GET /watchtower/sources                     {total, sources}
GET /watchtower/pages                       {total, pages}
GET /watchtower/events?cursor&limit&eventType&campaignId&sourceType&since
GET /watchtower/metrics/daily?period=Nd     N∈[1..90], default 7
GET /watchtower/funnels?period=Nd           воронка по реальным событиям
GET /watchtower/alerts                      разрывы active/resolved (legacy включены)
GET /watchtower/forecast                    всегда dataQuality=unavailable, confidence 0.0, forecast/model=null
GET /watchtower/metrics                     Prometheus text 0.0.4 (без JSON-конверта)
GET /watchtower/proposals                   журнал proposal (контроль-плейн, read-only)
GET /watchtower/audit                       хеш-цепочка аудита + последние записи
GET /watchtower/blocks                      активные блокировки и паузы кампаний
GET /watchtower/consent                     модель consent и состояние реестра
GET /watchtower/identity                    связывание session → внешний идентификатор (статистика)
GET /watchtower/forensics                   качество детекторов + DLQ отклонённых
GET /watchtower/severity                    словарь p1/p2/p3 + severity событий
GET /watchtower/detectors                   состояние фоновых детекторов
GET /watchtower/quality                     задержки (p50/p95/p99), DLQ, счётчики за сутки
POST|PUT|DELETE|PATCH /watchtower/*         -> 405 + Allow: GET, OPTIONS

POST /api/track                             приём событий (batch или одиночное)
POST /api/control/proposals                 создать предложение (нужен Bearer)
POST /api/control/proposals/:id/approve     подтвердить (2FA, не автор)
POST /api/control/proposals/:id/confirm     применить (второй человек, 2FA)
POST /api/control/proposals/:id/rollback    откатить применённое
POST /api/control/consent                   записать решение по consent/opt-out
```

`/watchtower/events`: `limit` clamp 1..500 (default 50); сортировка строго по `id`;
`hasMore` по выборке `limit+1`; **невалидный `cursor` → 400 `invalid_cursor`**
(тихий сброс чтения запрещён — он ломал replay у потребителя). Повтор за `limit > 500`
клэмпится без ошибки. Принимаются две формы курсора: каноническая base64(`cursor:<id>`)
и база base64 голого id (например `MA==` → id 0, replay с начала) — вторая форма
поддерживается ради клиентского чек-листа Watchtower.

Совместимости ради: `/watchtower/health.data` содержит и `ok: true`, и `status: "ok"`;
`/watchtower/config.data` дополнительно содержит массив `campaigns` (то же содержимое,
что и `GET /watchtower/campaigns`).

`/watchtower/config.auth`: `"api-key"`, если установлен `WATCHTOWER_READ_TOKEN`
(сравнение `hmac.compare_digest`; на период ротации валидны ещё и
`WATCHTOWER_READ_TOKEN_PREVIOUS`), иначе `"none"`. Токен не логируется и не
возвращается в ответах.

---

## 4. Конверт события и каноническая идентичность

```json
{
  "eventId": "ev_<hex12> | uuid",
  "identity": "offchain:trafficgen:<campaignId>:<pageId>:<sessionId>:<seq>",
  "chain": "offchain", "source": "trafficgen", "app": "trafficgen",
  "eventType": "PageView", "timestamp": "ISO 8601 UTC (ms)", "observedAt": "ISO 8601 UTC (ms)",
  "campaignId": "talkchart_seo", "sourceId": "x_twitter", "sourceType": "real",
  "pageId": "target_terminal", "sessionId": "sess_<псевдоним>", "seq": 1,
  "payload": {}, "parserVersion": "trafficgen-v1", "dataQuality": "complete | partial"
}
```

- `timestamp` — момент события, `observedAt` — момент приёма. При backfill расходятся:
  агрегаты идут по `timestamp`, курсор — по `id`.
- `sessionId` обязан иметь форму `sess_…`. Иное значение детерминированно
  псевдонимизируется (`sess_<sha256[:10]>`), `identity` пересобирается.
- Системные идентичности расширяют суффикс: `…:gap:<from>-<to>` (DataGapDetected),
  `…:heal:<from>-<to>` (DataGapHealed), `…:created|started|stopped:<…>|updated:<hash>`
  (lifecycle реконсилёра). Коллизий с числовым `seq` не дают.
- Системные сессии (`sess_system*`) исключены из gap-отслеживания.
- Версионирование парсера: `trafficgen-v1`. Ломающие изменения схемы/семантики →
  `trafficgen-v2`, исторические события не переписываются; косметические дополнения
  версию не повышают. Политика депрекации — по согласованию с Watchtower.

### Валидация и каскад отказов (7.2)

| Нарушение | Результат |
|---|---|
| нет/пустой `eventType` | rejected `schema`, 422 для всей партии-одиночки |
| `seq` < 1 или не целое | rejected `schema` |
| `timestamp`/`observedAt` невалидны | rejected `invalid_timestamp` |
| время не в UTC (offset ≠ 0) | rejected `non_utc_timestamp` |
| `sourceType` вне `real|bot|hybrid` | rejected `schema` |
| неизвестный `eventType` | **сохраняется** с `dataQuality: partial`, счётчик `trafficgen_events_unknown_type_total` |
| любой найденный PII | ключи удаляются, литералы → `[redacted]` (до записи) |

Отклонённое событие в `events` не попадает и в агрегатах не учитывается.

---

## 5. Каталог событий (фактические статусы)

`implemented` = есть реальный эмиттер в коде + тест. Всё остальное — `unavailable`
с причиной, без выдуманных эмиссий.

### Implemented (25)

`implemented` = есть реальный эмиттер в коде **и** тест на его работу.

| Группа | События | Эмиттер |
|---|---|---|
| Кампании | `CampaignCreated`, `CampaignStarted`, `CampaignStopped`, `CampaignUpdated` | реконсилёр config store при старте (`catalog_state`, идемпотентно) + предложение `campaign_upsert` |
| Источники | `SourceConnected`, `SourceDisconnected`, `SourceHealthChanged` | реконсилёр |
| Страницы | `PageAssigned`, `PageRemoved` | реконсилёр (вкл. games.js) |
| Сессия/воронка | `SessionStarted`, `PageView`, `Click`, `CTAClicked`, `SessionEnded` | `site/app.js` (`pagehide` + `sendBeacon` для SessionEnded) |
| Завершение без ухода | `SessionAbandoned` | `watchtower_detectors.SessionSweeper` (сессия не завершена, `idle > 1800 c`) |
| Завершённая навигация | `NavigationCompleted` | `site/app.js`: прохождение пути `pool_selected → call_placed → call_resolved` (или `share_flow` / `swap_flow`), один раз на путь |
| Надёжность | `RateLimited` | экспортёр при 429 на `/api/track` |
| Ошибки конвейера | `TrafficError` | `site/factory/watchtower_client.emit_traffic_error` (инструментирован `fetch_data.py`) |
| Здоровье | `ExporterHealth` | `watchtower_detectors.HealthProbe` (БД, снапшот, свежесть, разрывы) |
| Целостность | `DataGapDetected`, `DataGapHealed` | gap detector + backfill |
| Классификация ботов | `BotFlagged` | `watchtower_detectors.BotClassifier` (взвешенные правила, порог 0.6, журнал решений) |
| Аномалии | `AnomalyDetected` | `watchtower_detectors.AnomalyDetector` (медиана/MAD, \|z\| ≥ 3.5, база ≥ 6 ч) |
| Блокировка злоупотребления | `AbuseBlocked` | предложение `block_source` / `block_session` (два подтверждения + 2FA) |
| Аварийная пауза | `EmergencyPause` | предложение `emergency_pause` (два подтверждения + 2FA) |

### Реализовано: 26 / недоступно: 3

Списки выше генерируются из `EVENTS_CATALOG` — документация не может разъехаться с кодом.

### `LandingReached` — как подтверждается переход (раньше был unavailable)

1. CTA ведёт на `GET /r/<clickId>?to=<target>`; `target` берётся только из allowlist
   целей, произвольный URL отклоняется `400` (иначе это open-redirect).
2. Проход через `/r/` регистрирует клик в `site/data/landings.sqlite3` и отдаёт
   `302` на свою страницу с `?wt_click=<clickId>`.
3. Игра/лендинг шлёт `POST /api/track` с `eventType: LandingReached` и
   `payload.clickId`. Нет зарегистрированного клика → `rejected: landing_unconfirmed`.
4. Статистика — `GET /watchtower/landings` (`clicks`, `confirmed`, `pending`,
   `confirmationRate`; при нулевом знаменателе `null`, а не `0`).

Журнал ведёт экспортёр, хаб остаётся read-only: он только потребляет события.

### Unavailable (3) — причины

| Событие | Причина |
|---|---|
| `DeliveryFailed` / `RetryScheduled` | приём синхронный, очереди доставки нет — нечем эмитить |
| `ConfigUpdated` | осознанно не вводим: покрывается granular-событиями реконсилёра и журналом proposal (ADR-0003) |

Исторические названия нормализуются: `Abandoned` → `SessionAbandoned`
(`EVENT_TYPE_ALIASES`), `terminal` → `target_terminal` (`PAGE_ID_ALIASES`).

### Контроль-плейн (никакой автоматики)

Блокировка человека и пауза кампании — необратимые в пределах сессии решения,
поэтому они проходят через proposal:

```
POST /api/control/proposals        (роль proposer)  → status: proposed
POST …/approve                     (роль approver, не автор, TOTP)
POST …/confirm                     (второй approver, TOTP) → status: applied
POST …/rollback                    (любой approver, TOTP)  → status: rolled_back
```

Правила: two-person rule (автор не подтверждает, подтверждают двое разных),
TOTP с защитой от повтора, TTL предложения, RBAC (`proposer`/`approver`/`admin`),
хеш-цепочка аудита (`GET /watchtower/audit`, `verify_audit_chain()`),
откат возвращает предыдущее состояние и пишет событие в историю.
Без `TRAFFICGEN_CONTROL_USERS` все `/api/control/*` — честный `503`.

---

---

## 6. Дедупликация, Gap Detection/Healing, Replay, Retention

- **Дедупликация**: `UNIQUE(event_id)` + `UNIQUE(identity)`; повтор → `duplicate`,
  счётчик инкрементируется, в агрегаты не попадает.
- **Gap detection**: сессия `sess_…` ведёт `last_seq`; `seq > last_seq + 1` →
  открывается разрыв: событие `DataGapDetected` (`payload: {expectedSeq, receivedSeq,
  missingCount}`), строка `session_gaps(status=open)`, алерт `active`.
- **Healing**: разрыв закрывается **только** реальным backfill'ом всего диапазона
  `[from_seq, to_seq]` (частичное заполнение разрыв НЕ лечит — алерт остаётся `active`).
  Полное закрытие → `DataGapHealed` (`payload: {healedSeq, gapRef, healedAt}`),
  алерт `resolved` + `resolvedAt`.
- **Replay**: base64-курсор `cursor:<lastId>`; чтение без курсора — с начала окна.
- **Backfill**: оригинальный `timestamp` сохраняется, `observedAt` — текущее; события
  старше retention принимаются, но попадают под прайнинг.
- **Retention**: события старше **30 дней** удаляются, агрегаты держатся 180 дней.
  Прайнинг **не** выполняется при старте сервера (ADR-0005): отдельная команда
  `python3 site/factory/watchtower_exporter.py --no-detectors --prune` пишет
  машиночитаемый отчёт `site/data/prune-report.json` (в git не коммитится).
  Регламент и остальные команды — в `docs/SLO_TRAFFICGEN.md` §5.

---

## 7. Метрики и воронка

### `GET /watchtower/metrics/daily?period=Nd`

Непрерывная серия дней (дни без трафика возвращаются с нулями) + `totals`:

- `pageViews`, `sessions`, `uniquePseudoVisitors` (+ `visitorsByType real/bot/hybrid`),
- `sessionDurationSeconds {avg, p50, p95, estimate, sampleSize, explicitCompletions,
  minSessionsForExact}` — точная длительность берётся из `SessionEnded`/
  `SessionAbandoned` (при наличии `payload.durationSeconds` — из него); пока
  явных завершений меньше `MIN_SESSIONS_FOR_DURATION` (30), это оценка
  first→last event и `estimate: true` (ADR-0004). Порог достигнут — флаг снимается
  сам, вручную его не переключают;
- `bounceRate` (сессии ровно с 1 событием), `ctaClickRate = CTA/PageView`,
  `landingReachedRate = Landing/CTA` — `null`/0 при нулевом знаменателе,
- `events {total, byType}`, `errors {rejectedSchema, rejectedPii, rejectedUnknownType,
  rejectedTimestamp, rateLimited, paused, blocked, trafficErrors}` — **по дням**
  (таблица `counters_daily`, ADR-0006);
- `integrity {duplicates, rejected, accepted, dataGaps, dataGapsHealed}` — тоже по дням;
- `systemEventsExcluded` — системные сессии (`sess_system*`) не входят в
  пользовательскую статистику: иначе бот-трафик и bounce-rate раздуваются
  событиями детекторов;
- `breakdowns {byCampaign, bySource, byPage}` на каждый день и в totals;
- раздельно `trafficType {real, bot, hybrid}` — бот-трафик нигде не смешивается с real.

### `GET /watchtower/funnels?period=Nd`

Ступени `CampaignStarted → SessionStarted → PageView → CTAClicked → LandingReached`:

- `count` — только реально принятые события (R3: подстановка `len(CAMPAIGNS_DEF)`
  **удалена** — была и отмечена как нарушение);
- `conversionFromPrev`/`conversionFromFirst`/`dropOffRate` — `null` при нулевом
  знаменателе (Watchtower отрисует прочерк, не 0% и не 100%);
- `LandingReached` помечена `stageUnavailable: true` (нет эмиттера);
- разрезы `byCampaign` и `bySource` обязательны и возвращаются;
- `acquisition` — сквозная воронка привлечения `ad_click → visit → page_view →
  identity_bound → first_action → retained` (retained = сессии с активностью в
  ≥ 2 разных днях). Шаг `ad_click` помечен `available: false`: реального клика по
  объявлению мы не видим, это верхняя граница, а не факт клика.

### Prometheus (`GET /watchtower/metrics`, text 0.0.4)

`trafficgen_events_total{source_type}`, `trafficgen_events_duplicate_total`,
`trafficgen_events_rejected_total{reason="schema|pii|unknown_type"}`,
`trafficgen_events_unknown_type_total{event_type}` (cap 20 меток, дальше `__other__`),
`trafficgen_delivery_failures_total`, `trafficgen_exporter_errors_total`,
`trafficgen_rate_limited_total`, `trafficgen_buffer_depth` (gauge),
`trafficgen_data_gaps_total`, `trafficgen_data_gaps_healed_total`.

- Счётчики переживают перезапуск: производные пересчитываются из `events`, остальные
  персистятся в `metrics_state` write-through. Проверено тестом (15).
- `trafficgen_buffer_depth = 0` — **константа по проекту**: очереди/буфера в
  архитектуре нет (синхронный приём в SQLite), а не заглушка.

---

## 8. Безопасность, PII, consent, bot-маркировка

1. **Read-only**: любой пишущий метод к `/watchtower/*` → `405` + `Allow`
   (смок-проверено для POST/PUT/DELETE/PATCH на ключевых маршрутах).
2. **PII**: `strip_pii()` до записи и до логов — ключи по расширенному списку,
   IPv4/IPv6/email/64-hex-литералы в значениях, wallet-подобные ключи, чувствительные
   query-параметры URL (utm_* сохраняются). Девиации от ТЗ зафиксированы и
   обоснованы в отчёте (голое `name` и публичные адреса пулов не чистятся —
   это не PII; персональные `firstName/lastName/username/login` — чистятся).
   Access-log не содержит query-строк и тел; полный лог — только отладочный env-флаг.
3. **Псевдонимизация**: `sess_<random>` (клиент) + серверная sha256-псевдонимизация
   несоответствующих `sessionId`. Cross-device связывание не выполняется.
4. **Consent/opt-out**: `?notrack=1` / `localStorage.tc_notrack=1` / DNT / GPC.
   Сервер: `DNT: 1` или `Sec-GPC: 1` на `/api/track` → `202 {"status":"opted_out"}`,
   в БД не пишется. См. `PRIVACY.md`.
5. **Секреты**: сканер `scripts/scan_secrets.py` (12 сигнатур, 279 текстовых файлов) —
   исторично чисто; включён в CI (джоб `watchtower-contract` в `factory.yml`) +
   pre-commit рекомендован. `WATCHTOWER_READ_TOKEN(_PREVIOUS)` — только через env,
   в репо встречается только имя (`.env.example` с пустыми значениями).
6. **Rate limit**: `/api/track` — token bucket 30 rps/burst 30 (env
   `WATCHTOWER_TRACK_RPS/_BURST`), при превышении `429 + Retry-After` и системное
   событие `RateLimited`.
7. **Bot-маркировка**: `sourceId: factory_pipeline` → принудительно `sourceType: bot`;
   `isBot: true` → `bot`. Синтетика (`payload.synthetic: true`) исключена из всех
   продуктовых агрегатов (метрики, воронка) но видна в потоке событий.
8. **Размер тела**: `/api/track` ограничен 256 KiB → `413`.

---

## 9. Локальный запуск и верификация

```bash
npm test                    # python3 scripts/test_watchtower.py      (17 контрактных тестов)
npm run test:max            # python3 scripts/test_watchtower_max.py  (20 тестов максимума)
npm run smoke               # python3 scripts/smoke_watchtower.py     (77 e2e проверок)
npm run scan                # python3 scripts/scan_secrets.py         (secret-сканер)
npm start                   # python3 site/factory/watchtower_exporter.py  → 0.0.0.0:8000

# отчёты и регламент (см. docs/SLO_TRAFFICGEN.md §5)
python3 scripts/slo_report.py                     # reports/slo-trafficgen.{json,md}
python3 scripts/load_test_traffic.py --report     # reports/load-test-trafficgen.json
python3 scripts/backup_restore.py --report        # reports/dr-trafficgen.json
python3 scripts/sbom.py                           # reports/sbom.json
python3 scripts/unit_economics.py                 # reports/unit-economics.json
TRAFFICGEN_API_BASE_URL=http://127.0.0.1:8000 node scripts/verify_hub_integration.mjs
```

При старте сервер: прогоняет реконсилёр каталогов (идемпотентно, первый запуск
запишет ~24 lifecycle-события), поднимает фоновые детекторы
(`TRAFFICGEN_DETECTORS=0` отключает) и HTTP на `0.0.0.0:8000`. Прайнинг
retention отдельной командой (`--prune`), в старт он не входит.
Для CI: job `watchtower-contract` в `.github/workflows/factory.yml` выполняет
scan → tests → test:max → smoke.

Реальные выводы прогонов — в отчёте об интеграции (уместилось в конце PROGRESS.md).
