#!/usr/bin/env python3
"""
Requesty + Langfuse Analytics Collector
========================================
Sources:
  - Requesty API  -> requesty_daily  (cost, tokens, real request count)
  - Langfuse /traces -> lf_users      (export / by-user ; churn = Mongo messages)
  - Langfuse /observations -> model_daily (per-model breakdown)

Run locally:
  pip install -r requirements.txt
  # Optionnel churn enrich : .env avec WONKA_MONGO_URI (+ WONKA_MONGO_DB=wonkachat-prod)
  # Sync Langfuse users par morceaux (LF_TRACES_CHUNK_PAGES, LF_TRACES_SLEEP) pour ne pas surcharger l'API
  python collector.py --auto --port 7842   # clé via REQUESTY_KEY dans .env
  # ou : python collector.py --key ... --auto --port 7842
"""

import argparse, json, os, re, sqlite3, sys, time, threading, logging, socket
from datetime import datetime, timedelta, timezone

def utcnow():
    """Timezone-aware UTC datetime (replaces deprecated utcnow())."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
from pathlib import Path
from functools import wraps

import requests
from flask import Flask, jsonify, request, Response
from flask_cors import CORS

try:
    from dotenv import load_dotenv
    _here = Path(__file__).resolve().parent
    load_dotenv(_here / ".env")
    load_dotenv(_here.parent / ".env")
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_BASE      = "https://api-v2.requesty.ai"
DB_PATH       = Path("requesty_analytics.db")
PORT          = 7842
SYNC_INTERVAL = 3600  # 1h

LF_PUBLIC = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
LF_SECRET = os.environ.get("LANGFUSE_SECRET_KEY", "")
LF_HOST   = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("collector")

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
def init_db(path=DB_PATH):
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS requesty_daily (
            id          TEXT PRIMARY KEY,
            date        TEXT NOT NULL,
            key_id      TEXT,
            key_name    TEXT,
            completions INTEGER DEFAULT 0,
            input_tokens  INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            total_tokens  INTEGER DEFAULT 0,
            cost        REAL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_rd_date ON requesty_daily(date);

        CREATE TABLE IF NOT EXISTS model_daily (
            id            TEXT PRIMARY KEY,
            date          TEXT NOT NULL,
            model         TEXT,
            input_tokens  INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            total_tokens  INTEGER DEFAULT 0,
            cost          REAL DEFAULT 0,
            request_count INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_md_date  ON model_daily(date);
        CREATE INDEX IF NOT EXISTS idx_md_model ON model_daily(model);

        CREATE TABLE IF NOT EXISTS lf_users (
            user_id     TEXT PRIMARY KEY,
            first_seen  TEXT,
            last_seen   TEXT,
            total_traces INTEGER DEFAULT 0,
            updated_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS lf_sessions (
            session_id  TEXT PRIMARY KEY,
            user_id     TEXT,
            created_at  TEXT,
            trace_count INTEGER DEFAULT 0,
            updated_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    conn.commit()
    return conn

def set_meta(conn, k, v):
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, str(v)))
    conn.commit()

def get_meta(conn, k, default=None):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
    return r["value"] if r else default

# ---------------------------------------------------------------------------
# Wonka MongoDB — email + orgs pour les user_id = ObjectId Langfuse/Wonka
# ---------------------------------------------------------------------------
_mongo_client = None

def _mongo_churn_meta(configured=False, requested=0, matched=0, error=None):
    return {
        "configured": configured,
        "requested": requested,
        "matched": matched,
        "error": error,
    }


def _get_mongo_client():
    """Retourne (client, err). err est None si OK."""
    global _mongo_client
    uri = (os.environ.get("WONKA_MONGO_URI") or "").strip()
    if not uri:
        return None, "no_uri"
    try:
        from pymongo import MongoClient
    except ImportError:
        return None, "pymongo_missing"
    if _mongo_client is None:
        client_kw = {"serverSelectionTimeoutMS": 10000}
        try:
            import certifi
            client_kw["tlsCAFile"] = certifi.where()
        except ImportError:
            pass
        if os.environ.get("WONKA_MONGO_TLS_INSECURE", "").strip().lower() in ("1", "true", "yes"):
            client_kw["tlsAllowInvalidCertificates"] = True
        _mongo_client = MongoClient(uri, **client_kw)
    return _mongo_client, None


def _dt_iso_utc(dt):
    if not dt or not isinstance(dt, datetime):
        return str(dt or "")
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def wonka_churn_rows_from_messages():
    """
    Agrège la collection messages par user : first_seen, last_seen, total_traces (nombre de messages).
    Retourne (liste de dicts compatibles churn, meta).
    """
    client, mc_err = _get_mongo_client()
    if mc_err == "no_uri":
        return [], {**_mongo_churn_meta(False, 0, 0, None), "source": "mongodb_messages"}
    if mc_err == "pymongo_missing":
        return [], {
            **_mongo_churn_meta(True, 0, 0, "pymongo_missing"),
            "source": "mongodb_messages",
        }

    db_name = (os.environ.get("WONKA_MONGO_DB") or "wonkachat-prod").strip()
    db = client[db_name]
    match = {"user": {"$exists": True, "$ne": None}, "createdAt": {"$exists": True}}
    lb = (os.environ.get("WONKA_CHURN_MESSAGES_LOOKBACK_DAYS") or "").strip()
    if lb.isdigit() and int(lb) > 0:
        since = datetime.now(timezone.utc) - timedelta(days=int(lb))
        match["createdAt"] = {"$gte": since}
    if os.environ.get("WONKA_CHURN_USER_MESSAGES_ONLY", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        match["isCreatedByUser"] = True

    max_ms = int(os.environ.get("WONKA_CHURN_MAX_TIME_MS", "180000") or 180000)
    max_ms = max(30000, min(600000, max_ms))
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    days_since_monday = now.weekday()
    week_start = (now - timedelta(days=days_since_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    pipeline = [
        {"$match": match},
        {
            "$addFields": {
                "_uid": {
                    "$toLower": {
                        "$trim": {
                            "input": {
                                "$convert": {
                                    "input": "$user",
                                    "to": "string",
                                    "onError": "",
                                }
                            }
                        }
                    }
                }
            }
        },
        {"$match": {"_uid": {"$ne": ""}}},
        {
            "$group": {
                "_id": "$_uid",
                "first_seen": {"$min": "$createdAt"},
                "last_seen": {"$max": "$createdAt"},
                "total_traces": {"$sum": 1},
                "traces_this_week": {
                    "$sum": {"$cond": [{"$gte": ["$createdAt", week_start]}, 1, 0]}
                },
                "traces_this_month": {
                    "$sum": {"$cond": [{"$gte": ["$createdAt", month_start]}, 1, 0]}
                },
                "consumption": {"$sum": {"$ifNull": ["$cost", 0]}},
            }
        },
        {"$sort": {"last_seen": -1}},
    ]
    try:
        rows = []
        for doc in db.messages.aggregate(
            pipeline, allowDiskUse=True, maxTimeMS=max_ms
        ):
            uid = doc.get("_id")
            if not uid:
                continue
            rows.append(
                {
                    "user_id": uid,
                    "first_seen": _dt_iso_utc(doc.get("first_seen")),
                    "last_seen": _dt_iso_utc(doc.get("last_seen")),
                    "total_traces": int(doc.get("total_traces") or 0),
                    "traces_this_week": int(doc.get("traces_this_week") or 0),
                    "traces_this_month": int(doc.get("traces_this_month") or 0),
                    "consumption": float(doc.get("consumption") or 0),
                }
            )
        meta = {
            **_mongo_churn_meta(True, len(rows), len(rows), None),
            "source": "mongodb_messages",
            "users_from_messages": len(rows),
        }
        if lb.isdigit():
            meta["lookback_days"] = int(lb)
        log.info("Churn Mongo messages: %s utilisateurs agrégés", len(rows))
        return rows, meta
    except Exception as e:
        log.warning("Churn messages aggregate: %s", e)
        return [], {
            **_mongo_churn_meta(True, 0, 0, str(e)[:280]),
            "source": "mongodb_messages",
        }


def wonka_messages_per_user_in_days(days: int):
    """
    Messages Mongo par utilisateur sur les N derniers jours (createdAt >= since).
    Même filtres optionnels que le churn (WONKA_CHURN_USER_MESSAGES_ONLY).
    """
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(days, 365))
    client, mc_err = _get_mongo_client()
    if mc_err == "no_uri":
        return [], {**_mongo_churn_meta(False, 0, 0, None), "error": "no_uri", "period_days": days}
    if mc_err == "pymongo_missing":
        return [], {**_mongo_churn_meta(True, 0, 0, "pymongo_missing"), "period_days": days}

    db_name = (os.environ.get("WONKA_MONGO_DB") or "wonkachat-prod").strip()
    db = client[db_name]
    since = datetime.now(timezone.utc) - timedelta(days=days)
    match = {"user": {"$exists": True, "$ne": None}, "createdAt": {"$gte": since}}
    if os.environ.get("WONKA_CHURN_USER_MESSAGES_ONLY", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        match["isCreatedByUser"] = True

    max_ms = int(os.environ.get("WONKA_CHURN_MAX_TIME_MS", "180000") or 180000)
    max_ms = max(30000, min(600000, max_ms))
    pipeline = [
        {"$match": match},
        {
            "$addFields": {
                "_uid": {
                    "$toLower": {
                        "$trim": {
                            "input": {
                                "$convert": {
                                    "input": "$user",
                                    "to": "string",
                                    "onError": "",
                                }
                            }
                        }
                    }
                }
            }
        },
        {"$match": {"_uid": {"$ne": ""}}},
        {
            "$group": {
                "_id": "$_uid",
                "first_seen": {"$min": "$createdAt"},
                "last_seen": {"$max": "$createdAt"},
                "total_traces": {"$sum": 1},
                "consumption": {"$sum": {"$ifNull": ["$cost", 0]}},
            }
        },
        {"$sort": {"last_seen": -1}},
    ]
    try:
        rows = []
        for doc in db.messages.aggregate(
            pipeline, allowDiskUse=True, maxTimeMS=max_ms
        ):
            uid = doc.get("_id")
            if not uid:
                continue
            rows.append(
                {
                    "user_id": uid,
                    "first_seen": _dt_iso_utc(doc.get("first_seen")),
                    "last_seen": _dt_iso_utc(doc.get("last_seen")),
                    "total_traces": int(doc.get("total_traces") or 0),
                    "consumption": float(doc.get("consumption") or 0),
                }
            )
        meta = {
            **_mongo_churn_meta(True, len(rows), len(rows), None),
            "source": "mongodb_messages_period",
            "period_days": days,
            "users_with_messages": len(rows),
        }
        return rows, meta
    except Exception as e:
        log.warning("wonka_messages_per_user_in_days: %s", e)
        return [], {
            **_mongo_churn_meta(True, 0, 0, str(e)[:280]),
            "period_days": days,
        }


def wonka_user_profiles_by_ids(user_ids):
    """
    Pour chaque user_id valide en ObjectId, retourne
    ( { user_id_lower: { email, organizations } }, meta ).

    Clés en minuscules pour matcher Langfuse (casse variable).
    Nécessite WONKA_MONGO_URI ; sinon profils vides et meta.configured=False.
    """
    uri = (os.environ.get("WONKA_MONGO_URI") or "").strip()
    if not user_ids:
        return {}, _mongo_churn_meta(bool(uri), 0, 0, None)

    try:
        from bson import ObjectId
        from bson.errors import InvalidId
    except ImportError:
        log.warning("pymongo absent — pip install pymongo pour email/org churn")
        oid_pat = re.compile(r"^[0-9a-fA-F]{24}$")
        n = sum(1 for uid in user_ids if uid and oid_pat.match(str(uid).strip()))
        return {}, _mongo_churn_meta(bool(uri), n, 0, "pymongo_missing")

    oids, seen = [], set()
    for uid in user_ids:
        if not uid:
            continue
        try:
            oid = ObjectId(str(uid).strip())
        except InvalidId:
            continue
        if oid not in seen:
            seen.add(oid)
            oids.append(oid)
    requested = len(oids)
    if not oids:
        return {}, _mongo_churn_meta(bool(uri), 0, 0, None)

    client, mc_err = _get_mongo_client()
    if mc_err == "no_uri":
        log.info("Churn enrich: WONKA_MONGO_URI absent — email/org désactivés")
        return {}, _mongo_churn_meta(False, requested, 0, None)
    if mc_err == "pymongo_missing":
        return {}, _mongo_churn_meta(bool(uri), requested, 0, "pymongo_missing")

    db_name = (os.environ.get("WONKA_MONGO_DB") or "wonkachat-prod").strip()
    try:
        db = client[db_name]
        pipeline = [
            {"$match": {"_id": {"$in": oids}}},
            {
                "$lookup": {
                    "from": "organizations",
                    "localField": "organizationMemberships.organizationId",
                    "foreignField": "_id",
                    "as": "_orgs",
                }
            },
            {
                "$project": {
                    "email": 1,
                    "org_names": {
                        "$map": {
                            "input": "$_orgs",
                            "as": "o",
                            "in": "$$o.name",
                        }
                    },
                }
            },
        ]
        out = {}
        for doc in db.users.aggregate(pipeline, allowDiskUse=False):
            key = str(doc["_id"]).lower()
            names = [n for n in (doc.get("org_names") or []) if n]
            out[key] = {
                "email": doc.get("email") or None,
                "organizations": names,
            }
        matched = len(out)
        if matched < requested:
            log.info(
                "Churn enrich Mongo: %s profils sur %s userIds (absents de users?)",
                matched,
                requested,
            )
        return out, _mongo_churn_meta(True, requested, matched, None)
    except Exception as e:
        log.warning("Wonka Mongo churn enrich: %s", e)
        err = str(e)[:240]
        return {}, _mongo_churn_meta(True, requested, 0, err)

# ---------------------------------------------------------------------------
# Requesty API client
# ---------------------------------------------------------------------------
class RequestyClient:
    def __init__(self, key):
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        })

    def keys(self):
        r = self.s.get(f"{API_BASE}/v1/manage/apikey", timeout=60)
        r.raise_for_status()
        return r.json().get("keys", [])

    def key_usage(self, key_id, start, end):
        r = self.s.get(
            f"{API_BASE}/v1/manage/apikey/{key_id}/usage",
            json={"start": start, "end": end, "resolution": "day"},
            timeout=60
        )
        r.raise_for_status()
        return r.json().get("usage", {})

# ---------------------------------------------------------------------------
# Sync: Requesty -> requesty_daily
# ---------------------------------------------------------------------------
def sync_requesty(conn, client, period="30d"):
    """
    Fetch daily usage per API key from Requesty.
    Stores completions_requests as the real request count.

    Data flow:
      Requesty /v1/manage/apikey/{id}/usage
        -> {date: {completions_requests, input_tokens, ..., spend}}
        -> requesty_daily (one row per key+date)
    """
    days  = min(int(period.rstrip("d")) if period.endswith("d") else 30, 90)
    end   = utcnow()
    start = end - timedelta(days=days)
    s_str = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    e_str = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    now   = utcnow().isoformat()

    keys = client.keys()
    rows = []
    for k in keys:
        kid   = k.get("id")
        kname = k.get("name", kid)
        try:
            usage = client.key_usage(kid, s_str, e_str)
            for date_str, u in usage.items():
                rows.append((
                    f"{kid}_{date_str}",
                    date_str,
                    kid,
                    kname,
                    int(u.get("completions_requests") or u.get("total_requests") or 0),
                    int(u.get("input_tokens", 0)),
                    int(u.get("output_tokens", 0)),
                    int(u.get("total_tokens", 0)),
                    float(u.get("spend", 0)),
                ))
        except Exception as e:
            log.warning(f"  Requesty key {kname}: {e}")

    if rows:
        conn.executemany("""
            INSERT OR REPLACE INTO requesty_daily
              (id, date, key_id, key_name, completions, input_tokens,
               output_tokens, total_tokens, cost)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, rows)
        conn.commit()

    set_meta(conn, "last_requesty_sync", now)
    log.info(f"  Requesty: {len(rows)} records ({len(keys)} keys)")
    return {"records": len(rows), "keys": len(keys)}

# ---------------------------------------------------------------------------
# Sync: Langfuse /api/public/traces -> lf_users
#
# Stratégie pour ne pas surcharger Langfuse :
#   - Sync par morceaux (chunked) : chaque run fait au plus N pages (défaut 15),
#     sauvegarde la progression (lf_users_full_next_page). Le run suivant reprend.
#   - Incrémental (<48h) : peu de pages, fenêtre 2j.
#   - Délai entre pages (défaut 1.5s), backoff sur 429/503.
# ---------------------------------------------------------------------------

def _lf_traces_page_size():
    return max(10, min(100, int(os.environ.get("LF_TRACES_PAGE_SIZE", "50"))))


def _lf_traces_sleep_s():
    return max(0.3, min(30.0, float(os.environ.get("LF_TRACES_SLEEP", "1.5"))))


def _lf_traces_chunk_pages():
    return max(5, min(50, int(os.environ.get("LF_TRACES_CHUNK_PAGES", "15"))))


def _paginate_traces(lf_session, from_ts, cutoff_90, start_page, max_pages, page_size, sleep_s):
    """Génère des dicts {user_id: {first, last, count}} page par page.
    Gère 429/503 avec backoff pour ne pas saturer Langfuse.
    """
    users: dict = {}
    page = start_page
    consecutive_errors = 0
    MAX_ERRORS = 5
    MAX_RATE_LIMIT_RETRIES = 3

    while page < start_page + max_pages:
        try:
            params = {"page": page, "limit": page_size}
            if from_ts:
                params["fromTimestamp"] = from_ts
            r = lf_session.get(f"{LF_HOST}/api/public/traces", params=params, timeout=60)

            if r.status_code == 429 or r.status_code in (502, 503):
                wait_s = 30
                if r.status_code == 429:
                    ra = r.headers.get("Retry-After")
                    if ra and ra.isdigit():
                        wait_s = min(120, int(ra))
                for _ in range(MAX_RATE_LIMIT_RETRIES):
                    log.warning(
                        "  Langfuse traces page %s: %s — attente %ss avant retry",
                        page, r.status_code, wait_s,
                    )
                    time.sleep(wait_s)
                    r = lf_session.get(
                        f"{LF_HOST}/api/public/traces", params=params, timeout=60
                    )
                    if r.status_code not in (429, 502, 503):
                        break
                    wait_s = min(120, wait_s * 2)
                if r.status_code in (429, 502, 503):
                    consecutive_errors += 1
                    if consecutive_errors >= MAX_ERRORS:
                        log.error(
                            "  Langfuse traces: %s erreurs consécutives, abandon",
                            MAX_ERRORS,
                        )
                        break
                    time.sleep(consecutive_errors * 10)
                    continue

            r.raise_for_status()
            consecutive_errors = 0
            traces = r.json().get("data", [])
            if not traces:
                break

            stop = False
            for t in traces:
                uid = t.get("userId")
                ts  = t.get("timestamp", "")
                if cutoff_90 and ts and ts < cutoff_90:
                    stop = True
                    break
                if uid:
                    if uid not in users:
                        users[uid] = {"first": ts, "last": ts, "count": 0}
                    else:
                        if ts and ts < users[uid]["first"]:
                            users[uid]["first"] = ts
                        if ts and ts > users[uid]["last"]:
                            users[uid]["last"] = ts
                    users[uid]["count"] += 1

            yield page, users, stop or len(traces) < page_size

            if stop or len(traces) < page_size:
                break
            page += 1
            time.sleep(sleep_s)

        except Exception as e:
            consecutive_errors += 1
            log.error("  Langfuse traces page %s: %s", page, e)
            if consecutive_errors >= MAX_ERRORS:
                break
            time.sleep(consecutive_errors * 5)


def _load_lf_users_from_db(conn):
    """Charge lf_users depuis la DB en dict {user_id: {first, last, count}} pour merge chunked."""
    rows = conn.execute(
        "SELECT user_id, first_seen, last_seen, total_traces FROM lf_users"
    ).fetchall()
    return {
        r["user_id"]: {
            "first": r["first_seen"] or "",
            "last": r["last_seen"] or "",
            "count": int(r["total_traces"] or 0),
        }
        for r in rows
    }


def _merge_lf_users(existing: dict, new_batch: dict):
    """Fusionne new_batch dans existing (first=min, last=max, count+=)."""
    for uid, v in new_batch.items():
        if uid not in existing:
            existing[uid] = {"first": v["first"], "last": v["last"], "count": v["count"]}
        else:
            if v["first"] and (not existing[uid]["first"] or v["first"] < existing[uid]["first"]):
                existing[uid]["first"] = v["first"]
            if v["last"] and (not existing[uid]["last"] or v["last"] > existing[uid]["last"]):
                existing[uid]["last"] = v["last"]
            existing[uid]["count"] += v["count"]


def _upsert_lf_users(conn, users: dict, now_str: str, replace=False):
    """INSERT OR REPLACE (full) ou upsert-merge (incrémental)."""
    if not users:
        return
    if replace:
        conn.executemany("""
            INSERT OR REPLACE INTO lf_users
              (user_id, first_seen, last_seen, total_traces, updated_at)
            VALUES (?,?,?,?,?)
        """, [(uid, v["first"], v["last"], v["count"], now_str) for uid, v in users.items()])
    else:
        conn.executemany("""
            INSERT INTO lf_users (user_id, first_seen, last_seen, total_traces, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              first_seen   = MIN(first_seen, excluded.first_seen),
              last_seen    = MAX(last_seen,  excluded.last_seen),
              total_traces = total_traces + excluded.total_traces,
              updated_at   = excluded.updated_at
        """, [(uid, v["first"], v["last"], v["count"], now_str) for uid, v in users.items()])
    conn.commit()


def sync_lf_users(conn, days=90):
    """
    Point d'entrée pour le sync users. Retourne toujours le count actuel en DB.

    - Sync récent (<48h) : incrémental (quelques pages, léger).
    - Sinon : sync par morceaux (chunked) — un seul chunk par appel (défaut 15 pages),
      progression sauvegardée ; le prochain appel reprend où on en était.
    """
    if not LF_PUBLIC or not LF_SECRET:
        log.info("  Langfuse users: skipped (no credentials)")
        return 0

    now = utcnow()
    now_str = now.isoformat()
    cutoff_90 = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    page_size = _lf_traces_page_size()
    sleep_s = _lf_traces_sleep_s()
    chunk_pages = _lf_traces_chunk_pages()

    last_sync_str = get_meta(conn, "last_lf_users_sync")
    next_page_str = get_meta(conn, "lf_users_full_next_page")

    # -- Incrémental si sync récent (<48h) et pas de full en cours -------------
    if last_sync_str and not next_page_str:
        try:
            hours_ago = (now - datetime.fromisoformat(last_sync_str[:19])).total_seconds() / 3600
            if hours_ago < 48:
                from_ts = (now - timedelta(days=2)).strftime("%Y-%m-%dT00:00:00Z")
                log.info(
                    "  Langfuse users: incrémental (depuis 48h, %.1fh depuis dernier sync)",
                    hours_ago,
                )
                lf_session = requests.Session()
                lf_session.auth = (LF_PUBLIC, LF_SECRET)
                users: dict = {}
                for _, u, _ in _paginate_traces(
                    lf_session, from_ts, None, 1, 20, page_size, 0.3
                ):
                    users = u
                _upsert_lf_users(conn, users, now_str, replace=False)
                purged = conn.execute(
                    "DELETE FROM lf_users WHERE last_seen < ?",
                    [(now - timedelta(days=90)).strftime("%Y-%m-%d")],
                ).rowcount
                conn.commit()
                set_meta(conn, "last_lf_users_sync", now_str)
                total = conn.execute("SELECT COUNT(*) FROM lf_users").fetchone()[0]
                log.info(
                    "  Langfuse users: incrémental OK — %s mis à jour, %s en DB, %s purgés",
                    len(users), total, purged,
                )
                return total
        except Exception:
            pass

    # -- Sync par morceaux (full ou suite du full) ----------------------------
    start_page = 1
    if next_page_str:
        try:
            start_page = int(next_page_str)
        except ValueError:
            start_page = 1

    log.info(
        "  Langfuse users: chunk sync (page %s, %s pages max, %s traces/page)",
        start_page, chunk_pages, page_size,
    )
    lf_session = requests.Session()
    lf_session.auth = (LF_PUBLIC, LF_SECRET)
    existing = _load_lf_users_from_db(conn)
    last_page = start_page
    done_fully = False
    for page, users_batch, done in _paginate_traces(
        lf_session, None, cutoff_90, start_page, chunk_pages, page_size, sleep_s
    ):
        _merge_lf_users(existing, users_batch)
        last_page = page
        if done:
            done_fully = True
            break

    _upsert_lf_users(conn, existing, now_str, replace=True)
    if done_fully:
        set_meta(conn, "last_lf_users_sync", now_str)
        conn.execute("DELETE FROM meta WHERE key = ?", ["lf_users_full_next_page"])
        purged = conn.execute(
            "DELETE FROM lf_users WHERE last_seen < ?", [cutoff_90[:10]]
        ).rowcount
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM lf_users").fetchone()[0]
        log.info(
            "  Langfuse users: chunk sync TERMINÉ — %s users, %s purgés",
            total, purged,
        )
    else:
        set_meta(conn, "lf_users_full_next_page", str(last_page + 1))
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM lf_users").fetchone()[0]
        log.info(
            "  Langfuse users: chunk sync — page %s → %s users (prochain run: page %s)",
            last_page, total, last_page + 1,
        )
    return total

# ---------------------------------------------------------------------------
# Sync: Langfuse /metrics/daily -> model_daily  (incremental + purge 90j)
# ---------------------------------------------------------------------------
def sync_lf_models(conn, days=30):
    """
    Incremental sync using /api/public/metrics/daily (1 requête, pré-agrégé).

    Stratégie :
      1. Cherche la date la plus récente en DB.
      2. Si up-to-date (aujourd'hui) : ne fait pas de requête réseau, juste purge.
      3. Sinon : fetch uniquement les jours manquants (depuis max_date-1j jusqu'à aujourd'hui).
      4. Première fois / données trop vieilles : fetch 90 jours complets.
      5. Après insert : purge les lignes > 90 jours.

    → Évite de refetcher 90 jours entiers à chaque sync.
    """
    if not LF_PUBLIC or not LF_SECRET:
        log.info("  Langfuse models: skipped (no credentials)")
        return 0

    session    = requests.Session()
    session.auth = (LF_PUBLIC, LF_SECRET)
    now        = utcnow()
    now_str    = now.isoformat()
    today      = now.strftime("%Y-%m-%d")
    cutoff_90  = (now - timedelta(days=90)).strftime("%Y-%m-%d")

    # -- Trouver la dernière date stockée ------------------------------------
    latest_row    = conn.execute("SELECT MAX(date) as d FROM model_daily").fetchone()
    latest_stored = latest_row["d"] if latest_row and latest_row["d"] else None

    if latest_stored and latest_stored >= today:
        log.info(f"  Langfuse models: already up-to-date ({latest_stored}), skipping fetch")
        purged = conn.execute("DELETE FROM model_daily WHERE date < ?", [cutoff_90]).rowcount
        conn.commit()
        if purged:
            log.info(f"  Langfuse models: purged {purged} rows (> 90 days)")
        return conn.execute("SELECT COUNT(DISTINCT model) FROM model_daily").fetchone()[0]

    # -- Déterminer la fenêtre à fetcher -------------------------------------
    if latest_stored and latest_stored > cutoff_90:
        # Incrémental : depuis la veille du dernier jour connu
        fetch_from = (
            datetime.strptime(latest_stored, "%Y-%m-%d") - timedelta(days=1)
        ).strftime("%Y-%m-%dT00:00:00Z")
        log.info(f"  Langfuse models: incremental fetch from {latest_stored} (stored) → today")
    else:
        # Première fois ou données périmées : fetch 90 jours complets
        fetch_from = (now - timedelta(days=90)).strftime("%Y-%m-%dT00:00:00Z")
        log.info(f"  Langfuse models: full fetch (90 days, latest_stored={latest_stored})")

    fetch_to = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # -- Fetch avec 3 tentatives ---------------------------------------------
    data = None
    for attempt in range(3):
        try:
            r = session.get(
                f"{LF_HOST}/api/public/metrics/daily",
                params={"fromTimestamp": fetch_from, "toTimestamp": fetch_to},
                timeout=30
            )
            if r.status_code in (502, 503):
                wait = (attempt + 1) * 5
                log.warning(f"  Langfuse metrics {r.status_code} (attempt {attempt+1}/3), retry in {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            data  = r.json().get("data", [])
            dates = [d.get("date", "?")[:10] for d in data]
            log.info(f"  Langfuse metrics: {len(data)} days → {dates[:5]}{'...' if len(dates) > 5 else ''}")
            break
        except Exception as e:
            log.warning(f"  Langfuse metrics error (attempt {attempt+1}/3): {e}")
            time.sleep(5)

    if data is None:
        log.error("  Langfuse metrics: failed after 3 attempts, keeping existing data")
        return conn.execute("SELECT COUNT(DISTINCT model) FROM model_daily").fetchone()[0]

    # -- Upsert (INSERT OR REPLACE pour écraser les jours partiels) ----------
    rows = []
    for day in data:
        date = day.get("date", "")[:10]
        if not date:
            continue
        for m in day.get("usage", []):
            model = m.get("model") or "unknown"
            rows.append((
                f"{model}|{date}", date, model,
                int(m.get("inputUsage")        or 0),
                int(m.get("outputUsage")       or 0),
                int(m.get("totalUsage")        or 0),
                float(m.get("totalCost")       or 0),
                int(m.get("countObservations") or 0),
            ))

    if rows:
        conn.executemany("""
            INSERT OR REPLACE INTO model_daily
              (id, date, model, input_tokens, output_tokens, total_tokens, cost, request_count)
            VALUES (?,?,?,?,?,?,?,?)
        """, rows)
        log.info(f"  Langfuse models: {len(rows)} rows upserted")

    # -- Purge > 90 jours ----------------------------------------------------
    purged = conn.execute("DELETE FROM model_daily WHERE date < ?", [cutoff_90]).rowcount
    conn.commit()
    if purged:
        log.info(f"  Langfuse models: purged {purged} rows (> 90 days)")

    n_models = conn.execute("SELECT COUNT(DISTINCT model) FROM model_daily").fetchone()[0]
    set_meta(conn, "last_lf_models_sync", now_str)
    log.info(f"  Langfuse models: {n_models} distinct models total in DB")
    return n_models

# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------
def make_auth(token):
    def decorator(f):
        @wraps(f)
        def wrapper(*a, **kw):
            if not token:
                return f(*a, **kw)
            if request.headers.get("Authorization") == f"Bearer {token}":
                return f(*a, **kw)
            if request.args.get("token") == token:
                return f(*a, **kw)
            if request.cookies.get("rq_token") == token:
                return f(*a, **kw)
            return jsonify({"error": "Unauthorized"}), 401
        return wrapper
    return decorator

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
def create_app(conn, client, auth_token=None, html_path=None):
    app = Flask(__name__)
    CORS(app, origins="*", supports_credentials=True)
    auth = make_auth(auth_token)
    R    = lambda rows: [dict(r) for r in rows]

    def since(default_days=30):
        """Return ISO timestamp from ?period=30d or ?since=<iso> param."""
        period = request.args.get("period", f"{default_days}d")
        if period.endswith("d"):
            try:
                days = int(period[:-1])
            except (ValueError, TypeError):
                days = default_days
        else:
            days = default_days
        days = max(1, min(days, 365))
        return (utcnow() - timedelta(days=days)).isoformat()

    def since_date(default_days=30):
        return since(default_days)[:10]

    # ── Dashboard HTML ────────────────────────────────────────────────────
    @app.route("/")
    @app.route("/analytics")
    def index():
        if html_path and Path(html_path).exists():
            return Path(html_path).read_text(), 200, {"Content-Type": "text/html; charset=utf-8"}
        return "Dashboard not found", 404

    # ── Health ────────────────────────────────────────────────────────────
    @app.route("/analytics/health")
    def health():
        rd = conn.execute("SELECT COUNT(*) as n FROM requesty_daily").fetchone()["n"]
        md = conn.execute("SELECT COUNT(*) as n FROM model_daily").fetchone()["n"]
        lu = conn.execute("SELECT COUNT(*) as n FROM lf_users").fetchone()["n"]
        return jsonify({
            "status": "ok",
            "requesty_records": rd,
            "model_records": md,
            "lf_users": lu,
            "last_requesty_sync": get_meta(conn, "last_requesty_sync", "never"),
            "last_lf_users_sync": get_meta(conn, "last_lf_users_sync", "never"),
            "last_lf_models_sync": get_meta(conn, "last_lf_models_sync", "never"),
            "auth_required": bool(auth_token),
        })

    # ── Sync ─────────────────────────────────────────────────────────────
    _sync_lock = threading.Lock()

    @app.route("/analytics/sync", methods=["POST"])
    @auth
    def do_sync():
        # Prevent concurrent syncs — if one is already running, reject
        if not _sync_lock.acquire(blocking=False):
            return jsonify({"error": "Sync déjà en cours, réessayez dans quelques secondes"}), 429

        period = (request.json or {}).get("period", "30d")
        days   = int(period[:-1]) if period.endswith("d") else 30

        result = {}
        try:
            try:
                result["requesty"] = sync_requesty(conn, client, period)
            except Exception as e:
                log.error(f"Requesty sync error: {e}")
                result["requesty"] = {"error": str(e)}
            try:
                result["lf_users"] = sync_lf_users(conn, days=days)
            except Exception as e:
                log.error(f"Langfuse users sync error: {e}")
                result["lf_users"] = {"error": str(e)}
            try:
                result["lf_models"] = sync_lf_models(conn, days=days)
            except Exception as e:
                log.error(f"Langfuse models sync error: {e}")
                result["lf_models"] = {"error": str(e)}
            result["synced_at"] = utcnow().isoformat()
        finally:
            _sync_lock.release()
        return jsonify(result)

    # ── Overview KPIs ─────────────────────────────────────────────────────
    @app.route("/analytics/overview")
    @auth
    def overview():
        s = since_date()
        r = conn.execute("""
            SELECT
              SUM(completions)   as total_requests,
              SUM(total_tokens)  as total_tokens,
              SUM(input_tokens)  as total_input,
              SUM(output_tokens) as total_output,
              SUM(cost)          as total_cost,
              COUNT(DISTINCT key_id) as unique_orgs
            FROM requesty_daily WHERE date>=?
        """, [s]).fetchone()
        d = dict(r)
        d["avg_latency"]  = 0
        d["cache_hits"]   = 0
        m = conn.execute(
            "SELECT COUNT(DISTINCT model) as n FROM model_daily WHERE date>=?", [s]
        ).fetchone()
        d["unique_models"] = m["n"] if m else 0
        return jsonify(d)

    # ── Timeseries ────────────────────────────────────────────────────────
    @app.route("/analytics/timeseries")
    @auth
    def timeseries():
        s = since_date()
        rows = conn.execute("""
            SELECT date as period,
                   SUM(completions)   as requests,
                   SUM(total_tokens)  as tokens,
                   SUM(cost)          as cost
            FROM requesty_daily WHERE date>=?
            GROUP BY date ORDER BY date
        """, [s]).fetchall()
        return jsonify(R(rows))

    # ── By model ──────────────────────────────────────────────────────────
    @app.route("/analytics/by-model")
    @auth
    def by_model():
        s = since_date()
        rows = conn.execute("""
            SELECT model,
                   SUM(request_count) as request_count,
                   SUM(input_tokens)  as input_tokens,
                   SUM(output_tokens) as output_tokens,
                   SUM(total_tokens)  as total_tokens,
                   SUM(cost)          as total_cost
            FROM model_daily WHERE date>=?
            GROUP BY model ORDER BY total_tokens DESC
        """, [s]).fetchall()
        if not rows:
            return jsonify({"data": [], "warning": "No model data yet -- POST /analytics/sync to populate"})
        return jsonify(R(rows))

    # ── By org (API key) ──────────────────────────────────────────────────
    @app.route("/analytics/by-org")
    @auth
    def by_org():
        s = since_date()
        rows = conn.execute("""
            SELECT key_name as org_name, key_id as org_id,
                   SUM(completions)   as request_count,
                   SUM(total_tokens)  as total_tokens,
                   SUM(cost)          as total_cost,
                   MAX(date)          as last_seen
            FROM requesty_daily WHERE date>=?
            GROUP BY key_id ORDER BY total_tokens DESC
        """, [s]).fetchall()
        return jsonify(R(rows))

    # ── Usage par organisation Wonka (Mongo messages + profils users) ─────
    @app.route("/analytics/usage-by-wonka-org")
    @auth
    def usage_by_wonka_org():
        from collections import defaultdict

        try:
            period = request.args.get("period", "30d")
            try:
                ddays = int(period[:-1]) if str(period).endswith("d") else 30
            except (ValueError, TypeError):
                ddays = 30
            ddays = max(1, min(ddays, 365))

            rows, msg_meta = wonka_messages_per_user_in_days(ddays)
            err = msg_meta.get("error")
            if err == "no_uri":
                return jsonify(
                    {
                        "data": [],
                        "meta": {
                            **msg_meta,
                            "hint": "Ajoutez WONKA_MONGO_URI dans .env",
                        },
                    }
                )
            if err and err != "no_uri":
                return jsonify({"data": [], "meta": msg_meta})

            seen_uid = set()
            uids_unique = []
            for r in rows:
                u = str(r.get("user_id") or "").strip().lower()
                if u and u not in seen_uid:
                    seen_uid.add(u)
                    uids_unique.append(r["user_id"])

            profiles = {}
            prof_matched = 0
            prof_requested = 0
            chunk_sz = 200
            for i in range(0, len(uids_unique), chunk_sz):
                chunk = uids_unique[i : i + chunk_sz]
                p, pm = wonka_user_profiles_by_ids(chunk)
                profiles.update(p)
                prof_matched += int(pm.get("matched") or 0)
                prof_requested += int(pm.get("requested") or 0)

            org_msg = defaultdict(float)
            org_consumption = defaultdict(float)
            org_users = defaultdict(set)
            for r in rows:
                uid = str(r["user_id"]).strip().lower()
                m = float(r["total_traces"] or 0)
                conso = float(r.get("consumption") or 0)
                orgs = (profiles.get(uid) or {}).get("organizations") or []
                if not orgs:
                    key = "Sans organisation"
                    org_msg[key] += m
                    org_consumption[key] += conso
                    org_users[key].add(uid)
                else:
                    share = m / len(orgs)
                    share_conso = conso / len(orgs)
                    for o in orgs:
                        label = o if o else "Sans nom"
                        org_msg[label] += share
                        org_consumption[label] += share_conso
                        org_users[label].add(uid)

            # Min. 4 membres actifs sur la période ; tri décroissant par messages
            data = [
                {
                    "organization": k,
                    "messages": round(v, 1),
                    "consumption": round(org_consumption.get(k, 0), 2),
                    "users": len(org_users[k]),
                }
                for k, v in sorted(org_msg.items(), key=lambda x: -x[1])
                if len(org_users[k]) >= 4
            ]
            return jsonify(
                {
                    "data": data,
                    "meta": {
                        "period_days": ddays,
                        "users_with_messages": len(rows),
                        "organizations_count": len(data),
                        "profiles_matched": prof_matched,
                        "profiles_requested": prof_requested,
                    },
                }
            )
        except Exception as e:
            log.exception("usage_by_wonka_org")
            return jsonify(
                {
                    "data": [],
                    "meta": {"error": str(e)[:500]},
                }
            )

    # ── Churn users ───────────────────────────────────────────────────────
    @app.route("/analytics/churn-users")
    @auth
    def churn_users():
        rows, msg_meta = wonka_churn_rows_from_messages()
        profiles, prof_meta = wonka_user_profiles_by_ids(
            [r["user_id"] for r in rows]
        )
        mongo_enrich = {
            **msg_meta,
            "profiles_matched": prof_meta.get("matched", 0),
            "profiles_requested": prof_meta.get("requested", 0),
        }
        if prof_meta.get("error") and not mongo_enrich.get("error"):
            mongo_enrich["profile_error"] = prof_meta["error"]

        now = utcnow()
        MIN_TRACES = 5
        actif, inactif, risque, churne, insuffisant = [], [], [], [], []
        for r in rows:
            d = dict(r)
            if "consumption" not in d or d["consumption"] is None:
                d["consumption"] = 0.0
            uid_key = (d.get("user_id") or "").strip().lower()
            p = profiles.get(uid_key, {})
            d["email"] = p.get("email")
            d["organizations"] = p.get("organizations") or []
            try:
                ls = d["last_seen"]
                if ls.endswith("Z"):
                    ls = ls[:-1]
                days_ago = (now - datetime.fromisoformat(ls[:19])).days
            except Exception:
                days_ago = 999
            d["days_since_last"] = days_ago
            traces = d.get("total_traces") or 0
            if traces < MIN_TRACES:
                d["status"] = "insuffisant"
                insuffisant.append(d)
            elif days_ago <= 3:
                d["status"] = "actif"
                actif.append(d)
            elif days_ago <= 7:
                d["status"] = "inactif"
                inactif.append(d)
            elif days_ago <= 10:
                d["status"] = "risque"
                risque.append(d)
            else:
                d["status"] = "churne"
                churne.append(d)
        return jsonify({
            "actif": actif,
            "inactif": inactif,
            "risque": risque,
            "churne": churne,
            "insuffisant": insuffisant,
            "total": len(rows),
            "mongo_enrich": mongo_enrich,
        })

    # ── By user (raw list) ────────────────────────────────────────────────
    @app.route("/analytics/by-user")
    @auth
    def by_user():
        rows = conn.execute("""
            SELECT user_id, first_seen, last_seen, total_traces, updated_at
            FROM lf_users ORDER BY last_seen DESC
        """).fetchall()
        now    = utcnow()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["days_since_last"] = (now - datetime.fromisoformat(d["last_seen"][:19])).days
            except Exception:
                d["days_since_last"] = 999
            result.append(d)
        return jsonify(result)

    # ── Export CSV ────────────────────────────────────────────────────────
    @app.route("/analytics/export/csv")
    @auth
    def export_csv():
        s = since_date(90)
        rows = conn.execute("""
            SELECT date, key_name, completions, total_tokens,
                   input_tokens, output_tokens, cost
            FROM requesty_daily WHERE date>=? ORDER BY date DESC
        """, [s]).fetchall()
        def gen():
            yield "date,key_name,completions,total_tokens,input_tokens,output_tokens,cost\n"
            for r in rows:
                yield ",".join(str(v or "") for v in r) + "\n"
        return Response(gen(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=export.csv"})

    return app

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--key",        default=os.environ.get("REQUESTY_KEY"),
                   help="Requesty API key (default: env REQUESTY_KEY, ex. depuis .env)")
    p.add_argument("--db",         default=str(DB_PATH))
    p.add_argument("--port",       type=int, default=PORT)
    p.add_argument("--period",     default="30d")
    p.add_argument("--auto",       action="store_true", help="Enable hourly auto-sync")
    p.add_argument("--auth-token", default=None, dest="auth_token")
    args = p.parse_args()
    if not args.key:
        p.error("Clé Requesty manquante : définis REQUESTY_KEY dans .env ou passe --key")

    conn   = init_db(Path(args.db))
    client = RequestyClient(args.key)

    # Initial sync on startup (non-fatal — server starts even if API is unreachable)
    # Requesty + models: rapides, inline.
    # Langfuse users: toujours en background pour ne pas bloquer le démarrage
    #   (Langfuse /traces peut être lent ou down, les retries prendraient 2+ min).
    log.info("Initial sync...")
    days = int(args.period[:-1]) if args.period.endswith("d") else 30
    try:
        sync_requesty(conn, client, args.period)
    except Exception as e:
        log.warning(f"Requesty sync failed on startup: {e}")
    try:
        sync_lf_models(conn, days=days)
    except Exception as e:
        log.warning(f"Langfuse models sync failed on startup: {e}")
    # Users en arrière-plan — ne bloque pas Flask
    threading.Thread(target=sync_lf_users, args=(conn,), daemon=True).start()

    # Auto-sync background thread
    if args.auto:
        def cron():
            while True:
                time.sleep(SYNC_INTERVAL)
                try:
                    sync_requesty(conn, client, args.period)
                    sync_lf_users(conn)
                    sync_lf_models(conn, days=days)
                    log.info("Auto-sync complete")
                except Exception as e:
                    log.error(f"Auto-sync error: {e}")
        threading.Thread(target=cron, daemon=True).start()
        log.info(f"Auto-sync enabled (every {SYNC_INTERVAL//60}min)")

    host = "0.0.0.0" if os.path.exists("/.dockerenv") else "127.0.0.1"
    html = Path(args.db).parent / "dashboard.html"
    log.info(f"Serving on http://{host}:{args.port}")
    create_app(conn, client, auth_token=args.auth_token, html_path=html).run(
        port=args.port, host=host, debug=False
    )

if __name__ == "__main__":
    main()
