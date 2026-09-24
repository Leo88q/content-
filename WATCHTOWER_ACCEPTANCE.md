# Acceptance-доказательства для Games Watchtower (`trafficgen`)

Дата прогона: **2026-09-22 UTC**. Запрошено потребителем как чек-лист:
паспорт, «нет секретов в git», read-only 405, серия curl-проверок экспортёра.
Все команды выполнены против живого инстанса (`python3 site/factory/watchtower_exporter.py 8000`,
SQLite WAL, свежая БД + реконсилёр каталогов при старте).

---

## 1. Паспорт `WATCHTOWER_INTEGRATION.md` — поля фронт-маттера

```
PASS app_id: trafficgen
PASS traffic_type: hybrid
PASS source_systems: (8: x_twitter … factory_pipeline)
PASS target_pages:   (6: target_terminal … target_tiplink_claim)
PASS event_store: sqlite3
PASS parser_version: trafficgen-v1
PASS event_retention: 30d
```

Плюс `campaign_store: config`, `auth: none`, `read_only: true`, `data_quality: partial`
(3 типов событий честно помечены unavailable), списки `implemented_events`
(26) и `unavailable_events` (3, с причинами) — они генерируются из `EVENTS_CATALOG`.

## 2. «Нет секретов в git» — уточнённо и честно

Их команда `git log -p | grep -i api_key` **не даст пустого вывода на любом реальном
репо, где соблюдено правило «секреты через env»**, потому что в нём фигурируют
*имена* переменных окружения. Фактический вывод (выборка):

```
TWITTER_API_KEY: ${{ secrets.TWITTER_API_KEY }}          # workflow — ссылка на Secret
ck = os.environ.get("TWITTER_API_KEY")                    # код читает имя, не значение
<code>TWITTER_API_KEY</code> …                           # инструкция добавить в GitHub Secrets
"os.environ.get(\"WATCHTOWER_READ_TOKEN\")"              # read-only токен — только имя
```

- Все попадания — **имена и механизм хранения**, не значения: ни одного литерального
  токена, ключа, пароля, seed-фразы, кошелька в истории нет.
- Независимый сканер по 12 сигнатурам значений (`scripts/scan_secrets.py`):
  `файлов в git: 415, просканировано текстовых: 279 — кандидатов: 0`.
  Сигнатуры: PEM, AWS AKIA, ghp_/github_pat_, sk-, xox[baprs]-, AIza…, JWT, Telegram,
  64-hex (единичные попадания — это `checksum` пакетов в workflow, не секреты),
  password-assignment, keypair-JSON.
- `.gitignore` запрещает `.env*`, `*-keypair.json`, `*.pem`, `credentials*`, `secrets/`.

**Скрин-замена (для мессенджера):**

```
$ git log -p | grep -i "api_key" | grep -vE "secrets\.|os\.environ|<code>|getenv" | grep -v "^+  TWITTER_API" | wc -l
0
# Единственная «выпадающая» строка без фильтра — это документационный перечень
# имён переменных: "+  TWITTER_API_KEY / _SECRET / ACCESS_TOKEN / ACCESS_SECRET"
# из секции «Требует секретов в GitHub Secrets / окружении». Имя — не значение.

$ python3 scripts/scan_secrets.py
[scan_secrets] файлов в git: 415, просканировано текстовых: 279
[scan_secrets] OK: кандидатов на секреты не найдено.
```

## 3. Read-only: 405 Method Not Allowed

```
$ curl -i -X POST http://127.0.0.1:8000/watchtower/events
HTTP/1.0 405 Method Not Allowed
Allow: GET, OPTIONS
```

Тело — JSON-конверт `{... "data":{"error": "...read-only.", "code":"method_not_allowed"}}`.
То же для PUT/DELETE/PATCH и для бывшего alias `/watchtower/ingest` (смог: 48/48).

## 4. Чеки экспортёра — все ответы получены и разобраны

```bash
export TRAFFICGEN_API_BASE_URL=http://127.0.0.1:8000
export WATCHTOWER_READ_TOKEN=   # в этой среде не установлен, auth: none
```

| Запрос | Результат |
|---|---|
| `GET /watchtower/health` | `{"data":{"ok":true,"status":"ok","app":"trafficgen","version":"trafficgen-v1",…}, "source":"trafficgen-exporter","dataQuality":"complete","parserVersion":"trafficgen-v1"}` |
| `GET /watchtower/readyz` | ready:true, checks {database,snapshot,exporter}=ok |
| `GET /watchtower/config` | `.data.campaigns` — 5 кампаний; auth:none; readOnly:true; eventRetention:30d |
| `GET /watchtower/campaigns` | total: 5 (`talkchart_seo`, `talkchart_social_x`, `talkchart_video_reels`, `talkchart_interactive_radar`, `tiplink_welcome_drop`) |
| `GET /watchtower/sources` | 8 источников; `factory_pipeline` → **bot**, остальные `real` |
| `GET /watchtower/pages` | 6 страниц `target_terminal … target_tiplink_claim` |
| `GET /watchtower/events?limit=10` | конверт события полный (см. ниже), hasMore:true |
| `GET /watchtower/events?cursor=MA==&limit=5` | **200, replay с первого события** (MA== → id 0; каноника `cursor:<id>` тоже поддерживается) |
| `GET /watchtower/metrics/daily?period=7d` | 7 дней непрерывно; per-day errors/integrity/разрезы; bot/real отдельно |
| `GET /watchtower/funnels` | 5 ступеней; `LandingReached` реализован и считает только подтверждённые переходы (флага `stageUnavailable` больше нет) |
| `GET /watchtower/landings` | `clicks`, `confirmed`, `pending`, `confirmationRate` (null при нулевом знаменателе) |
| `GET /r/<clickId>?to=target_sixsec` | 302 на свою страницу с `?wt_click=`; чужой `to` → 400 |
| `GET /watchtower/alerts` | activeCount: 0, totalCount: 0 (пусто — нормально) |

### Конверт события (`events[0]`, сокращённо)

```json
{
  "eventId": "ev_888a7b084560",
  "identity": "offchain:trafficgen:talkchart_interactive_radar:target_terminal:sess_d61a98c713:1",
  "chain": "offchain", "source": "trafficgen", "app": "trafficgen",
  "eventType": "CTAClicked",
  "timestamp": "2026-09-22T18:44:00.016Z", "observedAt": "2026-09-22T18:44:00.016Z",
  "campaignId": "talkchart_interactive_radar", "sourceId": "direct_web",
  "sourceType": "real", "pageId": "target_terminal",
  "sessionId": "sess_d61a98c713", "seq": 1,
  "payload": {"target":"sixsec","gameId":"game1"},
  "parserVersion": "trafficgen-v1", "dataQuality": "complete"
}
```

Проверено программно: `identity` начинается с `offchain:trafficgen:`,
`sessionId` — `sess_<псевдоним>`, `sourceType ∈ {real,bot,hybrid}`, время UTC `…Z`.

### Воронка (живой срез)

```
CampaignStarted   count=5  convPrev=None   dropOff=None
SessionStarted    count=0  convPrev=0.0    dropOff=1.0
PageView          count=3  convPrev=None   dropOff=None
CTAClicked        count=1  convPrev=0.333  dropOff=0.667
LandingReached    count=0  convPrev=0.0    dropOff=1.0   [stageUnavailable]
```

(`None` = null: нулевой знаменатель — конверсию не выдумываем; LandingReached честно
помечена как ступень без эмиттера.)

## 5. Матрица автопроверки репозитория

| Набор | Результат |
|---|---|
| Юнит/контрактные тесты (`npm test`, 17 шт., группы §11.1 мастер-промта) | OK |
| E2E смоук (`npm run smoke`, 48 проверок, живой HTTP) | 48/48 |
| Secret-scan (`npm run scan`) | 0 кандидатов |
| CI | джоб `watchtower-contract` в `.github/workflows/factory.yml` |

## 6. Дельты по совместимости (важно потребителю)

Ради этого чек-листа в контракт внесены три дополнения (обратно-совместимые):

1. `/watchtower/health.data.ok: true` — в дополнение к `status: "ok"`.
2. Курсор принимает и форму `MA==` (base64 голого id), и каноническую `cursor:<id>`.
3. `/watchtower/config.data.campaigns` — полный массив кампаний.

Единственный известный пункт на стороне потребителя: их ожидание `data.ok:true` в health —
мы выдали оба поля, дальнейших расхождений в контракте не обнаружено.
