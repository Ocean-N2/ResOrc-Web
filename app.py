#!/usr/bin/env python3
"""
Resilience Orchestrator v2.2 – Flask Web Backend.
"""
from __future__ import annotations
import asyncio, json, os, sys, threading, time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from flask import Flask, jsonify, render_template, request, Response, send_file

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.loader import load_campaign, load_nodes, validate_campaign
from config.models import AssetStatus, NodeStatus
from targets.manager import TargetManager
from agents.node_manager import NodeManager
from engine.campaign_engine import CampaignEngine
from safety.audit_logger import AuditLogger

app = Flask(__name__, template_folder="templates", static_folder="static")

_state = {
    "campaign": None, "nm": None, "tm": TargetManager(),
    "engine": None, "audit": None, "tick_data": [],
    "campaign_running": False, "campaign_result": None,
}

def _resolve(path):
    base = os.path.dirname(os.path.abspath(__file__))
    return path if os.path.isabs(path) else os.path.join(base, path)

def _init_audit():
    if _state["audit"] is None:
        _state["audit"] = AuditLogger(log_path=_resolve("audit.jsonl"))

def _serialize(obj):
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _serialize(v) for k, v in asdict(obj).items()}
    if hasattr(obj, "value"):
        return obj.value
    if isinstance(obj, list):
        return [_serialize(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    return obj

def _campaign_to_dict(cfg):
    return {
        "campaign_id": cfg.campaign_id, "name": cfg.name,
        "description": cfg.description,
        "global_max_rate_mbps": cfg.global_max_rate_mbps,
        "abort_threshold_mbps": cfg.abort_threshold_mbps,
        "phases": [{"phase_id": p.phase_id, "name": p.name,
                    "attack_mode": p.attack_mode.value, "targets": p.targets,
                    "duration_seconds": p.duration_seconds,
                    "rate": p.rate.value_str, "rate_mbps": p.rate.to_mbps(),
                    "ramp_up_seconds": p.ramp_up_seconds,
                    "ramp_down_seconds": p.ramp_down_seconds,
                    "node_ids": p.node_ids} for p in cfg.phases],
        "nodes": [{"node_id": n.node_id, "host": n.host, "port": n.port,
                   "role": n.role.value, "max_rate_mbps": n.max_rate_mbps,
                   "status": n.status.value} for n in cfg.nodes],
    }

def _targets_to_list(tm):
    return [{"address": t.address, "asset_type": t.asset_type, "port": t.port,
             "status": t.status.value, "latency_ms": round(t.latency_ms, 2),
             "last_checked": t.last_checked} for t in tm.targets]

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/campaign/load", methods=["POST"])
def api_campaign_load():
    _init_audit()
    data = request.get_json(silent=True) or {}
    path = data.get("path", "config/sample_campaign.yaml")
    try:
        cfg = load_campaign(_resolve(path))
        _state["campaign"] = cfg
        _state["nm"] = NodeManager(simulate=True)
        _state["nm"].register_all(cfg.nodes)
        _state["engine"] = None; _state["campaign_result"] = None; _state["tick_data"] = []
        _state["audit"].log_action("web", "load_campaign", path)
        return jsonify({"ok": True, "campaign": _campaign_to_dict(cfg)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

@app.route("/api/campaign", methods=["GET"])
def api_campaign_get():
    cfg = _state["campaign"]
    if cfg is None:
        return jsonify({"ok": False, "error": "No campaign loaded"}), 404
    return jsonify({"ok": True, "campaign": _campaign_to_dict(cfg)})

@app.route("/api/campaign/validate", methods=["POST"])
def api_campaign_validate():
    cfg = _state["campaign"]
    if cfg is None:
        return jsonify({"ok": False, "error": "No campaign loaded"}), 404
    warnings = validate_campaign(cfg)
    return jsonify({"ok": True, "valid": len(warnings) == 0, "warnings": warnings})

@app.route("/api/targets/load", methods=["POST"])
def api_targets_load():
    _init_audit()
    data = request.get_json(silent=True) or {}
    path = data.get("path", "config/sample_targets.rtf")
    try:
        count = _state["tm"].load_from_rtf(_resolve(path))
        _state["audit"].log_action("web", "load_targets", f"{count} targets from {path}")
        return jsonify({"ok": True, "count": count, "targets": _targets_to_list(_state["tm"])})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

@app.route("/api/targets", methods=["GET"])
def api_targets_get():
    return jsonify({"ok": True, "targets": _targets_to_list(_state["tm"]),
                    "summary": _state["tm"].get_summary()})

@app.route("/api/nodes/health", methods=["POST"])
def api_nodes_health():
    nm = _state["nm"]
    if nm is None:
        return jsonify({"ok": False, "error": "No campaign loaded"}), 404
    health = nm.check_health_all()
    nodes = [{"node_id": n.node_id, "host": n.host, "port": n.port,
              "role": n.role.value, "max_rate_mbps": n.max_rate_mbps,
              "status": n.status.value} for n in nm.all_nodes]
    return jsonify({"ok": True, "health": health, "nodes": nodes})

@app.route("/api/nodes", methods=["GET"])
def api_nodes_get():
    nm = _state["nm"]
    if nm is None:
        return jsonify({"ok": True, "nodes": []})
    nodes = [{"node_id": n.node_id, "host": n.host, "port": n.port,
              "role": n.role.value, "max_rate_mbps": n.max_rate_mbps,
              "status": n.status.value} for n in nm.all_nodes]
    return jsonify({"ok": True, "nodes": nodes})

def _run_campaign_thread(phase_selector=None):
    _init_audit()
    cfg = _state["campaign"]; nm = _state["nm"]; tm = _state["tm"]
    engine = CampaignEngine(cfg, nm, tm, simulate=True)
    _state["engine"] = engine; _state["campaign_running"] = True
    _state["tick_data"] = []; _state["campaign_result"] = None
    _state["audit"].log_action("web", "campaign_start", f"Starting {cfg.campaign_id}")
    def on_tick(tick):
        _state["tick_data"].append(tick)
        if len(_state["tick_data"]) > 500:
            _state["tick_data"] = _state["tick_data"][-500:]
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(
            engine.run_campaign(phase_selector=phase_selector, on_tick=on_tick))
        _state["campaign_result"] = result
    except Exception as exc:
        _state["campaign_result"] = {"error": str(exc)}
    finally:
        _state["campaign_running"] = False; loop.close()
        _state["audit"].log_action("web", "campaign_end",
            f"Aborted={_state['campaign_result'].get('aborted', False)}")

@app.route("/api/campaign/run", methods=["POST"])
def api_campaign_run():
    if _state["campaign"] is None:
        return jsonify({"ok": False, "error": "No campaign loaded"}), 404
    if _state["campaign_running"]:
        return jsonify({"ok": False, "error": "Campaign already running"}), 409
    data = request.get_json(silent=True) or {}
    phase_selector = data.get("phases", None)
    t = threading.Thread(target=_run_campaign_thread, args=(phase_selector,), daemon=True)
    t.start()
    return jsonify({"ok": True, "status": "started"})

@app.route("/api/campaign/abort", methods=["POST"])
def api_campaign_abort():
    engine = _state["engine"]
    if engine is None:
        return jsonify({"ok": False, "error": "No active engine"}), 404
    engine.abort()
    _init_audit()
    _state["audit"].log_action("web", "emergency_abort", "Operator triggered abort")
    return jsonify({"ok": True, "status": "abort_sent"})

@app.route("/api/campaign/status", methods=["GET"])
def api_campaign_status():
    ticks = _state["tick_data"][-50:] if _state["tick_data"] else []
    return jsonify({"ok": True, "running": _state["campaign_running"],
                    "result": _state["campaign_result"], "ticks": ticks,
                    "tick_count": len(_state["tick_data"])})

@app.route("/api/stream")
def api_stream():
    def event_stream():
        last_idx = 0
        while True:
            ticks = _state["tick_data"]
            if len(ticks) > last_idx:
                for tick in ticks[last_idx:]:
                    yield f"data: {json.dumps(tick)}\n\n"
                last_idx = len(ticks)
            if not _state["campaign_running"] and last_idx >= len(ticks):
                result = _state["campaign_result"]
                if result:
                    yield f"event: done\ndata: {json.dumps(result, default=str)}\n\n"
                break
            time.sleep(0.5)
    return Response(event_stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/audit", methods=["GET"])
def api_audit():
    _init_audit()
    try:
        entries = _state["audit"].get_entries()
        return jsonify({"ok": True, "entries": [_serialize(e) for e in entries]})
    except Exception as exc:
        return jsonify({"ok": True, "entries": [], "note": str(exc)})

@app.route("/api/report/json", methods=["GET"])
def api_report_json():
    result = _state["campaign_result"]
    if result is None:
        return jsonify({"ok": False, "error": "No campaign result"}), 404
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), **result}
    path = _resolve("report.json")
    with open(path, "w") as fh:
        json.dump(report, fh, indent=2, default=str)
    return send_file(path, as_attachment=True, download_name="report.json")

@app.route("/api/report/text", methods=["GET"])
def api_report_text():
    result = _state["campaign_result"]
    if result is None:
        return jsonify({"ok": False, "error": "No campaign result"}), 404
    lines = ["=" * 60, "RESILIENCE ORCHESTRATOR - CAMPAIGN REPORT", "=" * 60,
             f"Generated : {datetime.now(timezone.utc).isoformat()}",
             f"Campaign  : {result.get('campaign_id', '')} - {result.get('name', '')}",
             f"Aborted   : {'Yes' if result.get('aborted') else 'No'}",
             f"Enforcements: {result.get('total_enforcement_events', 0)}", ""]
    for pr in result.get("phase_results", []):
        lines += [f"-- Phase: {pr.get('phase_id', '?')} - {pr.get('name', '')} --",
                  f"  Status: {pr.get('status', '?')}", f"  Duration: {pr.get('duration_actual_s', 0):.1f}s",
                  f"  Peak TX: {pr.get('peak_tx_mbps', 0):.1f} Mbps",
                  f"  Avg TX: {pr.get('avg_tx_mbps', 0):.1f} Mbps", ""]
    lines.append("=" * 60)
    path = _resolve("report.txt")
    with open(path, "w") as fh:
        fh.write("\n".join(lines))
    return send_file(path, as_attachment=True, download_name="report.txt")

@app.route("/api/enforcement", methods=["GET"])
def api_enforcement():
    engine = _state["engine"]
    if engine is None:
        return jsonify({"ok": True, "events": []})
    events = [_serialize(e) for e in engine.governor.enforcement_log]
    return jsonify({"ok": True, "events": events})

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
