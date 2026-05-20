#!/usr/bin/env python3
"""
Shanty Monitor - Collector
Runs on 192.168.1.168:2777

Responsibilities:
  - Receive metric pushes from agents (POST /api/metrics)
  - Probe-only hosts: ping + port checks on a timer
  - External domain HTTPS checks
  - Store everything in SQLite
  - Write metrics.json for the dashboard (every probe cycle)

Dependencies: flask, pyyaml, requests
  pip3 install flask pyyaml requests
"""

import os
import sys
import time
import json
import math
import socket
import logging
import sqlite3
import threading
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml
import requests
from flask import Flask, request, jsonify, abort

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [collector] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("shanty-collector")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONFIG_PATHS = [
    "/etc/shanty-monitor/config.yaml",
    os.path.join(os.path.dirname(__file__), "config.yaml"),
]

def load_config():
    for path in CONFIG_PATHS:
        if os.path.exists(path):
            with open(path) as f:
                cfg = yaml.safe_load(f)
            log.info(f"Loaded config from {path}")
            return cfg
    log.error("No config.yaml found. Searched: " + ", ".join(CONFIG_PATHS))
    sys.exit(1)

CFG = load_config()
COLLECTOR_CFG  = CFG.get("collector", {})
TOKEN          = COLLECTOR_CFG.get("token", "")
DB_PATH        = COLLECTOR_CFG.get("db_path", "/var/lib/shanty-monitor/metrics.db")
JSON_OUTPUT    = COLLECTOR_CFG.get("json_output", "/var/www/html/dashboard/metrics.json")
PROBE_INTERVAL = int(COLLECTOR_CFG.get("probe_interval", 60))
HISTORY_DAYS   = int(COLLECTOR_CFG.get("history_days", 7))
HOST           = COLLECTOR_CFG.get("host", "0.0.0.0")
PORT           = int(COLLECTOR_CFG.get("port", 2777))

AGENT_HOSTS    = CFG.get("agent_hosts", [])
PROBE_HOSTS    = CFG.get("probe_only_hosts", [])
EXT_DOMAINS    = CFG.get("external_domains", [])

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def db_connect():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    conn = db_connect()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS agent_metrics (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            hostname    TEXT    NOT NULL,
            ts          REAL    NOT NULL,
            cpu_pct     REAL,
            mem_pct     REAL,
            mem_used_mb REAL,
            mem_total_mb REAL,
            load1       REAL,
            load5       REAL,
            load15      REAL,
            uptime_sec  REAL,
            disks_json  TEXT,
            os_json     TEXT
        );

        CREATE TABLE IF NOT EXISTS probe_results (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            hostname    TEXT    NOT NULL,
            ts          REAL    NOT NULL,
            ping_ok     INTEGER,
            ping_ms     REAL,
            ports_json  TEXT    -- {"22": true, "443": false, ...}
        );

        CREATE TABLE IF NOT EXISTS domain_checks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            ts          REAL    NOT NULL,
            status_code INTEGER,
            ok          INTEGER,
            latency_ms  REAL
        );

        CREATE INDEX IF NOT EXISTS idx_agent_ts   ON agent_metrics(hostname, ts);
        CREATE INDEX IF NOT EXISTS idx_probe_ts   ON probe_results(hostname, ts);
        CREATE INDEX IF NOT EXISTS idx_domain_ts  ON domain_checks(name, ts);
    """)
    conn.commit()
    conn.close()
    log.info(f"Database ready: {DB_PATH}")

def db_prune():
    """Remove rows older than HISTORY_DAYS."""
    cutoff = time.time() - HISTORY_DAYS * 86400
    conn = db_connect()
    c = conn.cursor()
    for table, col in [("agent_metrics","ts"), ("probe_results","ts"), ("domain_checks","ts")]:
        c.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,))
    conn.commit()
    conn.close()

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)

def check_token():
    if TOKEN and request.headers.get("X-Shanty-Token") != TOKEN:
        log.warning(f"Bad token from {request.remote_addr}")
        abort(403)

@app.route("/api/metrics", methods=["POST"])
def receive_metrics():
    check_token()
    data = request.get_json(force=True, silent=True)
    if not data:
        abort(400)

    hostname = data.get("hostname", "unknown")
    ts       = data.get("timestamp", time.time())
    mem      = data.get("memory", {})
    load     = data.get("load_avg", [0, 0, 0])
    disks    = data.get("disks", [])
    os_info  = data.get("os", {})

    conn = db_connect()
    conn.execute("""
        INSERT INTO agent_metrics
          (hostname, ts, cpu_pct, mem_pct, mem_used_mb, mem_total_mb,
           load1, load5, load15, uptime_sec, disks_json, os_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        hostname, ts,
        data.get("cpu_percent"), mem.get("percent"),
        mem.get("used_mb"), mem.get("total_mb"),
        load[0] if len(load)>0 else None,
        load[1] if len(load)>1 else None,
        load[2] if len(load)>2 else None,
        data.get("uptime_sec"),
        json.dumps(disks),
        json.dumps(os_info),
    ))
    conn.commit()
    conn.close()
    log.info(f"Stored metrics from {hostname} (cpu={data.get('cpu_percent')}%)")
    write_json()
    return jsonify({"ok": True})

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "ts": time.time()})

# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

def ping_host(ip, count=3, timeout=2):
    """Returns (ok: bool, avg_ms: float|None)"""
    try:
        result = subprocess.run(
            ["ping", "-c", str(count), "-W", str(timeout), ip],
            capture_output=True, text=True, timeout=timeout * count + 5
        )
        if result.returncode == 0:
            # parse avg from "rtt min/avg/max/mdev = X/Y/Z/W ms"
            for line in result.stdout.splitlines():
                if "avg" in line and "=" in line:
                    parts = line.split("=")[1].strip().split("/")
                    avg_ms = float(parts[1])
                    return True, avg_ms
            return True, None
        return False, None
    except Exception as e:
        log.debug(f"ping {ip} error: {e}")
        return False, None

def check_port(ip, port, timeout=3):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False

def probe_host(host):
    ip   = host["ip"]
    name = host["name"]
    ports = host.get("ports", [])

    ping_ok, ping_ms = ping_host(ip)
    port_results = {}
    for p in ports:
        port_results[str(p)] = check_port(ip, p)

    ts = time.time()
    conn = db_connect()
    conn.execute("""
        INSERT INTO probe_results (hostname, ts, ping_ok, ping_ms, ports_json)
        VALUES (?,?,?,?,?)
    """, (name, ts, int(ping_ok), ping_ms, json.dumps(port_results)))
    conn.commit()
    conn.close()
    log.info(f"Probe {name} ({ip}): ping={'ok' if ping_ok else 'FAIL'} ports={port_results}")

def check_domain(domain):
    name = domain["name"]
    url  = domain["url"]
    expected = domain.get("expected_status", 200)
    ts = time.time()
    try:
        t0 = time.time()
        resp = requests.get(url, timeout=10, allow_redirects=True,
                            headers={"User-Agent": "ShantyMonitor/1.0"})
        latency_ms = (time.time() - t0) * 1000
        ok = int(resp.status_code == expected)
        status_code = resp.status_code
    except Exception as e:
        log.warning(f"Domain check {name} failed: {e}")
        latency_ms = None
        ok = 0
        status_code = None

    conn = db_connect()
    conn.execute("""
        INSERT INTO domain_checks (name, ts, status_code, ok, latency_ms)
        VALUES (?,?,?,?,?)
    """, (name, ts, status_code, ok, latency_ms))
    conn.commit()
    conn.close()
    log.info(f"Domain {name}: status={status_code} ok={ok} latency={latency_ms:.1f}ms" if latency_ms else f"Domain {name}: FAIL")

def probe_loop():
    """Background thread: probe all probe-only hosts + external domains."""
    log.info("Probe loop started")
    while True:
        try:
            for host in PROBE_HOSTS:
                try:
                    probe_host(host)
                except Exception as e:
                    log.error(f"Probe error for {host['name']}: {e}")

            # Also probe agent hosts (port check + ping) for redundancy
            for host in AGENT_HOSTS:
                try:
                    probe_host(host)
                except Exception as e:
                    log.error(f"Probe error for {host['name']}: {e}")

            for domain in EXT_DOMAINS:
                try:
                    check_domain(domain)
                except Exception as e:
                    log.error(f"Domain check error for {domain['name']}: {e}")

            db_prune()
            write_json()
        except Exception as e:
            log.error(f"Probe loop error: {e}")
        time.sleep(PROBE_INTERVAL)

# ---------------------------------------------------------------------------
# JSON output for dashboard
# ---------------------------------------------------------------------------

def get_latest_agent_metrics():
    conn = db_connect()
    rows = conn.execute("""
        SELECT a.* FROM agent_metrics a
        INNER JOIN (
            SELECT hostname, MAX(ts) as max_ts FROM agent_metrics GROUP BY hostname
        ) b ON a.hostname = b.hostname AND a.ts = b.max_ts
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_latest_probe_results():
    conn = db_connect()
    rows = conn.execute("""
        SELECT a.* FROM probe_results a
        INNER JOIN (
            SELECT hostname, MAX(ts) as max_ts FROM probe_results GROUP BY hostname
        ) b ON a.hostname = b.hostname AND a.ts = b.max_ts
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_latest_domain_checks():
    conn = db_connect()
    rows = conn.execute("""
        SELECT a.* FROM domain_checks a
        INNER JOIN (
            SELECT name, MAX(ts) as max_ts FROM domain_checks GROUP BY name
        ) b ON a.name = b.name AND a.ts = b.max_ts
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_history(table, name_col, name_val, metric_col, hours=24, points=60):
    """Return up to `points` evenly-sampled readings over the last `hours` hours."""
    since = time.time() - hours * 3600
    conn = db_connect()
    rows = conn.execute(
        f"SELECT ts, {metric_col} FROM {table} WHERE {name_col}=? AND ts>=? ORDER BY ts",
        (name_val, since)
    ).fetchall()
    conn.close()
    if not rows:
        return []
    # Downsample to `points` buckets
    if len(rows) <= points:
        return [{"ts": r["ts"], "v": r[metric_col]} for r in rows]
    step = len(rows) / points
    result = []
    for i in range(points):
        idx = min(int(i * step), len(rows) - 1)
        r = rows[idx]
        result.append({"ts": r["ts"], "v": r[metric_col]})
    return result

def write_json():
    """Build and write metrics.json consumed by the dashboard."""
    try:
        now = time.time()

        # Latest snapshots
        agent_latest  = {r["hostname"]: r for r in get_latest_agent_metrics()}
        probe_latest  = {r["hostname"]: r for r in get_latest_probe_results()}
        domain_latest = {r["name"]: r for r in get_latest_domain_checks()}

        # Build host list (union of agent + all probed hosts)
        all_host_names = set(
            [h["name"] for h in AGENT_HOSTS] +
            [h["name"] for h in PROBE_HOSTS]
        )

        hosts_out = []
        for name in sorted(all_host_names):
            is_agent = any(h["name"] == name for h in AGENT_HOSTS)
            a = agent_latest.get(name)
            p = probe_latest.get(name)

            # Staleness: agent data older than 3x interval = warn
            agent_stale = False
            if a:
                age = now - a["ts"]
                agent_stale = age > PROBE_INTERVAL * 3

            host_entry = {
                "name":        name,
                "is_agent":    is_agent,
                "agent_stale": agent_stale,
                "last_seen":   a["ts"] if a else None,
                "ping_ok":     bool(p["ping_ok"]) if p else None,
                "ping_ms":     p["ping_ms"] if p else None,
                "ports":       json.loads(p["ports_json"]) if p and p["ports_json"] else {},
                "cpu_pct":     a["cpu_pct"] if a else None,
                "mem_pct":     a["mem_pct"] if a else None,
                "mem_used_mb": a["mem_used_mb"] if a else None,
                "mem_total_mb":a["mem_total_mb"] if a else None,
                "load_avg":    [a["load1"], a["load5"], a["load15"]] if a else None,
                "uptime_sec":  a["uptime_sec"] if a else None,
                "disks":       json.loads(a["disks_json"]) if a and a["disks_json"] else [],
                "os":          json.loads(a["os_json"])    if a and a["os_json"]    else {},
                "history": {
                    "cpu":  get_history("agent_metrics", "hostname", name, "cpu_pct"),
                    "mem":  get_history("agent_metrics", "hostname", name, "mem_pct"),
                    "ping": get_history("probe_results",  "hostname", name, "ping_ms"),
                } if is_agent else {
                    "ping": get_history("probe_results", "hostname", name, "ping_ms"),
                },
            }
            hosts_out.append(host_entry)

        domains_out = []
        for d in EXT_DOMAINS:
            name = d["name"]
            rec  = domain_latest.get(name)
            domains_out.append({
                "name":       name,
                "url":        d["url"],
                "ok":         bool(rec["ok"]) if rec else None,
                "status_code":rec["status_code"] if rec else None,
                "latency_ms": rec["latency_ms"]  if rec else None,
                "last_check": rec["ts"]          if rec else None,
                "history":    get_history("domain_checks", "name", name, "latency_ms"),
            })

        output = {
            "generated_at": now,
            "hosts":        hosts_out,
            "domains":      domains_out,
        }

        Path(JSON_OUTPUT).parent.mkdir(parents=True, exist_ok=True)
        tmp = JSON_OUTPUT + ".tmp"
        with open(tmp, "w") as f:
            json.dump(output, f)
        os.replace(tmp, JSON_OUTPUT)
        log.debug(f"Wrote {JSON_OUTPUT}")

    except Exception as e:
        log.error(f"write_json error: {e}", exc_info=True)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    db_init()
    write_json()  # write empty/current snapshot before first probe cycle

    # Start probe loop in background thread
    t = threading.Thread(target=probe_loop, daemon=True)
    t.start()

    log.info(f"Starting Flask on {HOST}:{PORT}")
    # Use threaded=True so probe loop and HTTP requests don't block each other
    app.run(host=HOST, port=PORT, threaded=True, debug=False)
