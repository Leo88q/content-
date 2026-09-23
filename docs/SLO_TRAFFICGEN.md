# SLO и runbook trafficgen

Фактический отчёт генерируется скриптом `scripts/slo_report.py`
(`reports/slo-trafficgen.json` + `reports/slo-trafficgen.md`). Здесь — цели,
источники и порядок действий. Главное правило: **неизмеренное помечается
`unknown`, а не нулём и не сотней процентов.**

## 1. Цели и источники

| SLO | Цель | Источник факта |
|---|---|---|
| Availability экспортёра | ≥ 99.9 % за 30 дней | события `ExporterHealth` (поле `status`) за окно; если их нет — uptime процесса против окна |
| Свежесть данных | ≤ 300 с | `lagSeconds` из последнего `ExporterHealth` |
| Задержка приёма (ingest p99) | ≤ 2000 мс | БД: `observedAt − timestamp` по событиям окна |
| Задержка чтения API p95 | ≤ 300 мс | таблица `latency_samples` (замер в самом обработчике) |
| Задержка чтения API p99 | ≤ 1000 мс | там же |
| Потеря событий | 0 молча | accepted + duplicates + rejected + paused + blocked + 429 + сетевые ошибки = числу отправленных |

Исключения, которые не считаются задержкой приёма:

- **backfill** — события с `observedAt − timestamp > 1 часа` догружены позже,
  это не задержка живого приёма. Они считаются отдельно
  (`sources.backfillSamples`) и в процентиль не идут;
- **clock skew** — отрицательная разница (часы источника впереди) учитывается
  отдельным счётчиком.

## 2. Бюджет ошибок

При цели 99.9 % на 30 дней бюджет — 43 минуты простоя. Считается как
`1 − availability` против `0.001`. Превышение — не «красная метрика», а
сигнал: разбор причины и, при систематическом нарушении, пересмотр цели
вместе с владельцем (а не перекрашивание порога).

## 3. Словарь severity (p1 / p2 / p3)

| Класс | Признак | Реакция | События |
|---|---|---|---|
| **p1** | приём событий остановлен или данные расходятся | 15 мин, немедленно: пауза кампании через proposal | `TrafficError` массово, `ExporterHealth.status = unhealthy`, `DataGapDetected` без лечения > 1 ч |
| **p2** | деградация, приём продолжается | 60 мин | `AnomalyDetected` с \|z\| ≥ 5, `ExporterHealth.status = degraded`, свежесть > 300 с |
| **p3** | шум, качество данных, единичные отказы | 1 рабочий день | одиночный `BotFlagged`, `schema`-rejected всплеск < 1 % потока, `p95` API выше цели при `allGreen = false` |

Словарь отдаётся машиной: `GET /watchtower/severity`.

## 4. Runbook

### p1 — приём остановился

1. `GET /watchtower/health` — живой ли процесс; `GET /watchtower/readyz` — деградация.
2. `GET /watchtower/quality` — DLQ (`deadLetter.byReason`) и задержки.
   Причина `schema` массово → проверьте `parserVersion` отправителя.
3. `GET /watchtower/alerts` — открытые разрывы; разрыв, который не лечится
   больше часа, — p1.
4. Если приём надо остановить: **только через proposal**
   (`POST /api/control/proposals` → 2 подтверждения разными людьми + TOTP).
   Автоматической паузы не существует — это осознанное решение
   (см. `docs/DECISION_JOURNAL.md`, ADR-0002).

### p2 — аномалия трафика

1. `GET /watchtower/events?eventType=AnomalyDetected&limit=20` — сигналы
   детектора (медиана/MAD, `|z| ≥ 3.5`, метод `robust_zscore_mad_v1`).
2. `GET /watchtower/forensics` — качество детектора
   (`precisionProxy`, `recallProxy`). Метки-прокси не равны истине:
   человеческая разметка проставляется через `labelDecision()` и
   обязательно попадает в отчёт отдельной строкой.
3. Решение о блокировке источника — снова только через proposal
   (`block_source`), с обязательным TTL и причиной.

### p3 — качество данных

`GET /watchtower/quality` → DLQ; `GET /watchtower/audit` → цепочка аудита
(должна быть `valid: true`); `GET /watchtower/identity` → доля сессий с
привязанным внешним идентификатором.

## 5. Регламент

| Что | Как часто | Команда |
|---|---|---|
| Бэкап БД | каждые 15 мин (cron) | `python3 scripts/backup_restore.py --backup` |
| Проверка восстановления | каждое воскресенье | `python3 scripts/backup_restore.py --report` |
| Прайнинг retention | ежедневно | `python3 scripts/backup_restore.py --backup && python3 site/factory/watchtower_exporter.py --no-detectors --prune` |
| Отчёт SLO | еженедельно | `python3 scripts/slo_report.py` |
| Нагрузочный тест | после изменений приёма | `python3 scripts/load_test_traffic.py --events 2000 --concurrency 8 --report` |
| SBOM | при обновлении зависимостей | `python3 scripts/sbom.py` |
| Юнит-экономика | ежемесячно | `TRAFFICGEN_COST_INFRA=… python3 scripts/unit_economics.py` |
| Проверка интеграции с хабом | после изменения каталога | `TRAFFICGEN_API_BASE_URL=http://127.0.0.1:8000 node scripts/verify_hub_integration.mjs` |

Ретеншен: события хранятся 30 дней, агрегаты — 180. Прайнинг **не** выполняется
при старте сервера (это удлиняло старт и теряло отчёт): он вынесен в отдельную
команду с машиночитаемым отчётом `site/data/prune-report.json` (в git не
коммитится).

## 6. Нагрузка

`scripts/load_test_traffic.py` меряет не «сколько выдержит», а три свойства:
backpressure даёт `429` с `Retry-After` (а не тихо теряет события), DLQ растёт
только за счёт `schema`-rejected, задержка остаётся в бюджете. Фактический
прогон — `reports/load-test-trafficgen.json`.

## 7. DR

`scripts/backup_restore.py --verify` **действительно восстанавливает** копию в
отдельный файл и сравнивает с живой базой потаблично (число строк + sha256
содержимого), затем прогоняет `PRAGMA integrity_check`. Расхождение — ошибка
скрипта (exit 1), а не предупреждение. `rpoSeconds` в отчёте — честное окно
возможной потери; при бэкапе раз в 15 минут оно ≤ 900 с, но не «ноль».
