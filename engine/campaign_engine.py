"""
CampaignEngine – full lifecycle orchestration.
Updated to manage heartbeat thread for dead-man's switch.
"""
from __future__ import annotations
import asyncio, time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from config.models import (
    AttackMode, CampaignConfig, MetricsSnapshot, PhaseConfig, PhaseStatus, RateConfig,
)
from agents.node_manager import NodeManager
from agents.trex_controller import TrexController
from agents.mhddos_controller import MHDDoSController
from engine.rate_governor import RampController, RateGovernor
from targets.manager import TargetManager

_ASTF_MODES = {AttackMode.HTTP_FLOOD, AttackMode.SLOWLORIS, AttackMode.DNS_AMP}


class CampaignEngine:
    def __init__(self, campaign: CampaignConfig, node_manager: NodeManager,
                 target_manager: TargetManager, simulate: bool = True) -> None:
        self.campaign = campaign
        self.nm = node_manager
        self.tm = target_manager
        self.simulate = simulate
        self.governor = RateGovernor(campaign.global_max_rate_mbps, campaign.abort_threshold_mbps)
        self._aborted = False

    def pre_flight(self) -> Dict[str, Any]:
        from config.loader import validate_campaign
        health = self.nm.check_health_all()
        warnings = validate_campaign(self.campaign)
        return {"node_health": health, "config_warnings": warnings,
                "targets": self.tm.get_summary(),
                "ready": all(health.values()) and len(warnings) == 0}

    async def run_phase(self, phase: PhaseConfig,
                        on_tick: Optional[Callable] = None) -> Dict[str, Any]:
        phase_mbps = phase.rate.to_mbps()
        ramp = RampController(phase_mbps, phase.ramp_up_seconds,
                              phase.ramp_down_seconds, phase.duration_seconds)
        pairs = self.nm.get_nodes_for_phase(phase)
        if not pairs:
            return {"phase_id": phase.phase_id, "status": "skipped", "reason": "no nodes"}
        n_nodes = len(pairs)
        per_node_mbps = phase_mbps / n_nodes
        for node, ctrl in pairs:
            node_rate = RateConfig(value_str=f"{per_node_mbps}Mbps")
            if isinstance(ctrl, TrexController):
                if phase.attack_mode in _ASTF_MODES:
                    ctrl.start_astf(node_rate, phase.targets)
                else:
                    ctrl.start_stl(node_rate, phase.targets)
            elif isinstance(ctrl, MHDDoSController):
                ctrl.start(phase.attack_mode, phase.targets, node_rate, phase.duration_seconds)
        metrics_history: List[MetricsSnapshot] = []
        enforcement_events: list = []
        start = time.monotonic()
        status = PhaseStatus.RAMPING
        while True:
            if self._aborted:
                status = PhaseStatus.ABORTED; break
            elapsed = time.monotonic() - start
            if elapsed >= phase.duration_seconds:
                status = PhaseStatus.DONE; break
            ramp_phase = ramp.get_phase(elapsed)
            if ramp_phase == "steady": status = PhaseStatus.ACTIVE
            elif ramp_phase == "ramp_down": status = PhaseStatus.COOLING
            current_target = ramp.get_rate_at(elapsed)
            per_node_target = current_target / n_nodes
            node_tx: Dict[str, float] = {}
            now_iso = datetime.now(timezone.utc).isoformat()
            for node, ctrl in pairs:
                stats = ctrl.get_stats()
                tx = stats.get("tx_mbps", 0)
                node_tx[node.node_id] = tx
                metrics_history.append(MetricsSnapshot(
                    timestamp=now_iso, node_id=node.node_id, phase_id=phase.phase_id,
                    tx_mbps=tx, rx_mbps=stats.get("rx_mbps", 0), pps=stats.get("pps", 0),
                    active_flows=stats.get("active_flows", 0), cpu_pct=stats.get("cpu_pct", 0)))
                evt = self.governor.check_node(node.node_id, phase.phase_id, tx, per_node_target)
                if evt:
                    enforcement_events.append(evt)
                    throttled = RateConfig(value_str=f"{per_node_target * 0.9}Mbps")
                    if isinstance(ctrl, TrexController): ctrl.update_rate(throttled)
                    elif isinstance(ctrl, MHDDoSController):
                        ctrl.restart_with_threads(throttled.to_mhddos_threads())
            agg_evt = self.governor.check_aggregate(phase.phase_id, node_tx)
            if agg_evt:
                enforcement_events.append(agg_evt)
                if agg_evt.action == "abort": self._aborted = True
            if on_tick:
                on_tick({"elapsed": round(elapsed, 1), "phase_id": phase.phase_id,
                         "status": status.value, "ramp_phase": ramp_phase,
                         "target_mbps": round(current_target, 2),
                         "actual_mbps": round(sum(node_tx.values()), 2),
                         "node_tx": {k: round(v, 2) for k, v in node_tx.items()}})
            await asyncio.sleep(1)
        for _, ctrl in pairs:
            ctrl.stop()
        return {"phase_id": phase.phase_id, "name": phase.name, "status": status.value,
                "duration_actual_s": round(time.monotonic() - start, 1),
                "metrics_count": len(metrics_history),
                "enforcement_events": len(enforcement_events),
                "peak_tx_mbps": round(max((s.tx_mbps for s in metrics_history), default=0), 2),
                "avg_tx_mbps": round(sum(s.tx_mbps for s in metrics_history) / max(1, len(metrics_history)), 2)}

    async def run_campaign(self, phase_selector: Optional[List[str]] = None,
                           on_tick: Optional[Callable] = None) -> Dict[str, Any]:
        self._aborted = False
        pre = self.pre_flight()

        # Start heartbeat to keep remote agents alive
        if not self.simulate:
            self.nm.start_heartbeat(interval=5)

        phases = self.campaign.phases
        if phase_selector:
            phases = [p for p in phases if p.phase_id in phase_selector]
        results = []
        for phase in phases:
            if self._aborted:
                results.append({"phase_id": phase.phase_id, "status": "aborted"}); continue
            res = await self.run_phase(phase, on_tick=on_tick)
            results.append(res)

        # Stop heartbeat
        if not self.simulate:
            self.nm.stop_heartbeat()

        return {"campaign_id": self.campaign.campaign_id, "name": self.campaign.name,
                "pre_flight": pre, "phase_results": results,
                "total_enforcement_events": len(self.governor.enforcement_log),
                "aborted": self._aborted}

    def abort(self) -> None:
        self._aborted = True
        for node in self.campaign.nodes:
            try:
                ctrl = self.nm.get_controller(node.node_id)
                ctrl.stop()
            except KeyError:
                pass
        if not self.simulate:
            self.nm.stop_heartbeat()
