#!/usr/bin/env node
/**
 * Проверка переплетения с хабом (i-01): живой прогон адаптера Games Watchtower
 * против работающего экспортёра trafficgen.
 *
 * Что именно проверяется
 * ----------------------
 * 1. `/watchtower/health` через адаптер хаба → ok: true (контракт живой).
 * 2. `backfill()` — хаб вытягивает события курсорно и нормализует их;
 *    проверяется, что набор не пуст и все события валидны.
 * 3. Дедупликация на стороне хаба: повторный прогон того же окна не должен
 *    увеличивать число принятых событий (identity стабильна).
 * 4. **Дрейф каталога**: адаптер хаба содержит зашитый список
 *    `unavailableEvents`. Если приложение научилось эмитить событие, а хаб об
 *    этом не знает, это расхождение нужно показать явно — в отчёте оно
 *    попадает в `catalogDrift` и требует отдельного PR в хабе (этот репозиторий
 *    мы не меняем: он read-only по условию контура).
 *
 * Запуск
 * ------
 *   TRAFFICGEN_API_BASE_URL=http://127.0.0.1:8000 \
 *   HUB_PATH=/tmp/watchtower-hub node scripts/verify_hub_integration.mjs
 *
 * Отчёт: reports/hub-integration.json
 */
import { writeFileSync, mkdirSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = path.resolve(__dirname, '..')
const HUB_PATH = process.env.HUB_PATH || '/tmp/watchtower-hub'
const BASE_URL = process.env.TRAFFICGEN_API_BASE_URL || 'http://127.0.0.1:8000'
const OUT = path.join(REPO_ROOT, 'reports', 'hub-integration.json')

const hubUrl = (p) => pathToFileURL(path.join(HUB_PATH, p)).href

async function loadHub() {
  const provider = await import(hubUrl('server/ingestion/provider.js'))
  const adapters = await import(hubUrl('server/ingestion/game-adapters.js'))
  const inbox = await import(hubUrl('server/ingestion/event-inbox.js'))
  let analytics = null
  try {
    analytics = await import(hubUrl('server/analytics/traffic.js'))
  } catch (error) {
    analytics = null
  }
  return { provider, adapters, inbox, analytics }
}

async function appCatalog() {
  const response = await fetch(`${BASE_URL}/watchtower/config`)
  if (!response.ok) throw new Error(`/watchtower/config -> ${response.status}`)
  const body = await response.json()
  return body.data
}

function main() {
  return (async () => {
    const { provider, adapters, inbox, analytics } = await loadHub()
    const report = {
      generatedAt: new Date().toISOString(),
      hub: { path: HUB_PATH, readOnly: true },
      app: { baseUrl: BASE_URL },
      checks: [],
      catalogDrift: [],
      ok: false,
    }
    const check = (name, ok, details = null) => {
      report.checks.push({ name, ok: Boolean(ok), details })
      console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${name}${ok || !details ? '' : ` — ${JSON.stringify(details)}`}`)
    }

    const traffic = provider.createTrafficgenProvider({ TRAFFICGEN_API_BASE_URL: BASE_URL })

    // 1. health через адаптер хаба
    const health = await traffic.health()
    check('hub: /watchtower/health через адаптер -> ok:true',
      health?.data?.ok === true || health?.ok === true, health)

    // 2. backfill + валидность событий
    const page = await traffic.backfill({ limit: 100 })
    const events = page.events || []
    check('hub: backfill вернул события', page.ok && events.length > 0,
      { count: events.length })
    const invalid = events.filter((e) => !provider.validateEvent(e).valid)
    check('hub: все события прошли валидацию', invalid.length === 0,
      invalid.slice(0, 2).map((e) => provider.validateEvent(e).errors))

    // 3. дедупликация на стороне хаба (identity стабильна)
    const first = events.map((e) => inbox.ingest(e, 'trafficgen'))
    const acceptedFirst = first.filter((r) => r.accepted).length
    const second = events.map((e) => inbox.ingest(e, 'trafficgen'))
    const acceptedSecond = second.filter((r) => r.accepted).length
    check('hub: повторный приём тех же событий -> дубли, а не новые записи',
      acceptedSecond === 0 && second.filter((r) => r.duplicate).length === events.length,
      { events: events.length, acceptedFirst, acceptedSecond })
    report.inbox = { ...inbox.inboxStatus(), acceptedFirst, acceptedSecond }

    // 4. дрейф каталога: хаб считает событие недоступным, приложение — нет
    let catalog = null
    try {
      catalog = await appCatalog()
    } catch (error) {
      check('app: /watchtower/config доступен', false, String(error))
    }
    if (catalog) {
      const adapter = adapters.getGameAdapter('trafficgen', process.env)
        || adapters.gameAdapters(process.env).find((a) => a.adapterId === 'trafficgen')
      const hubUnavailable = new Set(
        (adapter?.unavailableEvents || []).map((e) => e.type || e.eventType || e))
      const implemented = new Set(catalog.implementedEvents || [])
      for (const type of hubUnavailable) {
        if (implemented.has(type)) {
          report.catalogDrift.push({
            eventType: type,
            app: 'implemented',
            hub: 'unavailable',
            action: 'нужен отдельный PR в Games-watchtower (хаб read-only для этого контура)',
          })
        }
      }
      check('каталог: расхождение с адаптером хаба зафиксировано явно', true,
        { hubUnavailable: [...hubUnavailable], drift: report.catalogDrift.length,
          hubImplementedCount: adapter?.implementedCount ?? null,
          appImplementedCount: (catalog.implementedEvents || []).length })
      report.catalog = {
        appImplemented: (catalog.implementedEvents || []).length,
        appUnavailable: (catalog.unavailableEvents || []).length,
        hubImplemented: adapter?.implementedCount ?? null,
        hubUnavailable: [...hubUnavailable],
      }
    }

    // 5. Честность хаб-аналитики: нулевой знаменатель -> null, а не 0.
    //    Хаб для нас read-only: расхождения фиксируем как caveats, а не как падения.
    report.hubSideCaveats = []
    if (analytics?.trafficAnalytics) {
      const ingested = inbox.list({ source: 'trafficgen', limit: 1000 })
      const analyticsReport = analytics.trafficAnalytics({ events: ingested })
      const landing = (analyticsReport.funnel || []).find((s) => s.step === 'LandingReached')
      // Важно: null ожидается, когда знаменатель (предыдущая ступень) равен 0.
      // Нулевой числитель при ненулевом знаменателе — честный 0, а не подмена.
      const zeroDenominatorHonest = (analyticsReport.funnel || [])
        .every((step, index, arr) => {
          if (index === 0) return true
          const prev = arr[index - 1]
          return prev.count === 0 ? step.conversionFromPrevious === null : true
        })
      check('хаб-аналитика: LandingReached помечен stageUnavailable',
        landing?.stageUnavailable === true, landing)
      check('хаб-аналитика: при нулевом знаменателе конверсия null',
        zeroDenominatorHonest, analyticsReport.funnel)
      const durationsEmpty = (analyticsReport.totals?.p50 === 0 && analyticsReport.totals?.avg === 0)
      if (durationsEmpty && !ingested.some((e) => e.eventType === 'SessionEnded')) {
        report.hubSideCaveats.push({
          metric: 'totals.p50/p95/avg',
          hub: 0,
          expected: null,
          note: 'хаб отдаёт 0 вместо null, когда завершённых сессий нет: ноль ' +
                'неотличим от реального нулевого значения. Правится в хабе ' +
                '(этот репозиторий read-only для данного контура).',
        })
      }
      if ((analyticsReport.unavailableMetrics || [])
        .some((m) => String(m.metric).includes('process-global'))) {
        report.hubSideCaveats.push({
          metric: 'unavailableMetrics[process-global error counters]',
          hub: 'заявлено как недоступное',
          expected: 'снять: счётчики в приложении стали per-day',
          note: 'статическая запись в хабе устарела после W2.',
        })
      }
      report.hubAnalytics = {
        dataQuality: analyticsReport.dataQuality,
        confidence: analyticsReport.confidence,
        totals: analyticsReport.totals,
        funnel: analyticsReport.funnel,
      }
    } else {
      report.hubSideCaveats.push({
        metric: 'trafficAnalytics',
        hub: 'модуль не найден',
        note: `не удалось импортировать server/analytics/traffic.js из ${HUB_PATH}`,
      })
    }

    report.ok = report.checks.every((c) => c.ok)
    mkdirSync(path.dirname(OUT), { recursive: true })
    writeFileSync(OUT, JSON.stringify(report, null, 2) + '\n')
    const failed = report.checks.filter((c) => !c.ok)
    console.log(`\nhub-integration: ${report.checks.length - failed.length}/${report.checks.length} проверок пройдено`)
    console.log(`отчёт: ${path.relative(REPO_ROOT, OUT)}`)
    if (report.catalogDrift.length) {
      console.log(`дрейф каталога: ${report.catalogDrift.length} событий (см. catalogDrift в отчёте)`)
    }
    if (report.hubSideCaveats.length) {
      console.log(`замечания к хаб-аналитике: ${report.hubSideCaveats.length} (см. hubSideCaveats)`)
    }
    return report.ok ? 0 : 1
  })()
}

process.exitCode = await main()
