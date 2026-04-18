"""
Rate governor – ramp scheduling and real-time enforcement.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Dict, List, Optional
from config.models import EnforcementEvent


class RampController:
    def __init__(self, target_mbps: float, ramp_up_s: int, ramp_down_s: int, duration_s: int) -> None:
        self.target_mbps = target_mbps
        self.ramp_up_s = ramp_up_s
        self.ramp_down_s = ramp_down_s
        self.duration_s = duration_s

    def get_rate_at(self, elapsed_s: float) -> float:
        if elapsed_s < 0 or elapsed_s >= self.duration_s:
            return 0.0
        if elapsed_s < self.ramp_up_s:
            return self.target_mbps * (elapsed_s / self.ramp_up_s)
        cool_start = self.duration_s - self.ramp_down_s
        if elapsed_s >= cool_start:
            remaining = self.duration_s - elapsed_s
            return self.target_mbps * (remaining / self.ramp_down_s)
        return self.target_mbps

    def get_phase(self, elapsed_s: float) -> str:
        if elapsed_s < 0: return "pending"
        if elapsed_s >= self.duration_s: return "done"
        if elapsed_s < self.ramp_up_s: return "ramp_up"
        if elapsed_s >= self.duration_s - self.ramp_down_s: return "ramp_down"
        return "steady"


class RateGovernor:
    def __init__(self, global_max_mbps: float, abort_threshold_mbps: float) -> None:
        self.global_max_mbps = global_max_mbps
        self.abort_threshold_mbps = abort_threshold_mbps
        self.enforcement_log: List[EnforcementEvent] = []

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def check_node(self, node_id: str, phase_id: str, measured_mbps: float,
                   limit_mbps: float) -> Optional[EnforcementEvent]:
        if measured_mbps > limit_mbps * 1.10:
            evt = EnforcementEvent(timestamp=self._now(), node_id=node_id,
                phase_id=phase_id, measured_mbps=round(measured_mbps, 2),
                limit_mbps=round(limit_mbps, 2), action="throttle")
            self.enforcement_log.append(evt)
            return evt
        return None

    def check_aggregate(self, phase_id: str, measurements: Dict[str, float]) -> Optional[EnforcementEvent]:
        total = sum(measurements.values())
        if total > self.abort_threshold_mbps:
            evt = EnforcementEvent(timestamp=self._now(), node_id="AGGREGATE",
                phase_id=phase_id, measured_mbps=round(total, 2),
                limit_mbps=round(self.abort_threshold_mbps, 2), action="abort")
            self.enforcement_log.append(evt)
            return evt
        if total > self.global_max_mbps:
            evt = EnforcementEvent(timestamp=self._now(), node_id="AGGREGATE",
                phase_id=phase_id, measured_mbps=round(total, 2),
                limit_mbps=round(self.global_max_mbps, 2), action="global_throttle")
            self.enforcement_log.append(evt)
            return evt
        return None
