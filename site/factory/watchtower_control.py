#!/usr/bin/env python3
"""Контроль-плейн trafficgen: proposal → подтверждение → применение → откат.

Почему отдельный модуль и отдельный URL-префикс
----------------------------------------------
`/watchtower/*` — строго read-only контракт (любой пишущий метод → 405 + Allow).
Управление ломать этот контракт не должно: ни хаб, ни игры не пишут в трафик-
генератор напрямую, поэтому все изменяющие операции живут под `/api/control/*`,
а наружу отдаются только чтения (`/watchtower/proposals`, `/watchtower/audit`,
`/watchtower/blocks`, `/watchtower/consent`, `/watchtower/identity`,
`/watchtower/forensics`).

Инварианты, которые здесь защищены
----------------------------------
1. **Одного человека недостаточно.** Создавший предложение не может его
   подтвердить; первое и второе подтверждение — разные пользователи.
2. **2FA обязателен** на каждом подтверждении (TOTP RFC 6238, окно ±1 шаг);
   повторное использование кода в пределах шага запрещено.
3. **Никаких автоматических действий.** Детектор может только *предложить*
   (`suggestedBy`), применить — только человек. Блокировка человека без
   approval физически невозможна: другого пути записи в `blocks` нет.
4. **Любое применение обратимо**: до применения сохраняется `prev_state`,
   rollback восстанавливает его и пишет отдельную запись в audit.
5. **Audit — хеш-цепочка** (как в moderation backend'е SixSec): правка задним
   числом ломает цепочку, а не остаётся незамеченной.
6. **Секреты только из ENV.** Пользователи и TOTP-секреты — из
   `TRAFFICGEN_CONTROL_USERS`; в БД не хранится ни токенов, ни секретов.

Если `TRAFFICGEN_CONTROL_USERS` не задан, контроль-плейн честно недоступен:
эндпоинты отвечают 503 с `dataQuality: unavailable` и причиной, а не «работают
без проверки».
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import contextlib
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

try:
    import config
except ImportError:  # запуск из другого каталога
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config

DB_PATH = os.path.join(config.DATA_DIR, "watchtower.db")

PROPOSAL_TTL_SECONDS = int(os.environ.get("TRAFFICGEN_PROPOSAL_TTL_SECONDS", "1800"))
TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1  # ±1 шаг = ±30 с

# Словарь тяжести инцидентов (контур i-10): один на экосистему, а не «на глаз».
SEVERITY_DICTIONARY = {
    "p1": {
        "responseMinutes": 5,
        "description": "Данные недоступны или потеряны: экспортер не отвечает, разрыв в приёме событий, пауза кампании.",
        "escalation": "дежурный →lead студии немедленно",
        "examples": ["DataGapDetected (не закрыт >15 мин)", "EmergencyPause", "ExporterHealth(status=unhealthy)"],
    },
    "p2": {
        "responseMinutes": 30,
        "description": "Качество данных или аномалия, влияющая на решения: всплеск ботов, аномальный трафик, массовые отказы.",
        "escalation": "дежурный в рабочее время",
        "examples": ["AnomalyDetected(score>threshold)", "BotFlagged(share>50%)", "RateLimited(длительно)"],
    },
    "p3": {
        "responseMinutes": 240,
        "description": "Фоновое наблюдение: единичные отказы, деградация без влияния на отчётность.",
        "escalation": "тикет, разбор на недельном ревью",
        "examples": ["TrafficError (единичный)", "ExporterHealth(status=degraded)"],
    },
}

EVENT_SEVERITY = {
    "DataGapDetected": "p2",
    "DataGapHealed": "p3",
    "EmergencyPause": "p1",
    "AbuseBlocked": "p2",
    "AnomalyDetected": "p2",
    "BotFlagged": "p3",
    "RateLimited": "p2",
    "TrafficError": "p3",
    "ExporterHealth": "p2",
    "SessionAbandoned": "p3",
    "NavigationCompleted": "p3",
}


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------------------
# TOTP (RFC 6238) — без внешних зависимостей
# ---------------------------------------------------------------------------
def _b32_decode(secret: str) -> bytes:
    pad = "=" * (-len(secret) % 8)
    return base64.b32decode(secret.upper() + pad, casefold=True)


def totp_code(secret: str, ts: float | None = None, step: int = TOTP_STEP_SECONDS) -> str:
    ts = time.time() if ts is None else ts
    counter = int(ts) // step
    key = _b32_decode(secret)
    msg = counter.to_bytes(8, "big")
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return str(code % (10 ** TOTP_DIGITS)).zfill(TOTP_DIGITS)


def totp_verify(secret: str, code: str, ts: float | None = None) -> bool:
    """Постоянное по времени сравнение с окном ±1 шаг."""
    if not secret or not code or len(str(code)) != TOTP_DIGITS:
        return False
    ts = time.time() if ts is None else ts
    provided = str(code).encode()
    for delta in range(-TOTP_WINDOW, TOTP_WINDOW + 1):
        candidate = totp_code(secret, ts + delta * TOTP_STEP_SECONDS).encode()
        if hmac.compare_digest(provided, candidate):
            return True
    return False


# ---------------------------------------------------------------------------
# Пользователи контроля (только ENV)
# ---------------------------------------------------------------------------
def control_users() -> dict:
    raw = os.environ.get("TRAFFICGEN_CONTROL_USERS")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def authenticate(token: str):
    """Возвращает (user, role, secret) или None. Сравнение — constant-time."""
    if not token:
        return None
    provided = token.encode()
    for user, spec in control_users().items():
        expected = str((spec or {}).get("token", "")).encode()
        if expected and hmac.compare_digest(provided, expected):
            return user, (spec or {}).get("role", "viewer"), (spec or {}).get("secret", "")
    return None


def control_enabled() -> bool:
    return bool(control_users())


# ---------------------------------------------------------------------------
# Хранилище контроля
# ---------------------------------------------------------------------------
class ControlStore:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.init_db()

    @contextlib.contextmanager
    def get_conn(self):
        """`with store.get_conn() as conn:` — commit/rollback и ЗАКРЫТИЕ соединения.
        Раньше возвращался голый sqlite3.Connection: его `with` коммитит, но не закрывает,
        и на macOS (ulimit -n 256) тесты/экспортёр упирались в «unable to open database file»."""
        import sqlite3
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            with conn:
                yield conn
        finally:
            conn.close()

    def init_db(self):
        with self.get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT,
                    suggested_by TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    approvals TEXT NOT NULL DEFAULT '[]',
                    prev_state TEXT,
                    effect TEXT,
                    applied_at TEXT,
                    applied_by TEXT,
                    rolled_back_at TEXT,
                    rolled_back_by TEXT
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    object_type TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    details TEXT,
                    prev_hash TEXT NOT NULL,
                    hash TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS blocks (
                    id TEXT PRIMARY KEY,
                    target_type TEXT NOT NULL,
                    target_value TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    proposal_id TEXT
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS control_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_by TEXT
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS consent_registry (
                    subject_hash TEXT PRIMARY KEY,
                    decision TEXT NOT NULL,
                    source TEXT NOT NULL,
                    version TEXT NOT NULL,
                    decided_at TEXT NOT NULL,
                    details TEXT
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS identity_bindings (
                    session_id TEXT NOT NULL,
                    wallet_hash TEXT,
                    external_id_hash TEXT,
                    first_action_at TEXT,
                    campaign_id TEXT,
                    page_id TEXT,
                    bound_at TEXT NOT NULL,
                    PRIMARY KEY (session_id)
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS detector_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    detector TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    score REAL,
                    threshold REAL,
                    decision TEXT NOT NULL,
                    label TEXT,
                    labeled_at TEXT,
                    decided_at TEXT NOT NULL,
                    details TEXT
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS totp_used (
                    scope TEXT NOT NULL,
                    code_hash TEXT NOT NULL,
                    ts REAL NOT NULL,
                    PRIMARY KEY (scope, code_hash)
                );
            """)
            conn.commit()

    # --- audit ---------------------------------------------------------------
    def _last_hash(self, conn) -> str:
        row = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        return row["hash"] if row else "0" * 64

    def audit(self, conn, actor, action, object_type, object_id, details=None):
        prev = self._last_hash(conn)
        ts = now_utc_iso()
        body = json.dumps(
            {"ts": ts, "actor": actor, "action": action,
             "object": f"{object_type}:{object_id}", "details": details or {}},
            sort_keys=True, ensure_ascii=False,
        )
        digest = hashlib.sha256((prev + body).encode("utf-8")).hexdigest()
        conn.execute(
            "INSERT INTO audit_log (ts, actor, action, object_type, object_id, details, prev_hash, hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, actor, action, object_type, object_id,
             json.dumps(details or {}, ensure_ascii=False), prev, digest),
        )
        return digest

    def verify_audit_chain(self):
        with self.get_conn() as conn:
            rows = conn.execute(
                "SELECT id, ts, actor, action, object_type, object_id, details, prev_hash, hash "
                "FROM audit_log ORDER BY id ASC").fetchall()
        expected_prev = "0" * 64
        for r in rows:
            if r["prev_hash"] != expected_prev:
                return {"valid": False, "brokenAtId": r["id"], "entries": len(rows)}
            body = json.dumps(
                {"ts": r["ts"], "actor": r["actor"], "action": r["action"],
                 "object": f"{r['object_type']}:{r['object_id']}",
                 "details": json.loads(r["details"] or "{}")},
                sort_keys=True, ensure_ascii=False,
            )
            expected_prev = hashlib.sha256((r["prev_hash"] + body).encode("utf-8")).hexdigest()
            if expected_prev != r["hash"]:
                return {"valid": False, "brokenAtId": r["id"], "entries": len(rows)}
        return {"valid": True, "brokenAtId": None, "entries": len(rows)}

    def recent_audit(self, limit=100):
        with self.get_conn() as conn:
            rows = conn.execute(
                "SELECT id, ts, actor, action, object_type, object_id, details, prev_hash, hash "
                "FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # --- TOTP replay protection ---------------------------------------------
    def _consume_totp(self, conn, scope: str, code: str) -> bool:
        code_hash = hashlib.sha256(f"{scope}:{code}".encode()).hexdigest()
        try:
            conn.execute("INSERT INTO totp_used (scope, code_hash, ts) VALUES (?, ?, ?)",
                         (scope, code_hash, time.time()))
            conn.commit()
            return True
        except Exception:
            return False

    # --- proposals -----------------------------------------------------------
    def create_proposal(self, kind, payload, actor, role, reason=None, suggested_by=None):
        if role not in ("proposer", "approver", "admin"):
            return None, {"code": "forbidden", "message": "роль не может создавать предложения"}
        if kind not in PROPOSAL_KINDS:
            return None, {"code": "unknown_kind",
                          "message": f"неизвестный тип предложения: {kind}"}
        ok, err = PROPOSAL_KINDS[kind]["validate"](payload or {})
        if not ok:
            return None, {"code": "invalid_payload", "message": err}

        pid = f"prop_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)
        with self.get_conn() as conn:
            conn.execute(
                "INSERT INTO proposals (id, kind, payload, status, reason, suggested_by, "
                "created_by, created_at, expires_at, approvals) VALUES (?, ?, ?, 'proposed', ?, ?, ?, ?, ?, '[]')",
                (pid, kind, json.dumps(payload or {}, ensure_ascii=False), reason, suggested_by,
                 actor, now_utc_iso(),
                 (now + timedelta(seconds=PROPOSAL_TTL_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")),
            )
            self.audit(conn, actor, "proposal.create", "proposal", pid,
                       {"kind": kind, "payload": payload, "reason": reason,
                        "suggestedBy": suggested_by})
            conn.commit()
        return self.get_proposal(pid), None

    def get_proposal(self, pid):
        with self.get_conn() as conn:
            row = conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
        return self._proposal_view(row) if row else None

    @staticmethod
    def _proposal_view(row):
        d = dict(row)
        d["payload"] = json.loads(row["payload"] or "{}")
        d["approvals"] = json.loads(row["approvals"] or "[]")
        d["prevState"] = json.loads(row["prev_state"]) if row["prev_state"] else None
        d["effect"] = json.loads(row["effect"]) if row["effect"] else None
        return d

    def list_proposals(self, status=None, limit=50):
        with self.get_conn() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM proposals WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM proposals ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._proposal_view(r) for r in rows]

    def _expire_if_needed(self, conn, row):
        if row["status"] in ("applied", "rolled_back", "rejected", "expired"):
            return row
        if datetime.strptime(row["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc) < datetime.now(timezone.utc):
            conn.execute("UPDATE proposals SET status='expired' WHERE id = ?", (row["id"],))
            self.audit(conn, "system", "proposal.expire", "proposal", row["id"],
                       {"reason": "истёк TTL подтверждения"})
            conn.commit()
            return conn.execute("SELECT * FROM proposals WHERE id = ?", (row["id"],)).fetchone()
        return row

    def approve(self, pid, actor, role, code, secret):
        """Первое подтверждение. Второе (confirm) применяет предложение."""
        if role not in ("approver", "admin"):
            return None, {"code": "forbidden", "message": "подтверждать могут только approver/admin"}
        with self.get_conn() as conn:
            row = conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
            if row is None:
                return None, {"code": "not_found", "message": "предложение не найдено"}
            row = self._expire_if_needed(conn, row)
            view = self._proposal_view(row)
            if view["status"] != "proposed":
                return None, {"code": "invalid_state",
                              "message": f"предложение в статусе {view['status']}, подтверждение невозможно"}
            if view["created_by"] == actor:
                return None, {"code": "self_approval_denied",
                              "message": "автор предложения не может его подтверждать"}
            if any(a.get("actor") == actor for a in view["approvals"]):
                return None, {"code": "duplicate_approval", "message": "этот пользователь уже подтвердил"}
            if not secret or not totp_verify(secret, code):
                return None, {"code": "invalid_2fa", "message": "неверный TOTP-код"}
            if not self._consume_totp(conn, f"approve:{pid}:{actor}", code):
                return None, {"code": "totp_replay", "message": "этот TOTP-код уже использован"}

            approvals = view["approvals"] + [{
                "actor": actor, "role": role, "at": now_utc_iso(), "stage": "approve",
            }]
            conn.execute("UPDATE proposals SET approvals = ?, status = 'approved' WHERE id = ?",
                         (json.dumps(approvals, ensure_ascii=False), pid))
            self.audit(conn, actor, "proposal.approve", "proposal", pid,
                       {"stage": "approve", "2fa": "totp"})
            conn.commit()
        return self.get_proposal(pid), None

    def confirm(self, pid, actor, role, code, secret, emit_fn=None):
        """Второе подтверждение другим пользователем → применение."""
        if role not in ("approver", "admin"):
            return None, {"code": "forbidden", "message": "подтверждать могут только approver/admin"}
        with self.get_conn() as conn:
            row = conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
            if row is None:
                return None, {"code": "not_found", "message": "предложение не найдено"}
            row = self._expire_if_needed(conn, row)
            view = self._proposal_view(row)
            if view["status"] != "approved":
                return None, {"code": "invalid_state",
                              "message": "второе подтверждение возможно только после первого"}
            if view["created_by"] == actor:
                return None, {"code": "self_approval_denied", "message": "автор не подтверждает своё предложение"}
            first = next((a for a in view["approvals"] if a.get("stage") == "approve"), None)
            if first and first.get("actor") == actor:
                return None, {"code": "two_person_rule",
                              "message": "нужны два разных подтверждающих (two-person rule)"}
            if not secret or not totp_verify(secret, code):
                return None, {"code": "invalid_2fa", "message": "неверный TOTP-код"}
            if not self._consume_totp(conn, f"confirm:{pid}:{actor}", code):
                return None, {"code": "totp_replay", "message": "этот TOTP-код уже использован"}

            prev_state = capture_state(conn, view["kind"], view["payload"])
            effect, err = apply_effect(conn, view["kind"], view["payload"], actor, pid)
            if err:
                return None, err

            approvals = view["approvals"] + [{
                "actor": actor, "role": role, "at": now_utc_iso(), "stage": "confirm",
            }]
            conn.execute(
                "UPDATE proposals SET approvals = ?, status = 'applied', prev_state = ?, "
                "effect = ?, applied_at = ?, applied_by = ? WHERE id = ?",
                (json.dumps(approvals, ensure_ascii=False),
                 json.dumps(prev_state, ensure_ascii=False),
                 json.dumps(effect, ensure_ascii=False), now_utc_iso(), actor, pid))
            self.audit(conn, actor, "proposal.apply", "proposal", pid,
                       {"kind": view["kind"], "effect": effect, "2fa": "totp"})
            conn.commit()

        if emit_fn:
            emit_fn(view["kind"], view["payload"], effect, pid, actor)
        return self.get_proposal(pid), None

    def rollback(self, pid, actor, role, code, secret, emit_fn=None, reason=None):
        if role not in ("approver", "admin"):
            return None, {"code": "forbidden", "message": "откатывать могут только approver/admin"}
        with self.get_conn() as conn:
            row = conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
            if row is None:
                return None, {"code": "not_found", "message": "предложение не найдено"}
            view = self._proposal_view(row)
            if view["status"] != "applied":
                return None, {"code": "invalid_state", "message": "откатывать можно только применённое"}
            if not secret or not totp_verify(secret, code):
                return None, {"code": "invalid_2fa", "message": "неверный TOTP-код"}
            if not self._consume_totp(conn, f"rollback:{pid}:{actor}", code):
                return None, {"code": "totp_replay", "message": "этот TOTP-код уже использован"}

            revert_effect(conn, view["kind"], view["payload"], view["prevState"] or {}, actor, pid)
            conn.execute(
                "UPDATE proposals SET status = 'rolled_back', rolled_back_at = ?, "
                "rolled_back_by = ? WHERE id = ?",
                (now_utc_iso(), actor, pid))
            self.audit(conn, actor, "proposal.rollback", "proposal", pid,
                       {"kind": view["kind"], "restored": view["prevState"], "reason": reason})
            conn.commit()
        if emit_fn:
            emit_fn(view["kind"], view["payload"], {"rolledBack": True}, pid, actor)
        return self.get_proposal(pid), None

    # --- blocks ---------------------------------------------------------------
    def active_blocks(self):
        now = now_utc_iso()
        with self.get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM blocks WHERE active = 1 AND (expires_at IS NULL OR expires_at > ?) "
                "ORDER BY created_at DESC", (now,)).fetchall()
        return [dict(r) for r in rows]

    def is_blocked(self, target_type, target_value):
        now = now_utc_iso()
        with self.get_conn() as conn:
            row = conn.execute(
                "SELECT id FROM blocks WHERE active = 1 AND target_type = ? AND target_value = ? "
                "AND (expires_at IS NULL OR expires_at > ?) LIMIT 1",
                (target_type, target_value, now)).fetchone()
        return row is not None

    # --- pause state ----------------------------------------------------------
    def paused_campaigns(self):
        with self.get_conn() as conn:
            row = conn.execute("SELECT value FROM control_state WHERE key = 'paused_campaigns'").fetchone()
        if not row:
            return {}
        try:
            return json.loads(row["value"])
        except Exception:
            return {}

    def is_paused(self, campaign_id):
        return campaign_id in self.paused_campaigns()

    # --- consent (контур i-02) ------------------------------------------------
    def record_consent(self, subject_hash, decision, source, version="1", details=None):
        with self.get_conn() as conn:
            conn.execute("""
                INSERT INTO consent_registry (subject_hash, decision, source, version, decided_at, details)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(subject_hash) DO UPDATE SET decision=excluded.decision,
                    source=excluded.source, version=excluded.version,
                    decided_at=excluded.decided_at, details=excluded.details
            """, (subject_hash, decision, source, version, now_utc_iso(),
                  json.dumps(details or {}, ensure_ascii=False)))
            self.audit(conn, source, "consent.record", "subject", subject_hash[:12],
                       {"decision": decision, "version": version})
            conn.commit()
        return self.consent_state()

    def consent_state(self):
        with self.get_conn() as conn:
            rows = conn.execute("SELECT decision, COUNT(*) AS cnt FROM consent_registry GROUP BY decision").fetchall()
            last = conn.execute("SELECT MAX(decided_at) AS at FROM consent_registry").fetchone()
        return {
            "byDecision": {r["decision"]: r["cnt"] for r in rows},
            "lastDecisionAt": last["at"] if last else None,
            "propagation": "opt-out применяется немедленно во всех каналах приёма (DNT/GPC/параметр notrack)",
        }

    # --- late identity binding (d-06) ----------------------------------------
    def bind_identity(self, session_id, wallet_hash=None, external_id_hash=None,
                      first_action_at=None, campaign_id=None, page_id=None):
        with self.get_conn() as conn:
            conn.execute("""
                INSERT INTO identity_bindings
                (session_id, wallet_hash, external_id_hash, first_action_at, campaign_id, page_id, bound_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    wallet_hash=COALESCE(excluded.wallet_hash, identity_bindings.wallet_hash),
                    external_id_hash=COALESCE(excluded.external_id_hash, identity_bindings.external_id_hash),
                    first_action_at=COALESCE(excluded.first_action_at, identity_bindings.first_action_at)
            """, (session_id, wallet_hash, external_id_hash, first_action_at,
                  campaign_id, page_id, now_utc_iso()))
            conn.commit()

    def identity_stats(self):
        with self.get_conn() as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM identity_bindings").fetchone()["c"]
            with_wallet = conn.execute(
                "SELECT COUNT(*) AS c FROM identity_bindings WHERE wallet_hash IS NOT NULL").fetchone()["c"]
            with_ext = conn.execute(
                "SELECT COUNT(*) AS c FROM identity_bindings WHERE external_id_hash IS NOT NULL").fetchone()["c"]
            with_action = conn.execute(
                "SELECT COUNT(*) AS c FROM identity_bindings WHERE first_action_at IS NOT NULL").fetchone()["c"]
            wallets = conn.execute(
                "SELECT COUNT(DISTINCT wallet_hash) AS c FROM identity_bindings "
                "WHERE wallet_hash IS NOT NULL").fetchone()["c"]
        return {
            "sessions": total,
            "boundToWallet": with_wallet,
            "boundToExternalId": with_ext,
            "reachedFirstAction": with_action,
            "distinctWallets": wallets,
            "note": "Хранятся только sha256-хеши с солью из TRAFFICGEN_IDENTITY_SALT; сырых кошельков в БД нет.",
        }

    # --- решения детекторов (forensics, o-07) ---------------------------------
    def record_decision(self, detector, subject, score, threshold, decision, details=None):
        with self.get_conn() as conn:
            conn.execute(
                "INSERT INTO detector_decisions "
                "(detector, subject, score, threshold, decision, decided_at, details) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (detector, subject, score, threshold, decision, now_utc_iso(),
                 json.dumps(details or {}, ensure_ascii=False)))
            conn.commit()

    def label_decision(self, detector, subject, label):
        with self.get_conn() as conn:
            conn.execute(
                "UPDATE detector_decisions SET label = ?, labeled_at = ? "
                "WHERE detector = ? AND subject = ? AND label IS NULL",
                (label, now_utc_iso(), detector, subject))
            conn.commit()

    def detector_quality(self):
        with self.get_conn() as conn:
            rows = conn.execute("""
                SELECT detector, decision, label, COUNT(*) AS c
                FROM detector_decisions GROUP BY detector, decision, label
            """).fetchall()
        out = {}
        for r in rows:
            d = out.setdefault(r["detector"], {"decisions": 0, "labeled": 0,
                                               "truePositive": 0, "falsePositive": 0,
                                               "trueNegative": 0, "falseNegative": 0})
            d["decisions"] += r["c"]
            if r["label"] is None:
                continue
            d["labeled"] += r["c"]
            positive = r["decision"] in ("flagged", "anomaly")
            truth = r["label"] in ("bot", "anomaly")
            if positive and truth:
                d["truePositive"] += r["c"]
            elif positive and not truth:
                d["falsePositive"] += r["c"]
            elif not positive and truth:
                d["falseNegative"] += r["c"]
            else:
                d["trueNegative"] += r["c"]
        for name, d in out.items():
            tp, fp, fn = d["truePositive"], d["falsePositive"], d["falseNegative"]
            d["precision"] = round(tp / (tp + fp), 3) if (tp + fp) else None
            d["recall"] = round(tp / (tp + fn), 3) if (tp + fn) else None
            d["labeledShare"] = round(d["labeled"] / d["decisions"], 3) if d["decisions"] else None
        return out


# ---------------------------------------------------------------------------
# Эффекты предложений
# ---------------------------------------------------------------------------
def _require(payload, key, kind):
    if not payload.get(key):
        return False, f"поле `{key}` обязательно для `{kind}`"
    return True, None


PROPOSAL_KINDS = {
    "emergency_pause": {
        "validate": lambda p: _require(p, "campaignId", "emergency_pause"),
    },
    "resume_campaign": {
        "validate": lambda p: _require(p, "campaignId", "resume_campaign"),
    },
    "block_source": {
        "validate": lambda p: _require(p, "sourceId", "block_source"),
    },
    "block_session": {
        "validate": lambda p: _require(p, "sessionId", "block_session"),
    },
    "unblock": {
        "validate": lambda p: (True, None) if (p.get("sourceId") or p.get("sessionId"))
        else (False, "нужен sourceId или sessionId"),
    },
    "campaign_upsert": {
        "validate": lambda p: _require(p, "campaign", "campaign_upsert"),
    },
}


def capture_state(conn, kind, payload):
    """Снимок состояния ДО применения — основа для отката."""
    if kind in ("emergency_pause", "resume_campaign"):
        row = conn.execute("SELECT value FROM control_state WHERE key = 'paused_campaigns'").fetchone()
        return {"paused_campaigns": json.loads(row["value"]) if row else {}}
    if kind in ("block_source", "block_session", "unblock"):
        rows = conn.execute("SELECT * FROM blocks WHERE active = 1").fetchall()
        return {"blocks": [dict(r) for r in rows]}
    if kind == "campaign_upsert":
        cid = (payload.get("campaign") or {}).get("id")
        row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (cid,)).fetchone()
        return {"campaign": dict(row) if row else None}
    return {}


def apply_effect(conn, kind, payload, actor, pid):
    now = datetime.now(timezone.utc)
    if kind == "emergency_pause":
        cid = payload["campaignId"]
        minutes = int(payload.get("durationMinutes", 120))
        until = (now + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = conn.execute("SELECT value FROM control_state WHERE key = 'paused_campaigns'").fetchone()
        state = json.loads(row["value"]) if row else {}
        state[cid] = {"pausedAt": now_utc_iso(), "until": until,
                      "reason": payload.get("reason"), "proposalId": pid, "by": actor}
        conn.execute(
            "INSERT INTO control_state (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, "
            "updated_by=excluded.updated_by",
            ("paused_campaigns", json.dumps(state, ensure_ascii=False), now_utc_iso(), actor))
        return {"pausedCampaign": cid, "until": until}, None

    if kind == "resume_campaign":
        cid = payload["campaignId"]
        row = conn.execute("SELECT value FROM control_state WHERE key = 'paused_campaigns'").fetchone()
        state = json.loads(row["value"]) if row else {}
        state.pop(cid, None)
        conn.execute(
            "INSERT INTO control_state (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, "
            "updated_by=excluded.updated_by",
            ("paused_campaigns", json.dumps(state, ensure_ascii=False), now_utc_iso(), actor))
        return {"resumedCampaign": cid}, None

    if kind in ("block_source", "block_session"):
        target_type = "source" if kind == "block_source" else "session"
        target_value = payload.get("sourceId") or payload.get("sessionId")
        minutes = int(payload.get("ttlMinutes", 1440))
        expires = (now + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        bid = f"blk_{uuid.uuid4().hex[:12]}"
        conn.execute(
            "INSERT INTO blocks (id, target_type, target_value, reason, created_at, expires_at, "
            "active, proposal_id) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (bid, target_type, target_value, payload.get("reason"), now_utc_iso(), expires, pid))
        return {"blockId": bid, "targetType": target_type, "target": target_value,
                "expiresAt": expires}, None

    if kind == "unblock":
        target_type = "source" if payload.get("sourceId") else "session"
        target_value = payload.get("sourceId") or payload.get("sessionId")
        conn.execute(
            "UPDATE blocks SET active = 0 WHERE active = 1 AND target_type = ? AND target_value = ?",
            (target_type, target_value))
        return {"unblocked": {"type": target_type, "value": target_value}}, None

    if kind == "campaign_upsert":
        camp = payload["campaign"]
        cid = camp.get("id")
        if not cid:
            return None, {"code": "invalid_payload", "message": "campaign.id обязателен"}
        row = conn.execute("SELECT id FROM campaigns WHERE id = ?", (cid,)).fetchone()
        conn.execute(
            "INSERT INTO campaigns (id, name, type, status, data) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, type=excluded.type, "
            "status=excluded.status, data=excluded.data",
            (cid, camp.get("name"), camp.get("type"), camp.get("status", "active"),
             json.dumps(camp, ensure_ascii=False)))
        return {"campaign": cid, "created" if row is None else "updated": True}, None

    return None, {"code": "unknown_kind", "message": kind}


def revert_effect(conn, kind, payload, prev_state, actor, pid):
    if kind in ("emergency_pause", "resume_campaign"):
        state = prev_state.get("paused_campaigns", {})
        conn.execute(
            "INSERT INTO control_state (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, "
            "updated_by=excluded.updated_by",
            ("paused_campaigns", json.dumps(state, ensure_ascii=False), now_utc_iso(), actor))
        return
    if kind in ("block_source", "block_session"):
        target_value = payload.get("sourceId") or payload.get("sessionId")
        target_type = "source" if kind == "block_source" else "session"
        conn.execute(
            "UPDATE blocks SET active = 0 WHERE active = 1 AND target_type = ? AND target_value = ?",
            (target_type, target_value))
        return
    if kind == "unblock":
        for b in prev_state.get("blocks", []):
            conn.execute(
                "INSERT OR REPLACE INTO blocks (id, target_type, target_value, reason, created_at, "
                "expires_at, active, proposal_id) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                (b["id"], b["target_type"], b["target_value"], b["reason"],
                 b["created_at"], b["expires_at"], b["proposal_id"]))
        return
    if kind == "campaign_upsert":
        prev = prev_state.get("campaign")
        cid = (payload.get("campaign") or {}).get("id")
        if prev is None:
            conn.execute("DELETE FROM campaigns WHERE id = ?", (cid,))
        else:
            conn.execute(
                "INSERT INTO campaigns (id, name, type, status, data) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, type=excluded.type, "
                "status=excluded.status, data=excluded.data",
                (prev["id"], prev["name"], prev["type"], prev["status"], prev["data"]))
        return


CONTROL = ControlStore()
