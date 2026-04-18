"""
MHDDoS controller – dual mode: simulated OR real HTTP to remote agent.
"""
from __future__ import annotations

import json
import random
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

from config.models import AttackMode, NodeConfig, RateConfig

METHODS: Dict[AttackMode, str] = {
    AttackMode.TCP_FLOOD: "TCP", AttackMode.SYN_FLOOD: "SYN",
    AttackMode.UDP_FLOOD: "UDP", AttackMode.HTTP_FLOOD: "GET",
    AttackMode.SLOWLORIS: "SLOW", AttackMode.DNS_AMP: "DNS",
}


class MHDDoSController:

    def __init__(self, node: NodeConfig, simulate: bool = True) -> None:
        self.node = node
        self.simulate = simulate
        self._running = False
        self._threads: int = 0
        self._method: str = ""
        self._target: str = ""
        self._duration: int = 0
        self._start_ts: float = 0.0

    # ── HTTP helpers ───────────────────────────────────────────────

    def _url(self, path: str) -> str:
        return f"http://{self.node.host}:{self.node.port}{path}"

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.node.auth_token:
            h["X-Auth-Token"] = self.node.auth_token
        return h

    def _request(self, method: str, path: str,
                 data: Optional[Dict] = None, timeout: int = 5) -> Optional[Dict]:
        url = self._url(path)
        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(url, data=body, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception:
            return None

    # ── Lifecycle ──────────────────────────────────────────────────

    def connect(self) -> bool:
        if self.simulate:
            return True
        resp = self._request("GET", "/health")
        return resp is not None and resp.get("ok", False)

    @staticmethod
    def build_command(method: AttackMode, target: str, threads: int, duration: int) -> List[str]:
        return ["python3", "start.py", METHODS.get(method, "TCP"), target, str(threads), str(duration)]

    def start(self, method: AttackMode, targets: List[str],
              rate: RateConfig, duration: int) -> bool:
        self._threads = rate.to_mhddos_threads()
        self._method = METHODS.get(method, "TCP")
        self._target = targets[0] if targets else ""
        self._duration = duration

        if self.simulate:
            self._running = True
            self._start_ts = time.monotonic()
            return True

        resp = self._request("POST", "/start", {
            "attack_mode": method.value,
            "targets": targets,
            "rate_mbps": rate.to_mbps(),
            "duration_seconds": duration,
            "ramp_up_seconds": 0,
        })
        if resp and resp.get("ok"):
            self._running = True
            self._start_ts = time.monotonic()
            return True
        return False

    def stop(self) -> bool:
        if self.simulate:
            self._running = False
            self._threads = 0
            return True
        resp = self._request("POST", "/stop")
        self._running = False
        self._threads = 0
        return resp is not None and resp.get("ok", False)

    def kill(self) -> bool:
        if self.simulate:
            self._running = False
            self._threads = 0
            return True
        resp = self._request("POST", "/kill")
        self._running = False
        self._threads = 0
        return resp is not None and resp.get("ok", False)

    def restart_with_threads(self, threads: int) -> bool:
        rate_mbps = threads * 0.5
        if self.simulate:
            self._threads = threads
            return True
        resp = self._request("PUT", "/rate", {"rate_mbps": rate_mbps})
        if resp and resp.get("ok"):
            self._threads = threads
            return True
        return False

    # ── Telemetry ──────────────────────────────────────────────────

    def get_stats(self) -> Dict:
        if not self.simulate and self._running:
            resp = self._request("GET", "/stats", timeout=3)
            if resp and resp.get("ok"):
                return {
                    "tx_mbps": resp.get("tx_mbps", 0),
                    "rx_mbps": resp.get("rx_mbps", 0),
                    "pps": resp.get("pps", 0),
                    "cpu_pct": resp.get("cpu_pct", 0),
                    "active_flows": resp.get("active_flows", 0),
                }
        # Simulated
        if not self._running:
            return {"tx_mbps": 0, "rx_mbps": 0, "pps": 0, "cpu_pct": 0, "active_flows": 0}
        tx = self._threads * 0.5 * random.uniform(0.85, 1.15)
        return {
            "tx_mbps": round(tx, 2),
            "rx_mbps": round(tx * random.uniform(0.001, 0.01), 2),
            "pps": int(tx * 1000 / 8 * random.uniform(0.8, 1.2)),
            "cpu_pct": round(min(100, self._threads / 20 + random.uniform(0, 10)), 1),
            "active_flows": self._threads * random.randint(2, 6),
        }

    def get_health(self) -> Optional[Dict]:
        return self._request("GET", "/health", timeout=3)

    @property
    def running(self) -> bool:
        return self._running
