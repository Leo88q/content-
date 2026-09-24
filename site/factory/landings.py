#!/usr/bin/env python3
"""Подтверждённый переход «CTA → игра» (LandingReached) через click-id round-trip.

Раньше событие `LandingReached` числился `unavailable` с причиной «нет подтверждения
перехода». Механизм такой:

1. Кликабельная ссылка строится через `/r/<clickId>?to=<target>` (см. `cta_href`).
2. При проходе через `/r/` клик регистрируется в этом журнале (clickId → контекст).
3. Игра (или её лендинг) присылает `LandingReached` с тем же `payload.clickId`
   через обычный `POST /api/track`.
4. Событие принимается ТОЛЬКО если clickId уже зарегистрирован. Иначе —
   `rejected: landing_unconfirmed`.

Так воронка `… → CTAClicked → LandingReached` замыкается по факту, а не по оценке:
выдумать подтверждение со стороны экспортёра физически невозможно, журнал хранит
только псевдонимные идентификаторы (IP/кошелёк/utm-значения в него не пишутся).

Хранилище: отдельный SQLite (`site/data/landings.sqlite3`), чтобы не смешивать с
событийной базой и не мешать её retention/прайнингу.
"""
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone

CLICK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
LEDGER_TTL_DAYS = int(os.environ.get("TRAFFICGEN_LANDINGS_TTL_DAYS", "30"))

# Куда разрешено вести редирект. Только свои целевые страницы: произвольный
# внешний URL в параметре — это open-redirect, поэтому его здесь нет.
DEFAULT_TARGETS = {
    "target_terminal": "#/terminal",
    "target_sixsec": "#/sixsec",
    "target_duel": "#/duel",
    "target_crash": "#/crash",
    "target_quest": "#/quest",
    "target_tiplink_claim": "#/tiplink-claim",
}


def targets():
    """Целевые страницы: из env TALKCHART_LANDING_TARGETS (JSON), иначе дефолт-карта."""
    raw = os.environ.get("TALKCHART_LANDING_TARGETS")
    if not raw:
        return dict(DEFAULT_TARGETS)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return dict(DEFAULT_TARGETS)
    return {k: v for k, v in parsed.items() if isinstance(k, str) and isinstance(v, str)}


def valid_click_id(click_id):
    return isinstance(click_id, str) and bool(CLICK_ID_RE.match(click_id))


class LandingLedger:
    """Журнал кликов и подтверждений. Идемпотентен: повторный клик не дублирует строку."""

    def __init__(self, db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute(
                """CREATE TABLE IF NOT EXISTS landing_clicks (
                       click_id    TEXT PRIMARY KEY,
                       session_id  TEXT,
                       campaign_id TEXT,
                       source_id   TEXT,
                       page_id     TEXT,
                       target      TEXT,
                       created_at  TEXT NOT NULL,
                       confirmed_at TEXT
                   )"""
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_landing_created ON landing_clicks(created_at)")

    @contextmanager
    def _conn(self):
        """Соединение с commit/rollback И закрытием. Голый `with sqlite3.connect()`
        только коммитит, но не закрывает: каждое событие оставляло открытый файл, и на
        macOS (ulimit -n 256) экспортёр падал с «unable to open database file»."""
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def register_click(self, click_id, *, session_id=None, campaign_id=None,
                       source_id=None, page_id=None, target=None):
        if not valid_click_id(click_id):
            return {"status": "rejected", "reason": "bad_click_id"}
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO landing_clicks
                       (click_id, session_id, campaign_id, source_id, page_id, target, created_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(click_id) DO NOTHING""",
                (click_id, session_id, campaign_id, source_id, page_id, target, now),
            )
            if cur.rowcount == 0:  # уже зарегистрирован — подтверждение не переносим назад
                c.execute(
                    "UPDATE landing_clicks SET session_id=COALESCE(?, session_id),"
                    " campaign_id=COALESCE(?, campaign_id), source_id=COALESCE(?, source_id),"
                    " page_id=COALESCE(?, page_id), target=COALESCE(?, target) WHERE click_id=?",
                    (session_id, campaign_id, source_id, page_id, target, click_id),
                )
                return {"status": "already_registered", "clickId": click_id}
        return {"status": "registered", "clickId": click_id}

    def known(self, click_id):
        if not valid_click_id(click_id):
            return False
        with self._conn() as c:
            return c.execute("SELECT 1 FROM landing_clicks WHERE click_id=?", (click_id,)).fetchone() is not None

    def confirm(self, click_id, *, session_id=None, campaign_id=None, source_id=None, page_id=None):
        """Подтверждение перехода. Повторное подтверждение — уже `duplicate` (не портит счётчики)."""
        if not valid_click_id(click_id):
            return {"status": "rejected", "reason": "bad_click_id"}
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._conn() as c:
            row = c.execute("SELECT confirmed_at FROM landing_clicks WHERE click_id=?", (click_id,)).fetchone()
            if row is None:
                return {"status": "rejected", "reason": "landing_unconfirmed"}
            if row["confirmed_at"]:
                return {"status": "duplicate", "clickId": click_id}
            c.execute("UPDATE landing_clicks SET confirmed_at=? WHERE click_id=?", (now, click_id))
            c.execute(
                "UPDATE landing_clicks SET session_id=COALESCE(?, session_id),"
                " campaign_id=COALESCE(?, campaign_id), source_id=COALESCE(?, source_id),"
                " page_id=COALESCE(?, page_id) WHERE click_id=?",
                (session_id, campaign_id, source_id, page_id, click_id),
            )
        return {"status": "confirmed", "clickId": click_id, "confirmedAt": now}

    def stats(self):
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS clicks,"
                " SUM(CASE WHEN confirmed_at IS NOT NULL THEN 1 ELSE 0 END) AS confirmed"
                " FROM landing_clicks"
            ).fetchone()
        clicks = row["clicks"] or 0
        confirmed = row["confirmed"] or 0
        return {
            "clicks": clicks,
            "confirmed": confirmed,
            "pending": clicks - confirmed,
            # null вместо 0.0 при пустом знаменателе — конверсию не выдумываем
            "confirmationRate": round(confirmed / clicks, 4) if clicks else None,
        }

    def prune(self, keep_days=LEDGER_TTL_DAYS):
        cutoff = time.time() - keep_days * 86400
        stamp = datetime.fromtimestamp(cutoff, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._conn() as c:
            cur = c.execute("DELETE FROM landing_clicks WHERE created_at < ?", (stamp,))
            return cur.rowcount


def _payload_of(event):
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def gate_event(ledger, event):
    """Фильтр для LandingReached: без зарегистрированного clickId событие не принимается."""
    if not isinstance(event, dict) or event.get("eventType") != "LandingReached":
        return None
    click_id = _payload_of(event).get("clickId") or event.get("clickId")
    if not valid_click_id(click_id):
        return {"status": "rejected", "reason": "landing_unconfirmed",
                "message": "LandingReached требует payload.clickId (8–64 символа [A-Za-z0-9_-])"}
    if not ledger.known(click_id):
        return {"status": "rejected", "reason": "landing_unconfirmed",
                "message": f"clickId {click_id} не зарегистрирован — подтверждения перехода не было"}
    return None


def observe_event(ledger, event):
    """Регистрация клика и отметка подтверждения. Вызывается после приёма события."""
    if not isinstance(event, dict):
        return
    et = event.get("eventType")
    payload = _payload_of(event)
    click_id = payload.get("clickId") or event.get("clickId")
    ctx = dict(
        session_id=event.get("sessionId"),
        campaign_id=event.get("campaignId"),
        source_id=event.get("sourceId"),
        page_id=event.get("pageId"),
    )
    if et == "CTAClicked" and valid_click_id(click_id):
        ledger.register_click(click_id, **ctx)
    elif et == "LandingReached" and valid_click_id(click_id):
        ledger.confirm(click_id, **ctx)


def cta_href(base_url, target, click_id):
    """Ссылка CTA через редирект-подтверждение. target — из allowlist целей."""
    allowed = targets()
    if target not in allowed:
        raise ValueError(f"target {target!r} не в списке разрешённых: {sorted(allowed)}")
    return f"{base_url.rstrip('/')}/r/{click_id}?to={target}"
