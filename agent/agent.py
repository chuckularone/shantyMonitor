#!/usr/bin/env python3
"""
Shanty Monitor - Agent
Runs on each managed host. Collects system metrics and pushes
them to the central collector every PUSH_INTERVAL seconds.

Config via /etc/shanty-monitor/agent.yaml or env vars:
  SHANTY_COLLECTOR_URL
  SHANTY_TOKEN
  SHANTY_HOSTNAME (defaults to socket.hostname)
  SHANTY_INTERVAL (seconds, default 60)
"""

import os
import sys
import time
import socket
import logging
import platform
import subprocess
import json
import urllib.request
import urllib.error

# Optional yaml support, fall back to env vars only
try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

# Optional psutil - fall back to /proc parsing if missing
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [agent] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("shanty-agent")

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
CONFIG_PATHS = [
    "/etc/shanty-monitor/agent.yaml",
    os.path.expanduser("~/.shanty-monitor/agent.yaml"),
    os.path.join(os.path.dirname(__file__), "agent.yaml"),
]

def load_config():
    cfg = {}
    if HAS_YAML:
        for path in CONFIG_PATHS:
            if os.path.exists(path):
                with open(path) as f:
                    cfg = yaml.safe_load(f) or {}
                log.info(f"Loaded config from {path}")
                break

    # Env vars override file
    cfg.setdefault("collector_url", os.environ.get("SHANTY_COLLECTOR_URL", "http://192.168.1.168:2777"))
    cfg.setdefault("token",         os.environ.get("SHANTY_TOKEN", "CHANGE_THIS_TO_A_RANDOM_SECRET"))
    cfg.setdefault("hostname",      os.environ.get("SHANTY_HOSTNAME", socket.gethostname()))
    cfg.setdefault("interval",      int(os.environ.get("SHANTY_INTERVAL", "60")))
    return cfg

# ---------------------------------------------------------------------------
# Metric collection
# ---------------------------------------------------------------------------

def cpu_percent():
    if HAS_PSUTIL:
        return psutil.cpu_percent(interval=1)
    # /proc/stat fallback - two samples 1s apart
    def read_stat():
        with open("/proc/stat") as f:
            parts = f.readline().split()
        vals = list(map(int, parts[1:]))
        idle = vals[3]
        total = sum(vals)
        return idle, total
    i1, t1 = read_stat()
    time.sleep(1)
    i2, t2 = read_stat()
    idle_delta = i2 - i1
    total_delta = t2 - t1
    return round(100.0 * (1 - idle_delta / total_delta), 1) if total_delta else 0.0

def mem_info():
    if HAS_PSUTIL:
        m = psutil.virtual_memory()
        return {
            "total_mb": round(m.total / 1024 / 1024, 1),
            "used_mb":  round(m.used  / 1024 / 1024, 1),
            "percent":  m.percent,
        }
    with open("/proc/meminfo") as f:
        lines = f.readlines()
    info = {}
    for line in lines:
        key, val = line.split(":")
        info[key.strip()] = int(val.split()[0])  # kB
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    used  = total - avail
    pct   = round(100.0 * used / total, 1) if total else 0
    return {
        "total_mb": round(total / 1024, 1),
        "used_mb":  round(used  / 1024, 1),
        "percent":  pct,
    }

def disk_info():
    if HAS_PSUTIL:
        partitions = []
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
                partitions.append({
                    "mount":      part.mountpoint,
                    "total_gb":   round(usage.total / 1024**3, 1),
                    "used_gb":    round(usage.used  / 1024**3, 1),
                    "percent":    usage.percent,
                })
            except PermissionError:
                pass
        return partitions
    # df fallback
    try:
        out = subprocess.check_output(
            ["df", "-BG", "--output=target,size,used,pcent"],
            text=True
        ).splitlines()[1:]
        partitions = []
        for line in out:
            parts = line.split()
            if len(parts) < 4:
                continue
            mount = parts[0]
            # skip pseudo filesystems
            if any(mount.startswith(p) for p in ["/sys", "/proc", "/dev", "/run"]):
                continue
            total_g = int(parts[1].rstrip("G"))
            used_g  = int(parts[2].rstrip("G"))
            pct     = int(parts[3].rstrip("%"))
            partitions.append({
                "mount":    mount,
                "total_gb": float(total_g),
                "used_gb":  float(used_g),
                "percent":  pct,
            })
        return partitions
    except Exception as e:
        log.warning(f"disk_info fallback failed: {e}")
        return []

def load_avg():
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        return [float(parts[0]), float(parts[1]), float(parts[2])]
    except Exception:
        return [0.0, 0.0, 0.0]

def uptime_seconds():
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0

def os_info():
    return {
        "system":   platform.system(),
        "release":  platform.release(),
        "machine":  platform.machine(),
        "python":   platform.python_version(),
    }

def collect_metrics(hostname):
    return {
        "hostname":   hostname,
        "timestamp":  time.time(),
        "cpu_percent": cpu_percent(),
        "memory":     mem_info(),
        "disks":      disk_info(),
        "load_avg":   load_avg(),
        "uptime_sec": uptime_seconds(),
        "os":         os_info(),
    }

# ---------------------------------------------------------------------------
# Push to collector
# ---------------------------------------------------------------------------

def push(metrics, collector_url, token):
    url = collector_url.rstrip("/") + "/api/metrics"
    payload = json.dumps(metrics).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Shanty-Token": token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            log.info(f"Pushed metrics -> {resp.status} {body}")
            return True
    except urllib.error.HTTPError as e:
        log.error(f"HTTP error pushing metrics: {e.code} {e.reason}")
    except urllib.error.URLError as e:
        log.error(f"URL error pushing metrics: {e.reason}")
    except Exception as e:
        log.error(f"Unexpected error pushing metrics: {e}")
    return False

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    cfg = load_config()
    log.info(f"Shanty Monitor Agent starting on {cfg['hostname']}")
    log.info(f"Collector: {cfg['collector_url']}  Interval: {cfg['interval']}s")
    log.info(f"psutil available: {HAS_PSUTIL}")

    while True:
        try:
            metrics = collect_metrics(cfg["hostname"])
            push(metrics, cfg["collector_url"], cfg["token"])
        except Exception as e:
            log.error(f"Collection error: {e}")
        time.sleep(cfg["interval"])

if __name__ == "__main__":
    main()
