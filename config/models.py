"""
Core data models, enums, and configuration dataclasses.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Tuple


class AttackMode(Enum):
    TCP_FLOOD = "tcp_flood"
    SYN_FLOOD = "syn_flood"
    UDP_FLOOD = "udp_flood"
    HTTP_FLOOD = "http_flood"
    SLOWLORIS = "slowloris"
    DNS_AMP = "dns_amp"


class NodeRole(Enum):
    TREX = "trex"
    MHDDOS = "mhddos"


class NodeStatus(Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"
    ERROR = "error"


class PhaseStatus(Enum):
    PENDING = "pending"
    RAMPING = "ramping"
    ACTIVE = "active"
    COOLING = "cooling"
    DONE = "done"
    ABORTED = "aborted"


class AssetStatus(Enum):
    LIVE = "live"
    DEAD = "dead"
    UNKNOWN = "unknown"


@dataclass
class RateConfig:
    value_str: str
    _RATE_RE = re.compile(r"^\s*([\d.]+)\s*(gbps|mbps|kbps|bps)\s*$", re.IGNORECASE)

    def parse(self) -> Tuple[float, str]:
        m = self._RATE_RE.match(self.value_str)
        if not m:
            raise ValueError(f"Cannot parse rate string: {self.value_str!r}")
        return float(m.group(1)), m.group(2).lower()

    def to_mbps(self) -> float:
        val, unit = self.parse()
        multipliers = {"bps": 1e-6, "kbps": 1e-3, "mbps": 1.0, "gbps": 1e3}
        return val * multipliers[unit]

    def to_trex_multiplier(self, link_speed_gbps: float = 10.0) -> float:
        return self.to_mbps() / (link_speed_gbps * 1_000)

    def to_mhddos_threads(self) -> int:
        return max(1, min(2048, int(self.to_mbps() / 0.5)))


@dataclass
class TargetAsset:
    address: str
    asset_type: str = "ipv4"
    port: int = 80
    status: AssetStatus = AssetStatus.UNKNOWN
    latency_ms: float = 0.0
    last_checked: str = ""


@dataclass
class CheckResult:
    address: str
    alive: bool
    latency_ms: float
    method: str
    detail: str = ""


@dataclass
class NodeConfig:
    node_id: str
    host: str
    port: int
    role: NodeRole
    max_rate_mbps: float
    status: NodeStatus = NodeStatus.ONLINE
    auth_token: str = ""


@dataclass
class HeartbeatConfig:
    timeout_seconds: int = 30
    check_interval_seconds: int = 5


@dataclass
class PhaseConfig:
    phase_id: str
    name: str
    attack_mode: AttackMode
    targets: List[str]
    duration_seconds: int
    rate: RateConfig
    ramp_up_seconds: int = 30
    ramp_down_seconds: int = 10
    node_ids: List[str] = field(default_factory=list)


@dataclass
class CampaignConfig:
    campaign_id: str
    name: str
    description: str
    phases: List[PhaseConfig]
    nodes: List[NodeConfig]
    global_max_rate_mbps: float = 2000.0
    abort_threshold_mbps: float = 2500.0


@dataclass
class MetricsSnapshot:
    timestamp: str
    node_id: str
    phase_id: str
    tx_mbps: float
    rx_mbps: float
    pps: int
    active_flows: int
    cpu_pct: float


@dataclass
class EnforcementEvent:
    timestamp: str
    node_id: str
    phase_id: str
    measured_mbps: float
    limit_mbps: float
    action: str


@dataclass
class AuditEntry:
    timestamp: str
    actor: str
    action: str
    detail: str
    phase_id: str = ""
