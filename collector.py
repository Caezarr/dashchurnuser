#!/usr/bin/env python3
"""
Requesty + Langfuse Analytics Collector
========================================
Sources:
  - Requesty API  -> requesty_daily  (cost, tokens, real request count)
  - Langfuse /traces -> lf_users      (user churn analysis)
  - Langfuse /observations -> model_daily (per-model breakdown)

Run locally:
  pip install requests flask flask-cors
  cp .env.example .env   # fill in your keys
  python collector.py --key $REQUESTY_KEY --auto --port 7842

Deploy:
  git add collector.py && git commit -m "..." && git push vps main
"""

import argparse, json, os, sqlite3, sys, time, threading, logging, socket
from datetime import datetime, timedelta, timezone

def utcnow():
    """Timezone-aware UTC datetime (replaces deprecated utcnow())."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
from pathlib import Path
from functools import wraps

import requests
from flask import Flask, jsonify, request, Response
from flask_cors import CORS

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
# Sync: Langfuse /traces -> lf_users + lf_sessions
# ---------------------------------------------------------------------------
def sync_lf_users(conn, days=30):
    """
    Paginate Langfuse /api/public/traces (last N days only).
    Aggregates first_seen/last_seen/trace_count per user_id.
    Also captures session data from trace.sessionId.
    """
    if not LF_PUBLIC or not LF_SECRET:
        log.info("  Langfuse users: skipped (no credentials)")
        return 0

    session = requests.Session()
    session.auth = (LF_PUBLIC, LF_SECRET)
    cutoff = (utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    now   = utcnow().isoformat()
    page  = 1
    limit = 100
    # Safety cap: max pages to fetch per sync to avoid hammering a self-hosted
    # Langfuse that has no server-side date filter on /traces.
    # At 100 traces/page, 50 pages = 5 000 traces max — enough for 30 days.
    MAX_PAGES = 50
    users    = {}  # user_id -> {first, last, count}
    sessions = {}  # session_id -> {user_id, created_at, count}

    while page <= MAX_PAGES:
        try:
            r = session.get(
                f"{LF_HOST}/api/public/traces",
                params={"page": page, "limit": limit},
                timeout=60
            )
            log.info(f"  Langfuse users: page {page}/{MAX_PAGES} ({r.status_code})")
            r.raise_for_status()
            traces = r.json().get("data", [])
            if not traces:
                break

            stop = False
            for t in traces:
                uid = t.get("userId")
                ts  = t.get("timestamp", "")
                # Langfuse retourne les traces les plus récentes en premier.
                # On s'arrête dès qu'on dépasse le cutoff pour ne pas
                # parcourir l'historique complet (protection Langfuse).
                if ts and ts[:19] < cutoff[:19]:
                    stop = True
                    break
                if uid:
                    if uid not in users:
                        users[uid] = {"first": ts, "last": ts, "count": 0}
                    else:
                        if ts < users[uid]["first"]: users[uid]["first"] = ts
                        if ts > users[uid]["last"]:  users[uid]["last"]  = ts
                    users[uid]["count"] += 1

                sid = t.get("sessionId")
                if sid:
                    if sid not in sessions:
                        sessions[sid] = {"user_id": uid or "", "created_at": ts, "count": 0}
                    elif ts and ts < sessions[sid]["created_at"]:
                        sessions[sid]["created_at"] = ts
                    sessions[sid]["count"] += 1

            if stop or len(traces) < limit:
                break
            page += 1
            time.sleep(0.5)  # be gentle with self-hosted instance

        except Exception as e:
            log.error(f"  Langfuse users error (page {page}): {e}")
            break

    if users:
        conn.executemany("""
            INSERT OR REPLACE INTO lf_users
              (user_id, first_seen, last_seen, total_traces, updated_at)
            VALUES (?,?,?,?,?)
        """, [(uid, v["first"], v["last"], v["count"], now) for uid, v in users.items()])
    if sessions:
        conn.executemany("""
            INSERT OR REPLACE INTO lf_sessions
              (session_id, user_id, created_at, trace_count, updated_at)
            VALUES (?,?,?,?,?)
        """, [(sid, v["user_id"], v["created_at"], v["count"], now) for sid, v in sessions.items()])
    conn.commit()
    set_meta(conn, "last_lf_users_sync", now)
    log.info(f"  Langfuse users: {len(users)} users, {len(sessions)} sessions")
    return len(users)

# ---------------------------------------------------------------------------
# Sync: Langfuse /observations -> model_daily
# ---------------------------------------------------------------------------
def sync_lf_models(conn, days=30):
    """
    Paginate Langfuse /api/public/observations?type=GENERATION.
    Commits after each page (checkpoint pattern — resumable on error).
    Uses fromStartTime to limit to last N days.
    Retries up to 3x on 5xx errors.

    Data flow:
      Langfuse /api/public/observations?type=GENERATION
        -> {model, startTime, usage:{input,output,total}, calculatedTotalCost}
        -> model_daily (INSERT OR REPLACE, one row per model+date)
    """
    if not LF_PUBLIC or not LF_SECRET:
        log.info("  Langfuse models: skipped (no credentials)")
        return 0

    session   = requests.Session()
    session.auth = (LF_PUBLIC, LF_SECRET)
    cutoff    = (utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    now       = utcnow().isoformat()
    page      = 1
    limit     = 50
    total_obs = 0

    # Clear existing records for this period before re-fetching — prevents
    # double-counting on repeated syncs (the ON CONFLICT +accumulate pattern
    # is only safe within a single sync run, not across multiple runs).
    conn.execute("DELETE FROM model_daily WHERE date >= ?", [cutoff[:10]])
    conn.commit()

    while True:
        # Retry loop for 5xx errors
        data = None
        for attempt in range(3):
            try:
                r = session.get(
                    f"{LF_HOST}/api/public/observations",
                    params={
                        "type": "GENERATION",
                        "page": page,
                        "limit": limit,
                        "fromStartTime": cutoff,
                    },
                    timeout=60
                )
                if r.status_code in (502, 503):
                    wait = (attempt + 1) * 3
                    log.warning(f"  Langfuse obs {r.status_code} (attempt {attempt+1}/3), wait {wait}s")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                data = r.json().get("data", [])
                break
            except Exception as e:
                log.warning(f"  Langfuse obs error (attempt {attempt+1}/3): {e}")
                time.sleep(3)

        if data is None:
            log.error(f"  Langfuse obs: failed after 3 attempts on page {page}, stopping")
            break
        if not data:
            break

        # Aggregate this page into model_daily
        agg = {}
        for obs in data:
            model = obs.get("model") or "unknown"
            ts    = obs.get("startTime", "")
            date  = ts[:10] if ts else "unknown"
            key   = f"{model}|{date}"
            usage = obs.get("usage") or {}
            inp   = int(usage.get("input") or obs.get("promptTokens") or 0)
            out   = int(usage.get("output") or obs.get("completionTokens") or 0)
            tot   = int(usage.get("total") or obs.get("totalTokens") or inp + out)
            cost  = float(obs.get("calculatedTotalCost") or 0)
            if key not in agg:
                agg[key] = {"model": model, "date": date,
                            "inp": 0, "out": 0, "tot": 0, "cost": 0.0, "count": 0}
            agg[key]["inp"]   += inp
            agg[key]["out"]   += out
            agg[key]["tot"]   += tot
            agg[key]["cost"]  += cost
            agg[key]["count"] += 1

        # Checkpoint: commit this page's data immediately
        if agg:
            conn.executemany("""
                INSERT INTO model_daily
                  (id, date, model, input_tokens, output_tokens, total_tokens, cost, request_count)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  input_tokens  = input_tokens  + excluded.input_tokens,
                  output_tokens = output_tokens + excluded.output_tokens,
                  total_tokens  = total_tokens  + excluded.total_tokens,
                  cost          = cost          + excluded.cost,
                  request_count = request_count + excluded.request_count
            """, [
                (f"{v['model']}|{v['date']}", v["date"], v["model"],
                 v["inp"], v["out"], v["tot"], v["cost"], v["count"])
                for v in agg.values()
            ])
            conn.commit()
        total_obs += len(data)

        log.info(f"  Langfuse models: page {page}, {total_obs} obs processed")
        if len(data) < limit:
            break
        page += 1
        time.sleep(0.5)  # be gentle with the server

    n_models = conn.execute("SELECT COUNT(DISTINCT model) FROM model_daily").fetchone()[0]
    set_meta(conn, "last_lf_models_sync", now)
    log.info(f"  Langfuse models: {n_models} distinct models, {total_obs} observations")
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
            days = int(period[:-1])
        else:
            days = default_days
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

    # ── Churn users ───────────────────────────────────────────────────────
    @app.route("/analytics/churn-users")
    @auth
    def churn_users():
        rows = conn.execute("""
            SELECT user_id, first_seen, last_seen, total_traces
            FROM lf_users ORDER BY last_seen DESC
        """).fetchall()
        now    = utcnow()
        MIN_TRACES = 5
        actif, inactif, risque, churne, insuffisant = [], [], [], [], []
        for r in rows:
            d = dict(r)
            try:
                days_ago = (now - datetime.fromisoformat(d["last_seen"][:19])).days
            except Exception:
                days_ago = 999
            d["days_since_last"] = days_ago
            traces = d.get("total_traces") or 0
            if traces < MIN_TRACES:
                # Pas assez de données pour classifier
                d["status"] = "insuffisant"; insuffisant.append(d)
            elif days_ago <= 3:
                # Actif dans les 3 derniers jours
                d["status"] = "actif"; actif.append(d)
            elif days_ago <= 7:
                # Actif dans les 7 derniers jours → inactif
                d["status"] = "inactif"; inactif.append(d)
            elif days_ago <= 10:
                # Zone grise 7–10 jours
                d["status"] = "risque"; risque.append(d)
            else:
                # Pas actif depuis plus de 10 jours → churné
                d["status"] = "churne"; churne.append(d)
        return jsonify({
            "actif":       actif,
            "inactif":     inactif,
            "risque":      risque,
            "churne":      churne,
            "insuffisant": insuffisant,
            "total":       len(rows),
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
    p.add_argument("--key",        required=True, help="Requesty API key")
    p.add_argument("--db",         default=str(DB_PATH))
    p.add_argument("--port",       type=int, default=PORT)
    p.add_argument("--period",     default="30d")
    p.add_argument("--auto",       action="store_true", help="Enable hourly auto-sync")
    p.add_argument("--auth-token", default=None, dest="auth_token")
    args = p.parse_args()

    conn   = init_db(Path(args.db))
    client = RequestyClient(args.key)

    # Initial sync on startup (non-fatal — server starts even if API is unreachable)
    log.info("Initial sync...")
    days = int(args.period[:-1]) if args.period.endswith("d") else 30
    try:
        sync_requesty(conn, client, args.period)
    except Exception as e:
        log.warning(f"Requesty sync failed on startup: {e}")
    try:
        sync_lf_users(conn)
    except Exception as e:
        log.warning(f"Langfuse users sync failed on startup: {e}")
    try:
        sync_lf_models(conn, days=days)
    except Exception as e:
        log.warning(f"Langfuse models sync failed on startup: {e}")

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
