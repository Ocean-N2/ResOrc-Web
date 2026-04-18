#!/usr/bin/env python3
"""
Resilience Orchestrator – Remote Agent Daemon.

Runs on each traffic-generation node (TRex or MHDDoS).
Accepts commands from the central orchestrator via REST API,
manages the local traffic generator, reports telemetry, and
implements a dead-man's switch for autonomous safety.

Usage:
    python agent.py \
        --node-id trex-01 \
        --role trex \
        --port 4501 \
        --auth-token trex01-secret-token \
        --max-rate-mbps 1000 \
        --heartbeat-timeout 30
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Dict, Optional

from flask import Flask, jsonify, request

# ── Logging ────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("agent")

# ── Flask App ──────────────────────────────────────────────────────

app = Flask(__name__)

AGENT_VERSION = "2.1.0"

# ── Global State ───────────────────────────────────────────────────

class AgentState:
    """Mutable singleton holding all runtime state."""

    def __init__(self) -> None:
        # Identity
        self.node_id: str = ""
        self.role: str = "trex"             # "trex" | "mhddos"
        self.auth_token: str = ""
        self.max_rate_mbps: float = 1000.0

        # Paths
        self.trex_daemon_port: int = 4501
        self.mhddos_path: str = "./mhddos"

        # Execution
        self.running: bool = False
        self.current_config: Dict[str, Any] = {}
        self.process: Optional[subprocess.Popen] = None
        self.start_time: float = 0.0

        # TRex handle (real mode)
        self.trex_client = None
        self.trex_available: bool = False

        # Simulated stats
        self._sim_threads: int = 0
        self._sim_mult: float = 0.0

        # Heartbeat / dead-man's switch
        self.last_heartbeat: float = time.monotonic()
        self.heartbeat_timeout: int = 30
        self.deadman_active: bool = False
        self.deadman_triggered: bool = False

        # Agent start time
        self.agent_start: float = time.monotonic()

        # Audit log path
        self.audit_path: str = "agent_audit.jsonl"

        # Lock for thread safety
        self.lock = threading.Lock()


S = AgentState()

# ── Audit Logger ───────────────────────────────────────────────────

def audit_log(action: str, detail: str = "") -> None:
    """Append an entry to the local audit log."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "node_id": S.node_id,
        "action": action,
        "detail": detail,
    }
    try:
        with open(S.audit_path, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass
    log.info(f"AUDIT: {action} – {detail}")

# ── Auth Middleware ─────────────────────────────────────────────────

def require_auth(fn):
    """Decorator: reject requests without a valid auth token."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = request.headers.get("X-Auth-Token", "")
        if S.auth_token and token != S.auth_token:
            audit_log("auth_failure", f"Invalid token from {request.remote_addr}")
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        # Update heartbeat on every authenticated request
        S.last_heartbeat = time.monotonic()
        S.deadman_triggered = False
        return fn(*args, **kwargs)
    return wrapper

# ── TRex Helpers ───────────────────────────────────────────────────

def _trex_connect():
    """Attempt to connect to the local TRex daemon."""
    try:
        from trex_stl_lib.api import STLClient
        client = STLClient(server="127.0.0.1", sync_port=S.trex_daemon_port)
        client.connect()
        client.acquire(force=True)
        client.reset()
        S.trex_client = client
        S.trex_available = True
        log.info("TRex daemon connected")
        return True
    except ImportError:
        log.warning("trex_stl_lib not available – using simulated mode")
        S.trex_available = False
        return False
    except Exception as exc:
        log.warning(f"TRex connect failed: {exc} – using simulated mode")
        S.trex_available = False
        return False


def _trex_start(rate_mbps: float, targets: list, mode: str = "stl"):
    """Start TRex traffic generation."""
    if S.trex_available and S.trex_client:
        try:
            from trex_stl_lib.api import STLStream, STLPktBuilder, STLTXCont
            # Build a simple UDP packet flood profile
            pkt = STLPktBuilder(pkt_len=64)
            stream = STLStream(packet=pkt, mode=STLTXCont())
            S.trex_client.remove_all_streams()
            S.trex_client.add_streams([stream], ports=[0])
            mult = rate_mbps / 10000.0  # fraction of 10Gbps link
            S.trex_client.start(ports=[0], mult=f"{mult}", force=True)
            S._sim_mult = mult
            return True
        except Exception as exc:
            log.error(f"TRex start failed: {exc}")
            return False
    else:
        # Simulated mode
        S._sim_mult = rate_mbps / 10000.0
        return True


def _trex_update_rate(rate_mbps: float):
    """Update TRex traffic rate on the fly."""
    if S.trex_available and S.trex_client:
        try:
            mult = rate_mbps / 10000.0
            S.trex_client.update(ports=[0], mult=f"{mult}")
            S._sim_mult = mult
            return True
        except Exception as exc:
            log.error(f"TRex rate update failed: {exc}")
            return False
    else:
        S._sim_mult = rate_mbps / 10000.0
        return True


def _trex_stop():
    """Stop TRex traffic."""
    if S.trex_available and S.trex_client:
        try:
            S.trex_client.stop(ports=[0])
        except Exception:
            pass
    S._sim_mult = 0.0


def _trex_stats() -> Dict:
    """Get TRex stats (real or simulated)."""
    if S.trex_available and S.trex_client and S.running:
        try:
            stats = S.trex_client.get_stats()
            port = stats.get(0, {})
            return {
                "tx_mbps": round(port.get("tx_bps", 0) / 1e6, 2),
                "rx_mbps": round(port.get("rx_bps", 0) / 1e6, 2),
                "pps": int(port.get("tx_pps", 0)),
                "cpu_pct": round(stats.get("global", {}).get("cpu_util", 0), 1),
                "active_flows": int(port.get("tx_pps", 0) * 0.01),
            }
        except Exception:
            pass
    # Simulated
    if not S.running:
        return {"tx_mbps": 0, "rx_mbps": 0, "pps": 0, "cpu_pct": 0, "active_flows": 0}
    tx = S._sim_mult * 10000 * random.uniform(0.96, 1.04)
    return {
        "tx_mbps": round(tx, 2),
        "rx_mbps": round(tx * random.uniform(0.001, 0.01), 2),
        "pps": int(tx * 1000 / 8 * random.uniform(0.9, 1.1)),
        "cpu_pct": round(min(100, tx / S.max_rate_mbps * 80 + random.uniform(0, 5)), 1),
        "active_flows": int(tx * random.uniform(50, 200)),
    }

# ── MHDDoS Helpers ─────────────────────────────────────────────────

_MHDDOS_METHODS = {
    "tcp_flood": "TCP", "syn_flood": "SYN", "udp_flood": "UDP",
    "http_flood": "GET", "slowloris": "SLOW", "dns_amp": "DNS",
}


def _mhddos_start(attack_mode: str, targets: list, threads: int, duration: int):
    """Start MHDDoS process."""
    method = _MHDDOS_METHODS.get(attack_mode, "TCP")
    target = targets[0] if targets else "127.0.0.1"
    script = os.path.join(S.mhddos_path, "start.py")

    if os.path.exists(script):
        cmd = [sys.executable, script, method, target, str(threads), str(duration)]
        log.info(f"Spawning MHDDoS: {' '.join(cmd)}")
        S.process = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    else:
        # Simulated mode – no real process
        log.info(f"MHDDoS simulated: {method} → {target}, {threads} threads, {duration}s")
        S.process = None

    S._sim_threads = threads


def _mhddos_stop():
    """Graceful stop MHDDoS."""
    if S.process is not None:
        try:
            S.process.terminate()
            S.process.wait(timeout=5)
        except Exception:
            pass
        S.process = None
    S._sim_threads = 0


def _mhddos_kill():
    """Force kill MHDDoS."""
    if S.process is not None:
        try:
            S.process.kill()
        except Exception:
            pass
        S.process = None
    S._sim_threads = 0


def _mhddos_stats() -> Dict:
    """Get MHDDoS stats (real or simulated)."""
    if not S.running:
        return {"tx_mbps": 0, "rx_mbps": 0, "pps": 0, "cpu_pct": 0, "active_flows": 0}

    # Try real network counters
    try:
        import psutil
        net = psutil.net_io_counters()
        cpu = psutil.cpu_percent(interval=0)
        # Rough estimate – psutil gives bytes since boot, not per-second
        # In production you'd diff two samples
    except ImportError:
        pass

    # Simulated
    tx = S._sim_threads * 0.5 * random.uniform(0.85, 1.15)
    return {
        "tx_mbps": round(tx, 2),
        "rx_mbps": round(tx * random.uniform(0.001, 0.01), 2),
        "pps": int(tx * 1000 / 8 * random.uniform(0.8, 1.2)),
        "cpu_pct": round(min(100, S._sim_threads / 20 + random.uniform(0, 10)), 1),
        "active_flows": S._sim_threads * random.randint(2, 6),
    }

# ── Generic Stop/Kill ──────────────────────────────────────────────

def _stop_all():
    """Stop all traffic generation regardless of role."""
    with S.lock:
        if S.role == "trex":
            _trex_stop()
        else:
            _mhddos_stop()
        S.running = False
        S.current_config = {}


def _kill_all():
    """Force-kill all traffic generation."""
    with S.lock:
        if S.role == "trex":
            _trex_stop()
        else:
            _mhddos_kill()
        S.running = False
        S.current_config = {}

# ── Dead-Man's Switch ──────────────────────────────────────────────

def _deadman_thread():
    """Background thread: auto-stop if orchestrator goes silent."""
    while True:
        time.sleep(5)
        if not S.running:
            continue
        age = time.monotonic() - S.last_heartbeat
        if age > S.heartbeat_timeout:
            if not S.deadman_triggered:
                log.critical(
                    f"DEAD-MAN SWITCH ACTIVATED – no heartbeat for {age:.0f}s "
                    f"(threshold: {S.heartbeat_timeout}s)"
                )
                audit_log("deadman_switch", f"No heartbeat for {age:.0f}s – stopping all traffic")
                _kill_all()
                S.deadman_triggered = True
                S.deadman_active = True

# ── Routes ─────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
@require_auth
def route_health():
    uptime = time.monotonic() - S.agent_start
    hb_age = time.monotonic() - S.last_heartbeat
    cpu_pct = 0
    mem_pct = 0
    try:
        import psutil
        cpu_pct = psutil.cpu_percent(interval=0)
        mem_pct = psutil.virtual_memory().percent
    except ImportError:
        pass
    return jsonify({
        "ok": True,
        "node_id": S.node_id,
        "role": S.role,
        "running": S.running,
        "uptime_s": round(uptime, 1),
        "cpu_pct": round(cpu_pct, 1),
        "mem_pct": round(mem_pct, 1),
        "tx_mbps": (_trex_stats if S.role == "trex" else _mhddos_stats)().get("tx_mbps", 0),
        "max_rate_mbps": S.max_rate_mbps,
        "agent_version": AGENT_VERSION,
        "heartbeat_age_s": round(hb_age, 1),
        "deadman_active": S.deadman_active,
        "deadman_triggered": S.deadman_triggered,
    })


@app.route("/start", methods=["POST"])
@require_auth
def route_start():
    data = request.get_json(silent=True) or {}
    attack_mode = data.get("attack_mode", "tcp_flood")
    targets = data.get("targets", [])
    rate_mbps = min(float(data.get("rate_mbps", 100)), S.max_rate_mbps)
    duration = int(data.get("duration_seconds", 60))
    ramp_up = int(data.get("ramp_up_seconds", 0))

    # Stop any existing traffic first
    _stop_all()

    with S.lock:
        S.current_config = {
            "attack_mode": attack_mode, "targets": targets,
            "rate_mbps": rate_mbps, "duration_seconds": duration,
            "ramp_up_seconds": ramp_up,
        }
        S.start_time = time.monotonic()
        S.deadman_active = False
        S.deadman_triggered = False

        if S.role == "trex":
            _trex_connect()
            ok = _trex_start(rate_mbps, targets)
        else:
            threads = max(1, min(2048, int(rate_mbps / 0.5)))
            _mhddos_start(attack_mode, targets, threads, duration)
            ok = True

        S.running = True

    audit_log("start", json.dumps(S.current_config))
    return jsonify({"ok": True, "status": "started", "config": S.current_config})


@app.route("/stop", methods=["POST"])
@require_auth
def route_stop():
    _stop_all()
    audit_log("stop", "Graceful stop")
    return jsonify({"ok": True, "status": "stopped"})


@app.route("/kill", methods=["POST"])
@require_auth
def route_kill():
    _kill_all()
    audit_log("kill", "Force kill (kill switch)")
    return jsonify({"ok": True, "status": "killed"})


@app.route("/rate", methods=["PUT"])
@require_auth
def route_rate():
    data = request.get_json(silent=True) or {}
    new_rate = min(float(data.get("rate_mbps", 100)), S.max_rate_mbps)

    with S.lock:
        if S.role == "trex":
            _trex_update_rate(new_rate)
        else:
            # MHDDoS: restart with new threads
            old_cfg = S.current_config.copy()
            _mhddos_stop()
            threads = max(1, min(2048, int(new_rate / 0.5)))
            _mhddos_start(
                old_cfg.get("attack_mode", "tcp_flood"),
                old_cfg.get("targets", []),
                threads,
                int(old_cfg.get("duration_seconds", 60)),
            )
            S._sim_threads = threads

        S.current_config["rate_mbps"] = new_rate

    audit_log("rate_update", f"New rate: {new_rate} Mbps")
    return jsonify({"ok": True, "status": "updated", "new_rate_mbps": new_rate})


@app.route("/stats", methods=["GET"])
@require_auth
def route_stats():
    if S.role == "trex":
        stats = _trex_stats()
    else:
        stats = _mhddos_stats()

    uptime = time.monotonic() - S.start_time if S.running else 0
    stats.update({
        "running": S.running,
        "uptime_s": round(uptime, 1),
    })
    return jsonify({"ok": True, **stats})


@app.route("/status", methods=["GET"])
@require_auth
def route_status():
    hb_age = time.monotonic() - S.last_heartbeat
    uptime = time.monotonic() - S.start_time if S.running else 0
    return jsonify({
        "ok": True,
        "running": S.running,
        "current_config": S.current_config,
        "uptime_s": round(uptime, 1),
        "heartbeat_age_s": round(hb_age, 1),
        "deadman_active": S.deadman_active,
        "deadman_triggered": S.deadman_triggered,
        "agent_version": AGENT_VERSION,
    })

# ── CORS ───────────────────────────────────────────────────────────

@app.after_request
def cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Auth-Token"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, OPTIONS"
    return response

# ── Graceful Shutdown ──────────────────────────────────────────────

def _signal_handler(signum, frame):
    log.info(f"Signal {signum} received – stopping traffic and exiting")
    audit_log("shutdown", f"Signal {signum}")
    _kill_all()
    sys.exit(0)

signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)

# ── Entry Point ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Resilience Orchestrator – Remote Agent")
    parser.add_argument("--node-id", required=True, help="Unique node identifier")
    parser.add_argument("--role", required=True, choices=["trex", "mhddos"],
                        help="Agent role")
    parser.add_argument("--port", type=int, default=4501,
                        help="HTTP listen port (default: 4501)")
    parser.add_argument("--auth-token", default="",
                        help="Authentication token")
    parser.add_argument("--max-rate-mbps", type=float, default=1000.0,
                        help="Maximum rate ceiling (Mbps)")
    parser.add_argument("--heartbeat-timeout", type=int, default=30,
                        help="Dead-man switch timeout in seconds (default: 30)")
    parser.add_argument("--trex-daemon-port", type=int, default=4501,
                        help="Local TRex daemon sync port")
    parser.add_argument("--mhddos-path", default="./mhddos",
                        help="Path to MHDDoS installation")
    parser.add_argument("--audit-log", default="agent_audit.jsonl",
                        help="Local audit log path")

    args = parser.parse_args()

    # Apply config
    S.node_id = args.node_id
    S.role = args.role
    S.auth_token = args.auth_token
    S.max_rate_mbps = args.max_rate_mbps
    S.heartbeat_timeout = args.heartbeat_timeout
    S.trex_daemon_port = args.trex_daemon_port
    S.mhddos_path = args.mhddos_path
    S.audit_path = args.audit_log
    S.last_heartbeat = time.monotonic()

    log.info(f"Starting agent: {S.node_id} (role={S.role}, max={S.max_rate_mbps} Mbps)")
    log.info(f"Dead-man switch timeout: {S.heartbeat_timeout}s")
    audit_log("agent_start", f"node={S.node_id} role={S.role} max={S.max_rate_mbps}Mbps")

    # Start dead-man's switch thread
    dm = threading.Thread(target=_deadman_thread, daemon=True)
    dm.start()

    # Start Flask
    app.run(host="0.0.0.0", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
