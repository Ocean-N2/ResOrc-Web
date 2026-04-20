#!/usr/bin/env python3
"""
Resilience Orchestrator v2.2 – Flask Web Backend.
Extended with full CRUD for Nodes, Targets, Campaign, and Phases.
"""
from __future__ import annotations

import asyncio, json, os, sys, threading, time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import yaml
from flask import Flask, jsonify, render_template, request, Response, send_file

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.loader import load_campaign, load_nodes, validate_campaign
from config.models import (
    AssetStatus, AttackMode, CampaignConfig, NodeConfig, NodeRole,
    NodeStatus, PhaseConfig, RateConfig, TargetAsset,
)
from targets.manager import TargetManager
from targets.reader import extract_targets
from agents.node_manager import NodeManager
from engine.campaign_engine import CampaignEngine
from safety.audit_logger import AuditLogger

app = Flask(__name__, template_folder="templates", static_folder="static")

# ── Global State ───────────────────────────────────────────────────

_state = {
    "campaign": None,
    "nm": None,
    "tm": TargetManager(),
    "engine": None,
    "audit": None,
    "tick_data": [],
    "campaign_running": False,
    "campaign_result": None,
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
        "phases": [{
            "phase_id": p.phase_id, "name": p.name,
            "attack_mode": p.attack_mode.value, "targets": p.targets,
            "duration_seconds": p.duration_seconds,
            "rate": p.rate.value_str, "rate_mbps": p.rate.to_mbps(),
            "ramp_up_seconds": p.ramp_up_seconds,
            "ramp_down_seconds": p.ramp_down_seconds,
            "node_ids": p.node_ids,
        } for p in cfg.phases],
        "nodes": [{
            "node_id": n.node_id, "host": n.host, "port": n.port,
            "role": n.role.value, "max_rate_mbps": n.max_rate_mbps,
            "status": n.status.value, "auth_token": n.auth_token,
        } for n in cfg.nodes],
    }


def _targets_to_list(tm):
    return [{
        "address": t.address, "asset_type": t.asset_type, "port": t.port,
        "status": t.status.value, "latency_ms": round(t.latency_ms, 2),
        "last_checked": t.last_checked,
    } for t in tm.targets]


def _rebuild_nm():
    """Rebuild NodeManager from current campaign nodes."""
    cfg = _state["campaign"]
    if cfg:
        nm = NodeManager(simulate=True)
        nm.register_all(cfg.nodes)
        _state["nm"] = nm


# ═══════════════════════════════════════════════════════════════
# PAGES
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


# ═══════════════════════════════════════════════════════════════
# CAMPAIGN — LOAD / GET / VALIDATE / UPDATE / SAVE / EXPORT
# ═══════════════════════════════════════════════════════════════

@app.route("/api/campaign/load", methods=["POST"])
def api_campaign_load():
    _init_audit()
    data = request.get_json(silent=True) or {}
    path = data.get("path", "config/sample_campaign.yaml")
    try:
        cfg = load_campaign(_resolve(path))
        _state["campaign"] = cfg
        _rebuild_nm()
        _state["engine"] = None
        _state["campaign_result"] = None
        _state["tick_data"] = []
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


@app.route("/api/campaign/update", methods=["PUT"])
def api_campaign_update():
    """Update campaign-level metadata."""
    cfg = _state["campaign"]
    if cfg is None:
        return jsonify({"ok": False, "error": "No campaign loaded"}), 404
    data = request.get_json(silent=True) or {}
    if "campaign_id" in data:
        cfg.campaign_id = data["campaign_id"]
    if "name" in data:
        cfg.name = data["name"]
    if "description" in data:
        cfg.description = data["description"]
    if "global_max_rate_mbps" in data:
        cfg.global_max_rate_mbps = float(data["global_max_rate_mbps"])
    if "abort_threshold_mbps" in data:
        cfg.abort_threshold_mbps = float(data["abort_threshold_mbps"])
    _init_audit()
    _state["audit"].log_action("web", "update_campaign", json.dumps(data))
    return jsonify({"ok": True, "campaign": _campaign_to_dict(cfg)})


@app.route("/api/campaign/save", methods=["POST"])
def api_campaign_save():
    """Save current campaign state to YAML."""
    cfg = _state["campaign"]
    if cfg is None:
        return jsonify({"ok": False, "error": "No campaign loaded"}), 404
    data = request.get_json(silent=True) or {}
    path = _resolve(data.get("path", "config/sample_campaign.yaml"))
    out = {
        "campaign_id": cfg.campaign_id, "name": cfg.name,
        "description": cfg.description,
        "global_max_rate_mbps": cfg.global_max_rate_mbps,
        "abort_threshold_mbps": cfg.abort_threshold_mbps,
        "nodes": [{
            "node_id": n.node_id, "host": n.host, "port": n.port,
            "role": n.role.value, "max_rate_mbps": n.max_rate_mbps,
            "auth_token": n.auth_token,
        } for n in cfg.nodes],
        "phases": [{
            "phase_id": p.phase_id, "name": p.name,
            "attack_mode": p.attack_mode.value, "targets": p.targets,
            "duration_seconds": p.duration_seconds, "rate": p.rate.value_str,
            "ramp_up_seconds": p.ramp_up_seconds,
            "ramp_down_seconds": p.ramp_down_seconds,
            "node_ids": p.node_ids,
        } for p in cfg.phases],
    }
    with open(path, "w") as fh:
        yaml.dump(out, fh, default_flow_style=False, sort_keys=False)
    _init_audit()
    _state["audit"].log_action("web", "save_campaign", path)
    return jsonify({"ok": True, "path": path})


@app.route("/api/campaign/export", methods=["GET"])
def api_campaign_export():
    """Export campaign as downloadable YAML."""
    cfg = _state["campaign"]
    if cfg is None:
        return jsonify({"ok": False, "error": "No campaign loaded"}), 404
    out = {
        "campaign_id": cfg.campaign_id, "name": cfg.name,
        "description": cfg.description,
        "global_max_rate_mbps": cfg.global_max_rate_mbps,
        "abort_threshold_mbps": cfg.abort_threshold_mbps,
        "nodes": [{"node_id": n.node_id, "host": n.host, "port": n.port,
                    "role": n.role.value, "max_rate_mbps": n.max_rate_mbps,
                    "auth_token": n.auth_token} for n in cfg.nodes],
        "phases": [{"phase_id": p.phase_id, "name": p.name,
                    "attack_mode": p.attack_mode.value, "targets": p.targets,
                    "duration_seconds": p.duration_seconds, "rate": p.rate.value_str,
                    "ramp_up_seconds": p.ramp_up_seconds,
                    "ramp_down_seconds": p.ramp_down_seconds,
                    "node_ids": p.node_ids} for p in cfg.phases],
    }
    path = _resolve("campaign_export.yaml")
    with open(path, "w") as fh:
        yaml.dump(out, fh, default_flow_style=False, sort_keys=False)
    return send_file(path, as_attachment=True, download_name="campaign.yaml")


# ═══════════════════════════════════════════════════════════════
# NODES — CRUD
# ═══════════════════════════════════════════════════════════════

@app.route("/api/nodes", methods=["GET"])
def api_nodes_get():
    nm = _state["nm"]
    if nm is None:
        return jsonify({"ok": True, "nodes": []})
    nodes = []
    for n in nm.all_nodes:
        nodes.append({
            "node_id": n.node_id,
            "host": n.host,
            "port": n.port,
            "role": n.role.value,
            "max_rate_mbps": n.max_rate_mbps,
            "auth_token": n.auth_token,
            "status": getattr(n, "health_status", n.status.value),
            "last_healthcheck": getattr(n, "last_healthcheck", None)
        })
    return jsonify({"ok": True, "nodes": nodes})


app.route("/api/nodes/health", methods=["POST"])
def api_nodes_health():
    nm = _state["nm"]
    if nm is None:
        return jsonify({"ok": False, "error": "No campaign loaded"}), 404

    health = nm.check_health_all()
    now = time.time()
    nodes = []
    for n in nm.all_nodes:
        status = "online" if health.get(n.node_id) else "offline"
        n.last_healthcheck = now
        n.health_status = status
        nodes.append({
            "node_id": n.node_id,
            "host": n.host,
            "port": n.port,
            "role": n.role.value,
            "max_rate_mbps": n.max_rate_mbps,
            "auth_token": n.auth_token,
            "status": status,
            "last_healthcheck": now
        })
    return jsonify({"ok": True, "nodes": nodes})



@app.route("/api/nodes/add", methods=["POST"])
def api_nodes_add():
    """Add a new node to the campaign."""
    cfg = _state["campaign"]
    if cfg is None:
        return jsonify({"ok": False, "error": "No campaign loaded"}), 404
    data = request.get_json(silent=True) or {}
    required = ["node_id", "host", "port", "role", "max_rate_mbps"]
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify({"ok": False, "error": f"Missing fields: {', '.join(missing)}"}), 400
    # Check duplicate
    if any(n.node_id == data["node_id"] for n in cfg.nodes):
        return jsonify({"ok": False, "error": f"Node '{data['node_id']}' already exists"}), 409
    try:
        node = NodeConfig(
            node_id=data["node_id"], host=data["host"],
            port=int(data["port"]), role=NodeRole(data["role"]),
            max_rate_mbps=float(data["max_rate_mbps"]),
            auth_token=data.get("auth_token", ""),
        )
        cfg.nodes.append(node)
        _rebuild_nm()
        _init_audit()
        _state["audit"].log_action("web", "add_node", data["node_id"])
        return jsonify({"ok": True, "node": _serialize(node)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/nodes/<node_id>", methods=["PUT"])
def api_nodes_update(node_id):
    """Update an existing node."""
    cfg = _state["campaign"]
    if cfg is None:
        return jsonify({"ok": False, "error": "No campaign loaded"}), 404
    node = next((n for n in cfg.nodes if n.node_id == node_id), None)
    if node is None:
        return jsonify({"ok": False, "error": f"Node '{node_id}' not found"}), 404
    data = request.get_json(silent=True) or {}
    if "host" in data:
        node.host = data["host"]
    if "port" in data:
        node.port = int(data["port"])
    if "role" in data:
        node.role = NodeRole(data["role"])
    if "max_rate_mbps" in data:
        node.max_rate_mbps = float(data["max_rate_mbps"])
    if "auth_token" in data:
        node.auth_token = data["auth_token"]
    _rebuild_nm()
    _init_audit()
    _state["audit"].log_action("web", "update_node", node_id)
    return jsonify({"ok": True, "node": _serialize(node)})


@app.route("/api/nodes/<node_id>", methods=["DELETE"])
def api_nodes_delete(node_id):
    """Remove a node from the campaign."""
    cfg = _state["campaign"]
    if cfg is None:
        return jsonify({"ok": False, "error": "No campaign loaded"}), 404
    before = len(cfg.nodes)
    cfg.nodes = [n for n in cfg.nodes if n.node_id != node_id]
    if len(cfg.nodes) == before:
        return jsonify({"ok": False, "error": f"Node '{node_id}' not found"}), 404
    # Remove from all phases too
    for phase in cfg.phases:
        phase.node_ids = [nid for nid in phase.node_ids if nid != node_id]
    _rebuild_nm()
    _init_audit()
    _state["audit"].log_action("web", "delete_node", node_id)
    return jsonify({"ok": True, "deleted": node_id})


# ═══════════════════════════════════════════════════════════════
# TARGETS — CRUD
# ═══════════════════════════════════════════════════════════════

@app.route("/api/targets", methods=["GET"])
def api_targets_get():
    return jsonify({"ok": True, "targets": _targets_to_list(_state["tm"]),
                    "summary": _state["tm"].get_summary()})


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


@app.route("/api/targets/add", methods=["POST"])
def api_targets_add():
    """Add targets manually by address."""
    data = request.get_json(silent=True) or {}
    addresses = data.get("addresses", [])
    if "address" in data:
        addresses.append(data["address"])
    if not addresses:
        return jsonify({"ok": False, "error": "No addresses provided"}), 400
    count = _state["tm"].load_from_list(addresses)
    _init_audit()
    _state["audit"].log_action("web", "add_targets", f"{count} targets added manually")
    return jsonify({"ok": True, "count": count, "targets": _targets_to_list(_state["tm"])})


@app.route("/api/targets/<int:index>", methods=["DELETE"])
def api_targets_delete(index):
    """Remove a target by index."""
    tm = _state["tm"]
    if index < 0 or index >= len(tm.targets):
        return jsonify({"ok": False, "error": "Index out of range"}), 404
    removed = tm.targets.pop(index)
    _init_audit()
    _state["audit"].log_action("web", "delete_target", removed.address)
    return jsonify({"ok": True, "deleted": removed.address, "targets": _targets_to_list(tm)})


@app.route("/api/targets/clear", methods=["DELETE"])
def api_targets_clear():
    """Clear all targets."""
    count = len(_state["tm"].targets)
    _state["tm"].targets = []
    _init_audit()
    _state["audit"].log_action("web", "clear_targets", f"Cleared {count} targets")
    return jsonify({"ok": True, "cleared": count})


# ═══════════════════════════════════════════════════════════════
# PHASES — CRUD
# ═══════════════════════════════════════════════════════════════

@app.route("/api/campaign/phases/add", methods=["POST"])
def api_phases_add():
    """Add a new phase to the campaign."""
    cfg = _state["campaign"]
    if cfg is None:
        return jsonify({"ok": False, "error": "No campaign loaded"}), 404
    data = request.get_json(silent=True) or {}
    required = ["phase_id", "name", "attack_mode", "duration_seconds", "rate"]
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify({"ok": False, "error": f"Missing fields: {', '.join(missing)}"}), 400
    if any(p.phase_id == data["phase_id"] for p in cfg.phases):
        return jsonify({"ok": False, "error": f"Phase '{data['phase_id']}' already exists"}), 409
    try:
        phase = PhaseConfig(
            phase_id=data["phase_id"], name=data["name"],
            attack_mode=AttackMode(data["attack_mode"]),
            targets=data.get("targets", []),
            duration_seconds=int(data["duration_seconds"]),
            rate=RateConfig(value_str=str(data["rate"])),
            ramp_up_seconds=int(data.get("ramp_up_seconds", 30)),
            ramp_down_seconds=int(data.get("ramp_down_seconds", 10)),
            node_ids=data.get("node_ids", []),
        )
        cfg.phases.append(phase)
        _init_audit()
        _state["audit"].log_action("web", "add_phase", data["phase_id"])
        return jsonify({"ok": True, "campaign": _campaign_to_dict(cfg)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/campaign/phases/<phase_id>", methods=["PUT"])
def api_phases_update(phase_id):
    """Update an existing phase."""
    cfg = _state["campaign"]
    if cfg is None:
        return jsonify({"ok": False, "error": "No campaign loaded"}), 404
    phase = next((p for p in cfg.phases if p.phase_id == phase_id), None)
    if phase is None:
        return jsonify({"ok": False, "error": f"Phase '{phase_id}' not found"}), 404
    data = request.get_json(silent=True) or {}
    if "name" in data:
        phase.name = data["name"]
    if "attack_mode" in data:
        phase.attack_mode = AttackMode(data["attack_mode"])
    if "targets" in data:
        phase.targets = data["targets"]
    if "duration_seconds" in data:
        phase.duration_seconds = int(data["duration_seconds"])
    if "rate" in data:
        phase.rate = RateConfig(value_str=str(data["rate"]))
    if "ramp_up_seconds" in data:
        phase.ramp_up_seconds = int(data["ramp_up_seconds"])
    if "ramp_down_seconds" in data:
        phase.ramp_down_seconds = int(data["ramp_down_seconds"])
    if "node_ids" in data:
        phase.node_ids = data["node_ids"]
    _init_audit()
    _state["audit"].log_action("web", "update_phase", phase_id)
    return jsonify({"ok": True, "campaign": _campaign_to_dict(cfg)})


@app.route("/api/campaign/phases/<phase_id>", methods=["DELETE"])
def api_phases_delete(phase_id):
    """Remove a phase from the campaign."""
    cfg = _state["campaign"]
    if cfg is None:
        return jsonify({"ok": False, "error": "No campaign loaded"}), 404
    before = len(cfg.phases)
    cfg.phases = [p for p in cfg.phases if p.phase_id != phase_id]
    if len(cfg.phases) == before:
        return jsonify({"ok": False, "error": f"Phase '{phase_id}' not found"}), 404
    _init_audit()
    _state["audit"].log_action("web", "delete_phase", phase_id)
    return jsonify({"ok": True, "deleted": phase_id, "campaign": _campaign_to_dict(cfg)})


@app.route("/api/campaign/phases/reorder", methods=["POST"])
def api_phases_reorder():
    """Reorder phases by a list of phase_ids."""
    cfg = _state["campaign"]
    if cfg is None:
        return jsonify({"ok": False, "error": "No campaign loaded"}), 404
    data = request.get_json(silent=True) or {}
    order = data.get("order", [])
    phase_map = {p.phase_id: p for p in cfg.phases}
    try:
        cfg.phases = [phase_map[pid] for pid in order if pid in phase_map]
        # Append any phases not in order list at the end
        remaining = [p for p in phase_map.values() if p.phase_id not in order]
        cfg.phases.extend(remaining)
        return jsonify({"ok": True, "campaign": _campaign_to_dict(cfg)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


# ═══════════════════════════════════════════════════════════════
# EXECUTION (unchanged)
# ═══════════════════════════════════════════════════════════════

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
    t = threading.Thread(target=_run_campaign_thread,
                         args=(data.get("phases"),), daemon=True)
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


# ═══════════════════════════════════════════════════════════════
# AUDIT / ENFORCEMENT / REPORTS
# ═══════════════════════════════════════════════════════════════

@app.route("/api/audit", methods=["GET"])
def api_audit():
    _init_audit()
    try:
        entries = _state["audit"].get_entries()
        return jsonify({"ok": True, "entries": [_serialize(e) for e in entries]})
    except Exception as exc:
        return jsonify({"ok": True, "entries": [], "note": str(exc)})


@app.route("/api/enforcement", methods=["GET"])
def api_enforcement():
    engine = _state["engine"]
    if engine is None:
        return jsonify({"ok": True, "events": []})
    return jsonify({"ok": True, "events": [_serialize(e) for e in engine.governor.enforcement_log]})


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
                  f"  Status: {pr.get('status', '?')}",
                  f"  Duration: {pr.get('duration_actual_s', 0):.1f}s",
                  f"  Peak TX: {pr.get('peak_tx_mbps', 0):.1f} Mbps",
                  f"  Avg TX: {pr.get('avg_tx_mbps', 0):.1f} Mbps", ""]
    lines.append("=" * 60)
    path = _resolve("report.txt")
    with open(path, "w") as fh:
        fh.write("\n".join(lines))
    return send_file(path, as_attachment=True, download_name="report.txt")


# ═══════════════════════════════════════════════════════════════
# CORS
# ═══════════════════════════════════════════════════════════════

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return response


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
