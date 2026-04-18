"""
YAML configuration loader and validator.
"""
from __future__ import annotations
from typing import List
import yaml
from config.models import (
    AttackMode, CampaignConfig, NodeConfig, NodeRole, PhaseConfig, RateConfig,
)


def _parse_node(raw: dict) -> NodeConfig:
    return NodeConfig(
        node_id=raw["node_id"], host=raw["host"], port=int(raw["port"]),
        role=NodeRole(raw["role"]), max_rate_mbps=float(raw["max_rate_mbps"]),
        auth_token=raw.get("auth_token", ""),
    )


def _parse_phase(raw: dict) -> PhaseConfig:
    return PhaseConfig(
        phase_id=raw["phase_id"], name=raw["name"],
        attack_mode=AttackMode(raw["attack_mode"]),
        targets=raw.get("targets", []),
        duration_seconds=int(raw["duration_seconds"]),
        rate=RateConfig(value_str=str(raw["rate"])),
        ramp_up_seconds=int(raw.get("ramp_up_seconds", 30)),
        ramp_down_seconds=int(raw.get("ramp_down_seconds", 10)),
        node_ids=raw.get("node_ids", []),
    )


def load_campaign(path: str) -> CampaignConfig:
    with open(path, "r") as fh:
        data = yaml.safe_load(fh)
    nodes = [_parse_node(n) for n in data.get("nodes", [])]
    phases = [_parse_phase(p) for p in data.get("phases", [])]
    return CampaignConfig(
        campaign_id=data["campaign_id"], name=data["name"],
        description=data.get("description", ""), phases=phases, nodes=nodes,
        global_max_rate_mbps=float(data.get("global_max_rate_mbps", 2000)),
        abort_threshold_mbps=float(data.get("abort_threshold_mbps", 2500)),
    )


def load_nodes(path: str) -> List[NodeConfig]:
    with open(path, "r") as fh:
        data = yaml.safe_load(fh)
    nodes_raw = data if isinstance(data, list) else data.get("nodes", [])
    return [_parse_node(n) for n in nodes_raw]


def validate_campaign(cfg: CampaignConfig) -> List[str]:
    warnings: List[str] = []
    node_ids = {n.node_id for n in cfg.nodes}
    for phase in cfg.phases:
        for nid in phase.node_ids:
            if nid not in node_ids:
                warnings.append(f"Phase '{phase.phase_id}' references unknown node '{nid}'")
        if phase.duration_seconds <= 0:
            warnings.append(f"Phase '{phase.phase_id}' has non-positive duration")
        phase_mbps = phase.rate.to_mbps()
        assigned = [n for n in cfg.nodes if n.node_id in phase.node_ids]
        total_cap = sum(n.max_rate_mbps for n in assigned) if assigned else 0
        if assigned and phase_mbps > total_cap:
            warnings.append(
                f"Phase '{phase.phase_id}' rate ({phase_mbps:.0f} Mbps) "
                f"exceeds assigned node capacity ({total_cap:.0f} Mbps)")
        if phase_mbps > cfg.global_max_rate_mbps:
            warnings.append(
                f"Phase '{phase.phase_id}' rate ({phase_mbps:.0f} Mbps) "
                f"exceeds global max ({cfg.global_max_rate_mbps:.0f} Mbps)")
    return warnings
