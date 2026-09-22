# PRIVACY — TalkChart Traffic Generator (`trafficgen`)

Документ описывает, что именно собирает слой телеметрии Games Watchtower (`POST /api/track`
→ `site/factory/watchtower_exporter.py`), что не собирается принципиально, сроки хранения
и способы отключиться. Требования источника: `PROMPT_TRAFFIC_GENERATOR_INTEGRATION.md` §10.

## Что собирается

Канонический off-chain конверт события:

- `eventId`, `identity` (`offchain:trafficgen:<campaignId>:<pageId>:<sessionId>:<seq>`),
  `eventType` (например `PageView`, `CTAClicked`, `SessionStarted`, `SessionEnded`),
  `timestamp`/`observedAt` (UTC), `campaignId`, `sourceId` (класс источника —
  `x_twitter`, `google_search`, …, **не полный URL**), `sourceType` (`real`/`bot`),
  `pageId` (из справочника `target_*`), `sessionId` (псевдоним), `seq`,
  `payload` (целевой слот игры, путь страницы, публичные адреса пулов Solana), `parserVersion`.

## Что НЕ собирается (и удаляется до записи на диск)

Функция `strip_pii()` вызывается **до** записи в SQLite и до логирования, рекурсивно
(включая вложенные объекты, массивы и JSON-строки) и удаляет/редактирует:

- IP-адреса (ключи `ip`, `x_forwarded_for`, … + IPv4/IPv6-литералы в значениях);
- email и телефоны (ключи + литералы в значениях);
- fingerprint / device_id / user-agent / cookies / auth-заголовки / токены;
- приватные ключи, seed-фразы, подписи, адреса кошельков пользователей
  (`walletAddress`, `signer`, 64-символьные hex-литералы);
- геолокацию, имена и логины людей;
- чувствительные query-параметры в URL значений (`token`, `sig`, `email`, `phone`,
  `fbclid`, `gclid`, …) — `utm_*` сохраняется, это агрегированная маркетинговая разметка.

Публичные **on-chain** адреса пулов ликвидности PII не являются (они видны всем в сети
Solana) и сохраняются как `payload.poolAddress`.

## Псевдонимизация

- Посетитель идентифицируется только как `sess_<random>` (localStorage-ключ `tc_session_id`),
  случайный, без привязки к устройству/email/кошельку; cross-device-графы не строятся.
- Если клиент прислал `sessionId` не формы `sess_…`, сервер **детерминированно
  псевдонимизирует** его через `sha256` до `sess_<hash[:10]>`: исходное значение
  в хранилище не попадает, а идемпотентность replay сохраняется.

## Сроки хранения

- Сырые события: **30 дней** (автоматический retention-прайнинг при старте экспортёра,
  `EVENT_RETENTION_DAYS`).
- Дневные агрегаты (если материализуются): до 180 дней, события в них агрегированы
  без `sessionId`.

## Consent / Opt-out

Отказаться от телеметрии можно любым из способов (два независимых механизма):

1. **Параметр/флаг в клиенте** (`site/app.js`, функция `trackingOptOut()`):
   - `?notrack=1` в URL — немедленно прекращает отправку и запоминает выбор в
     `localStorage.tc_notrack=1`; `?notrack=0` — отменяет.
   - вручную: `localStorage.setItem("tc_notrack", "1")`.
2. **Браузерные сигналы**: сервер `POST /api/track` не принимает события с заголовком
   `DNT: 1` или `Sec-GPC: 1` (ответ `202 {"status":"opted_out"}`, в БД ничего не пишется).
   Клиент дополнительно уважает `navigator.doNotTrack` и `navigator.globalPrivacyControl`.

Opt-out не ретроактивен: уже собранные псевдонимные события остаются в рамках их
30-дневного срока хранения (полный `/watchtower/events` для удаления по sessionId
не предназначен — наружу доступен только read-only API; внутренний удаление по
`sessionId` выполняется прямым SQL-запросом к `site/data/watchtower.db`).

## Доступ к данным

- `/watchtower/*` — строго read-only (любой пишущий метод → `405`).
- Опционально чтение ограничивается токеном `WATCHTOWER_READ_TOKEN`
  (Bearer, сравнение constant-time; значение живёт только в секрет-менеджере,
  см. `.env.example`).

## Bot-трафик

События внутренней автоматики (`factory_pipeline`, GitHub Actions) всегда маркируются
`sourceType: "bot"` и считаются отдельно от реальных посетителей во всех агрегатах.
Синтетические события (`payload.synthetic: true`, смоук-тесты) исключаются из
продуктовых метрик и воронки.
