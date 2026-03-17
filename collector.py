#!/usr/bin/env python3
"""
Requesty Analytics Collector + Serveur partageable
====================================================
Modes de partage :
  --share local   → réseau local (collègues au bureau)
  --share ngrok   → tunnel internet via ngrok

Usage:
    pip install requests flask flask-cors
    pip install pyngrok   # si --share ngrok

    # Réseau local
    python collector.py --key YOUR_KEY --auto --share local

    # Internet via ngrok (partage à distance)
    python collector.py --key YOUR_KEY --auto --share ngrok

    # Avec token d'accès (recommandé pour ngrok)
    python collector.py --key YOUR_KEY --auto --share ngrok --auth-token MON_MOT_DE_PASSE
"""

import argparse, json, os, sqlite3, sys, time, threading, logging, socket
from datetime import datetime, timedelta
from pathlib import Path
from functools import wraps

import requests
from flask import Flask, jsonify, request, Response
from flask_cors import CORS

API_BASE       = "https://api-v2.requesty.ai"
DB_PATH        = Path("requesty_analytics.db")
PORT           = 7842
SYNC_INTERVAL  = 3600

LF_PUBLIC = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
LF_SECRET = os.environ.get("LANGFUSE_SECRET_KEY", "")
LF_HOST   = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("requesty")

# ── DB ────────────────────────────────────────────────────────────────────────
def init_db(path=DB_PATH):
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS requests (
            id TEXT PRIMARY KEY, timestamp TEXT, model TEXT,
            org_id TEXT, org_name TEXT,
            input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0, cost REAL DEFAULT 0,
            latency_ms INTEGER DEFAULT 0, status INTEGER DEFAULT 200,
            cached INTEGER DEFAULT 0, tags TEXT DEFAULT '[]', raw TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ts    ON requests(timestamp);
        CREATE INDEX IF NOT EXISTS idx_org   ON requests(org_id);
        CREATE INDEX IF NOT EXISTS idx_model ON requests(model);
        CREATE TABLE IF NOT EXISTS lf_users (
            user_id TEXT PRIMARY KEY,
            first_seen TEXT,
            last_seen TEXT,
            total_traces INTEGER DEFAULT 0,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS lf_model_daily (
            id TEXT PRIMARY KEY,
            date TEXT,
            model TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            cost REAL DEFAULT 0,
            request_count INTEGER DEFAULT 0,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS lf_sessions (
            session_id TEXT PRIMARY KEY,
            user_id TEXT,
            created_at TEXT,
            trace_count INTEGER DEFAULT 0,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            synced_at TEXT, endpoint TEXT, records INTEGER, status TEXT
        );
    """)
    conn.commit()
    return conn

def upsert(conn, records):
    conn.executemany("""INSERT OR REPLACE INTO requests
        (id,timestamp,model,org_id,org_name,input_tokens,output_tokens,total_tokens,
         cost,latency_ms,status,cached,tags,raw)
        VALUES (:id,:timestamp,:model,:org_id,:org_name,:input_tokens,:output_tokens,:total_tokens,
                :cost,:latency_ms,:status,:cached,:tags,:raw)""", records)
    conn.commit()

def set_meta(c, k, v): c.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k,str(v))); c.commit()
def get_meta(c, k, d=None):
    r = c.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
    return r["value"] if r else d

# ── API Client ────────────────────────────────────────────────────────────────
class Client:
    def __init__(self, key):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    def _get(self, path, **kw):
        r = self.s.get(f"{API_BASE}{path}", timeout=30, **kw)
        r.raise_for_status(); return r.json()
    def org(self):
        return self._get("/v1/manage/apikey")
    def keys(self):
        return self._get("/v1/manage/apikey").get("keys", [])
    def key_usage(self, key_id, start, end):
        return self._get(f"/v1/manage/apikey/{key_id}/usage",
                         json={"start": start, "end": end, "resolution": "day"}).get("usage", {})

def to_list(raw):
    if isinstance(raw, list): return raw
    for k in ("data","requests","logs","items","results"):
        if isinstance(raw, dict) and k in raw and isinstance(raw[k], list): return raw[k]
    return []

def norm(r, i=0):
    u = r.get("usage") or {}
    return {
        "id":            r.get("id") or r.get("request_id") or f"r_{i}_{int(time.time())}",
        "timestamp":     r.get("timestamp") or r.get("created_at") or datetime.utcnow().isoformat(),
        "model":         r.get("model") or "unknown",
        "org_id":        r.get("org_id") or r.get("user_id") or r.get("group_id") or "default",
        "org_name":      r.get("org_name") or r.get("username") or r.get("user_id") or "Default",
        "input_tokens":  int(r.get("input_tokens") or r.get("prompt_tokens") or u.get("prompt_tokens") or 0),
        "output_tokens": int(r.get("output_tokens") or r.get("completion_tokens") or u.get("completion_tokens") or 0),
        "total_tokens":  int(r.get("total_tokens") or u.get("total_tokens") or (r.get("input_tokens",0) or 0)+(r.get("output_tokens",0) or 0)),
        "cost":          float(r.get("cost") or r.get("price") or r.get("total_cost") or 0),
        "latency_ms":    int(r.get("latency_ms") or r.get("latency") or 0),
        "status":        int(r.get("status") or 200),
        "cached":        int(bool(r.get("cached") or r.get("cache_hit"))),
        "tags":          json.dumps(r.get("tags") or []),
        "raw":           json.dumps(r),
    }

def sync(client, conn, period="30d"):
    results = {}; total = 0
    try:
        days = int(period.rstrip("d")) if period.endswith("d") else 30
        days = min(days, 90)
        end_dt   = datetime.utcnow()
        start_dt = end_dt - timedelta(days=days)
        start    = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        end      = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        keys = client.keys()
        recs = []
        for k in keys:
            kid  = k.get("id"); kname = k.get("name", kid)
            try:
                usage = client.key_usage(kid, start, end)
                for date_str, u in usage.items():
                    recs.append({
                        "id":            f"{kid}_{date_str}",
                        "timestamp":     date_str + "T12:00:00",
                        "model":         kname,
                        "org_id":        kid,
                        "org_name":      kname,
                        "input_tokens":  int(u.get("input_tokens", 0)),
                        "output_tokens": int(u.get("output_tokens", 0)),
                        "total_tokens":  int(u.get("total_tokens", 0)),
                        "cost":          float(u.get("spend", 0)),
                        "latency_ms":    0,
                        "status":        200,
                        "cached":        0,
                        "tags":          "[]",
                        "raw":           json.dumps(u),
                    })
            except Exception as e:
                results[kname] = f"✗ {str(e)[:40]}"
        if recs:
            upsert(conn, recs); total = len(recs)
            results["/v1/manage/apikey"] = f"✓ {total} records ({len(keys)} keys)"
        else:
            results["/v1/manage/apikey"] = "empty"
    except Exception as e:
        results["sync"] = f"✗ {str(e)[:60]}"

    now = datetime.utcnow().isoformat()
    set_meta(conn, "last_sync", now)
    conn.execute("INSERT INTO sync_log (synced_at,endpoint,records,status) VALUES (?,?,?,?)",
                 (now, json.dumps(results), total, "ok" if total>0 else "empty"))
    conn.commit()
    return {"synced_at": now, "total_new": total, "endpoints": results}

# ── Langfuse sync ─────────────────────────────────────────────────────────────
def sync_langfuse(conn):
    if not LF_PUBLIC or not LF_SECRET:
        return 0
    session = requests.Session()
    session.auth = (LF_PUBLIC, LF_SECRET)
    page, limit = 1, 100
    now = datetime.utcnow().isoformat()
    # aggregate per user_id: first_seen, last_seen, count
    users = {}    # user_id -> {"first": ts, "last": ts, "count": int}
    sessions = {} # session_id -> {"user_id": uid, "created_at": ts, "count": int}
    while True:
        try:
            r = session.get(f"{LF_HOST}/api/public/traces",
                            params={"page": page, "limit": limit}, timeout=30)
            r.raise_for_status()
            traces = r.json().get("data", [])
            if not traces:
                break
            for t in traces:
                uid = t.get("userId")
                ts = t.get("timestamp", "")
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
            if len(traces) < limit:
                break
            page += 1
        except Exception as e:
            log.error(f"Langfuse sync error: {e}")
            break
    if users:
        conn.executemany("""INSERT OR REPLACE INTO lf_users
            (user_id, first_seen, last_seen, total_traces, updated_at)
            VALUES (?,?,?,?,?)""",
            [(uid, v["first"], v["last"], v["count"], now) for uid, v in users.items()])
    if sessions:
        conn.executemany("""INSERT OR REPLACE INTO lf_sessions
            (session_id, user_id, created_at, trace_count, updated_at)
            VALUES (?,?,?,?,?)""",
            [(sid, v["user_id"], v["created_at"], v["count"], now) for sid, v in sessions.items()])
    conn.commit()
    log.info(f"  Langfuse: {len(users)} users, {len(sessions)} sessions synced")
    return len(users)

def sync_langfuse_models(conn, days=30):
    """Aggregate Langfuse observations by (model, date) → lf_model_daily."""
    if not LF_PUBLIC or not LF_SECRET:
        return 0
    session = requests.Session()
    session.auth = (LF_PUBLIC, LF_SECRET)
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    page, limit = 1, 100
    now = datetime.utcnow().isoformat()
    # aggregate: (model, date) -> {input, output, total, cost, count}
    agg = {}
    while True:
        try:
            r = session.get(f"{LF_HOST}/api/public/observations",
                            params={"type": "GENERATION", "page": page, "limit": limit},
                            timeout=30)
            r.raise_for_status()
            data = r.json().get("data", [])
            if not data:
                break
            for obs in data:
                ts = obs.get("startTime", "")
                if ts and ts[:19] < cutoff[:19]:
                    # observations are returned newest-first; once we hit old data, stop
                    data = []
                    break
                model = obs.get("model") or "unknown"
                date = ts[:10] if ts else "unknown"
                key = f"{model}|{date}"
                usage = obs.get("usage") or {}
                inp = int(usage.get("input") or obs.get("promptTokens") or 0)
                out = int(usage.get("output") or obs.get("completionTokens") or 0)
                tot = int(usage.get("total") or obs.get("totalTokens") or 0)
                cost = float(obs.get("calculatedTotalCost") or 0)
                if key not in agg:
                    agg[key] = {"model": model, "date": date, "input": 0, "output": 0,
                                "total": 0, "cost": 0.0, "count": 0}
                agg[key]["input"] += inp
                agg[key]["output"] += out
                agg[key]["total"] += tot
                agg[key]["cost"] += cost
                agg[key]["count"] += 1
            log.info(f"  Langfuse models: page {page}, {len(agg)} model/day records so far")
            if not data or len(data) < limit:
                break
            page += 1
        except Exception as e:
            log.error(f"Langfuse models sync error: {e}")
            break
    if agg:
        conn.executemany("""INSERT OR REPLACE INTO lf_model_daily
            (id, date, model, input_tokens, output_tokens, total_tokens, cost, request_count, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            [(f"{v['model']}|{v['date']}", v["date"], v["model"],
              v["input"], v["output"], v["total"], v["cost"], v["count"], now)
             for v in agg.values()])
        conn.commit()
    set_meta(conn, "last_lf_sync", now)
    log.info(f"  Langfuse models: {len(agg)} model/day records synced")
    return len(set(v["model"] for v in agg.values()))

# ── Auth ──────────────────────────────────────────────────────────────────────
def make_auth(token):
    def decorator(f):
        @wraps(f)
        def wrapper(*a, **kw):
            if not token: return f(*a, **kw)
            auth = request.headers.get("Authorization","")
            if auth == f"Bearer {token}": return f(*a, **kw)
            if request.args.get("token") == token: return f(*a, **kw)
            if request.cookies.get("rq_token") == token: return f(*a, **kw)
            return jsonify({"error":"Unauthorized"}), 401
        return wrapper
    return decorator

# ── Flask app ─────────────────────────────────────────────────────────────────
def create_app(conn, client, auth_token=None, html_path=None):
    app = Flask(__name__)
    CORS(app, origins="*", supports_credentials=True)
    auth = make_auth(auth_token)
    R = lambda rows: [dict(r) for r in rows]

    def since(days=30):
        return request.args.get("since", (datetime.utcnow()-timedelta(days=days)).isoformat())

    @app.route("/")
    @app.route("/analytics")
    def index():
        if html_path and Path(html_path).exists():
            return Path(html_path).read_text(), 200, {"Content-Type": "text/html; charset=utf-8"}
        return "Dashboard not found", 404

    @app.route("/health")
    def health():
        n = conn.execute("SELECT COUNT(*) as n FROM requests").fetchone()["n"]
        return jsonify({"status":"ok","records":n,"last_sync":get_meta(conn,"last_sync","never"),
                        "last_lf_sync":get_meta(conn,"last_lf_sync","never"),
                        "auth_required":bool(auth_token)})

    @app.route("/sync", methods=["POST"])
    @auth
    def do_sync():
        period = (request.json or {}).get("period","30d")
        result = sync(client, conn, period)
        result["langfuse_users"] = sync_langfuse(conn)
        result["langfuse_models"] = sync_langfuse_models(conn, days=int(period.rstrip("d")) if period.endswith("d") else 30)
        return jsonify(result)

    @app.route("/requests")
    @auth
    def get_requests():
        q,p = "SELECT * FROM requests WHERE 1=1", []
        if v := request.args.get("org_id"):  q += " AND org_id=?"; p.append(v)
        if v := request.args.get("model"):   q += " AND model=?";  p.append(v)
        if v := request.args.get("since"):   q += " AND timestamp>=?"; p.append(v)
        q += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        p += [int(request.args.get("limit",500)), int(request.args.get("offset",0))]
        total = conn.execute("SELECT COUNT(*) as n FROM requests").fetchone()["n"]
        return jsonify({"data": R(conn.execute(q,p).fetchall()), "total": total})

    @app.route("/analytics/overview")
    @auth
    def overview():
        s = since()
        r = conn.execute("""SELECT COUNT(*) as total_requests, SUM(total_tokens) as total_tokens,
            SUM(input_tokens) as total_input, SUM(output_tokens) as total_output,
            SUM(cost) as total_cost, AVG(latency_ms) as avg_latency,
            COUNT(DISTINCT org_id) as unique_orgs,
            SUM(cached) as cache_hits FROM requests WHERE timestamp>=?""", [s]).fetchone()
        d = dict(r)
        # unique_models comes from Langfuse observations
        s_date = s[:10]
        m = conn.execute("SELECT COUNT(DISTINCT model) as n FROM lf_model_daily WHERE date>=?", [s_date]).fetchone()
        d["unique_models"] = m["n"] if m else 0
        return jsonify(d)

    @app.route("/analytics/by-org")
    @auth
    def by_org():
        s = since()
        return jsonify(R(conn.execute("""SELECT org_id, org_name, COUNT(*) as request_count,
            SUM(total_tokens) as total_tokens, SUM(cost) as total_cost,
            AVG(latency_ms) as avg_latency, MAX(timestamp) as last_seen,
            GROUP_CONCAT(DISTINCT model) as models
            FROM requests WHERE timestamp>=? GROUP BY org_id ORDER BY total_tokens DESC""", [s]).fetchall()))

    @app.route("/analytics/by-model")
    @auth
    def by_model():
        s = since()[:10]  # lf_model_daily.date is YYYY-MM-DD
        rows = conn.execute("""SELECT model, SUM(request_count) as request_count,
            SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens,
            SUM(total_tokens) as total_tokens, SUM(cost) as total_cost
            FROM lf_model_daily WHERE date>=? GROUP BY model ORDER BY total_tokens DESC""", [s]).fetchall()
        return jsonify(R(rows))

    @app.route("/analytics/timeseries")
    @auth
    def timeseries():
        s = since()
        fmt = {"hour":"%Y-%m-%dT%H","day":"%Y-%m-%d","week":"%Y-W%W"}.get(
              request.args.get("granularity","day"),"%Y-%m-%d")
        return jsonify(R(conn.execute(
            f"SELECT strftime('{fmt}',timestamp) as period, COUNT(*) as requests, "
            f"SUM(total_tokens) as tokens, SUM(cost) as cost "
            f"FROM requests WHERE timestamp>=? GROUP BY period ORDER BY period", [s]).fetchall()))

    @app.route("/analytics/churn-risk")
    @auth
    def churn_risk():
        s = since()
        high, low = float(request.args.get("high",2.0)), float(request.args.get("low",0.3))
        orgs = conn.execute("""SELECT org_id, org_name, SUM(total_tokens) as tokens,
            SUM(cost) as cost, COUNT(*) as requests, MAX(timestamp) as last_seen,
            GROUP_CONCAT(DISTINCT model) as models
            FROM requests WHERE timestamp>=? GROUP BY org_id""", [s]).fetchall()
        if not orgs: return jsonify({"over":[],"under":[],"normal":[],"median":0})
        vals = sorted(r["tokens"] for r in orgs)
        median = vals[len(vals)//2]
        over, under, normal = [], [], []
        for r in orgs:
            d = dict(r); ratio = d["tokens"]/median if median else 0
            d.update(median_ratio=round(ratio,2), action=(
                "Eduquer: prompt optimisation, modeles moins chers, caching" if ratio>high else
                "Activer: onboarding, cas d'usage, support proactif" if ratio<low else "Normal"))
            (over if ratio>high else under if ratio<low else normal).append(d)
        return jsonify({"over":sorted(over,key=lambda x:-x["tokens"]),
                        "under":sorted(under,key=lambda x:x["tokens"]),
                        "normal":sorted(normal,key=lambda x:-x["tokens"]),
                        "median":median,"thresholds":{"high":high*median,"low":low*median}})

    @app.route("/analytics/by-user")
    @auth
    def by_user():
        rows = conn.execute("""SELECT user_id, first_seen, last_seen, total_traces, updated_at
            FROM lf_users ORDER BY last_seen DESC""").fetchall()
        now = datetime.utcnow()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["days_since_last"] = (now - datetime.fromisoformat(d["last_seen"][:19])).days
            except Exception:
                d["days_since_last"] = 999
            result.append(d)
        return jsonify(result)

    @app.route("/analytics/by-session")
    @auth
    def by_session():
        rows = conn.execute("""SELECT session_id, user_id, created_at, trace_count, updated_at
            FROM lf_sessions ORDER BY created_at DESC LIMIT 500""").fetchall()
        return jsonify(R(rows))

    @app.route("/analytics/churn-users")
    @auth
    def churn_users():
        rows = conn.execute("""SELECT user_id, first_seen, last_seen, total_traces
            FROM lf_users ORDER BY last_seen DESC""").fetchall()
        now = datetime.utcnow()
        actif, risque, churne = [], [], []
        for r in rows:
            d = dict(r)
            try:
                days = (now - datetime.fromisoformat(d["last_seen"][:19])).days
            except Exception:
                days = 999
            d["days_since_last"] = days
            traces = d.get("total_traces", 0) or 0
            # churné : peu de traces ET inactif depuis > 5 jours
            if traces < 15 and days > 5:
                d["status"] = "churne"; churne.append(d)
            # à risque : peu de traces (mais encore récent)
            elif traces < 15:
                d["status"] = "risque"; risque.append(d)
            # actif : >= 15 traces
            else:
                d["status"] = "actif"; actif.append(d)
        return jsonify({"actif": actif, "risque": risque, "churne": churne,
                        "total": len(rows)})

    @app.route("/export/csv")
    @auth
    def export_csv():
        s = since(90)
        data = conn.execute("""SELECT id,timestamp,org_name,model,total_tokens,
            input_tokens,output_tokens,cost,latency_ms,status,cached
            FROM requests WHERE timestamp>=? ORDER BY timestamp DESC""", [s]).fetchall()
        def gen():
            yield "id,timestamp,org_name,model,total_tokens,input_tokens,output_tokens,cost,latency_ms,status,cached\n"
            for r in data: yield ",".join(str(v or "") for v in r)+"\n"
        return Response(gen(), mimetype="text/csv",
                        headers={"Content-Disposition":"attachment; filename=requesty_export.csv"})

    return app

# ── Share helpers ─────────────────────────────────────────────────────────────
def share_local(port):
    try:    ip = socket.gethostbyname(socket.gethostname())
    except: ip = "?.?.?.?"
    log.info(f"\n{'='*55}")
    log.info(f"  ✅  PARTAGE RÉSEAU LOCAL")
    log.info(f"  Donne ce lien à ton collègue (même WiFi/bureau) :")
    log.info(f"  →  http://{ip}:{port}")
    log.info(f"  (Firewall: autorise le port TCP {port})")
    log.info(f"{'='*55}\n")
    return "0.0.0.0"

def share_ngrok(port, auth_token=None):
    try: from pyngrok import ngrok, conf
    except ImportError:
        log.error("pyngrok non installé → pip install pyngrok")
        sys.exit(1)
    if tok := os.environ.get("NGROK_AUTHTOKEN"):
        conf.get_default().auth_token = tok
    tunnel = ngrok.connect(port, "http")
    url = tunnel.public_url
    log.info(f"\n{'='*55}")
    log.info(f"  ✅  TUNNEL NGROK ACTIF")
    log.info(f"  Partage ce lien à ton collègue :")
    if auth_token:
        log.info(f"  →  {url}?token={auth_token}")
        log.info(f"  Token requis pour accéder au dashboard")
    else:
        log.info(f"  →  {url}")
    log.info(f"  ⚠️   Lien valide tant que le script tourne")
    log.info(f"  💡  Inscris-toi sur ngrok.com (gratuit) pour un")
    log.info(f"      tunnel stable avec NGROK_AUTHTOKEN dans .env")
    log.info(f"{'='*55}\n")
    return "127.0.0.1"

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--key",        required=True)
    p.add_argument("--db",         default=str(DB_PATH))
    p.add_argument("--port",       type=int, default=PORT)
    p.add_argument("--period",     default="30d")
    p.add_argument("--sync",       action="store_true")
    p.add_argument("--serve",      action="store_true")
    p.add_argument("--auto",       action="store_true")
    p.add_argument("--share",      choices=["local","ngrok"], default=None)
    p.add_argument("--auth-token", default=None, dest="auth_token",
                   help="Protège l'API (accès via ?token=XXX ou Bearer XXX)")
    args = p.parse_args()

    conn   = init_db(Path(args.db))
    client = Client(args.key)

    log.info("Connexion à l'API Requesty...")
    try:
        keys = client.keys()
        log.info(f"  ✓ {len(keys)} clés API trouvées")
    except Exception as e:
        log.warning(f"  ⚠ {e}")

    if args.sync:
        print(json.dumps(sync(client, conn, args.period), indent=2)); return

    if not args.serve:
        log.info(f"Sync initiale (period={args.period})...")
        r = sync(client, conn, args.period)
        log.info(f"  {r['total_new']} records importés")
        for ep, st in r["endpoints"].items():
            log.info(f"    {ep}: {st}")

    if args.auto:
        def cron():
            while True:
                time.sleep(SYNC_INTERVAL)
                try:
                    r = sync(client, conn, args.period)
                    sync_langfuse(conn)
                    sync_langfuse_models(conn, days=int(args.period.rstrip("d")) if args.period.endswith("d") else 30)
                    log.info(f"Auto-sync: {r['total_new']} nouveaux records")
                except Exception as e:
                    log.error(f"Auto-sync erreur: {e}")
        threading.Thread(target=cron, daemon=True).start()
        log.info(f"Auto-sync activé (toutes les {SYNC_INTERVAL//60}min)")

    host = "0.0.0.0" if os.path.exists("/.dockerenv") else "127.0.0.1"
    if args.share == "local":   host = share_local(args.port)
    elif args.share == "ngrok": host = share_ngrok(args.port, args.auth_token)
    else:
        n = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        log.info(f"\n  API locale → http://localhost:{args.port}")
        log.info(f"  DB: {args.db} | {n} records\n")

    html = Path(args.db).parent / "dashboard_v3.html"
    create_app(conn, client, auth_token=args.auth_token, html_path=html).run(
        port=args.port, host=host, debug=False)

if __name__ == "__main__":
    main()
