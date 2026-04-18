"""
TRex controller – dual mode: simulated OR real HTTP to remote agent.
"""
from __future__ import annotations

import json
import random
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

from config.models import NodeConfig, RateConfig


class TrexController:

    def __init__(self, node: NodeConfig, simulate: bool = True) -> None:
        self.node = node
        self.simulate = simulate
        self._running = False
        self._current_mult: float = 0.0
        self._current_mode: str = "stl"
        self._targets: List[str] = []
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
        """Make an HTTP request to the remote agent. Returns parsed JSON or None."""
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

    def start_stl(self, rate: RateConfig, targets: List[str]) -> bool:
        self._targets = targets
        self._current_mode = "stl"
        if self.simulate:
            self._current_mult = rate.to_trex_multiplier()
            self._running = True
            self._start_ts = time.monotonic()
            return True
        resp = self._request("POST", "/start", {
            "attack_mode": "tcp_flood",
            "targets": targets,
            "rate_mbps": rate.to_mbps(),
            "duration_seconds": 3600,
            "ramp_up_seconds": 0,
        })
        if resp and resp.get("ok"):
            self._running = True
            self._current_mult = rate.to_trex_multiplier()
            self._start_ts = time.monotonic()
            return True
        return False

    def start_astf(self, rate: RateConfig, targets: List[str]) -> bool:
        self._targets = targets
        self._current_mode = "astf"
        if self.simulate:
            self._current_mult = rate.to_trex_multiplier()
            self._running = True
            self._start_ts = time.monotonic()
            return True
        resp = self._request("POST", "/start", {
            "attack_mode": "http_flood",
            "targets": targets,
            "rate_mbps": rate.to_mbps(),
            "duration_seconds": 3600,
            "ramp_up_seconds": 0,
        })
        if resp and resp.get("ok"):
            self._running = True
            self._current_mult = rate.to_trex_multiplier()
            self._start_ts = time.monotonic()
            return True
        return False

    def update_rate(self, new_rate: RateConfig) -> bool:
        if not self._running:
            return False
        if self.simulate:
            self._current_mult = new_rate.to_trex_multiplier()
            return True
        resp = self._request("PUT", "/rate", {"rate_mbps": new_rate.to_mbps()})
        if resp and resp.get("ok"):
            self._current_mult = new_rate.to_trex_multiplier()
            return True
        return False

    def stop(self) -> bool:
        if self.simulate:
            self._running = False
            self._current_mult = 0.0
            return True
        resp = self._request("POST", "/stop")
        self._running = False
        self._current_mult = 0.0
        return resp is not None and resp.get("ok", False)

    def kill(self) -> bool:
        if self.simulate:
            self._running = False
            self._current_mult = 0.0
            return True
        resp = self._request("POST", "/kill")
        self._running = False
        self._current_mult = 0.0
        return resp is not None and resp.get("ok", False)

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
        tx = self._current_mult * 10 * 1000
        jitter = random.uniform(-0.02, 0.02) * tx
        tx = max(0, tx + jitter)
        return {
            "tx_mbps": round(tx, 2),
            "rx_mbps": round(tx * random.uniform(0.001, 0.01), 2),
            "pps": int(tx * 1000 / 8 * random.uniform(0.9, 1.1)),
            "cpu_pct": round(min(100, tx / self.node.max_rate_mbps * 80 + random.uniform(0, 5)), 1),
            "active_flows": int(tx * random.uniform(50, 200)),
        }

    def get_health(self) -> Optional[Dict]:
        return self._request("GET", "/health", timeout=3)

    @property
    def running(self) -> bool:
        return self._running
