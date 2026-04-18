"""
NodeManager – fleet registry, health checks, and heartbeat thread.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional, Tuple, Union

from config.models import NodeConfig, NodeRole, NodeStatus, PhaseConfig
from agents.trex_controller import TrexController
from agents.mhddos_controller import MHDDoSController

Controller = Union[TrexController, MHDDoSController]
log = logging.getLogger("node_manager")


class NodeManager:

    def __init__(self, simulate: bool = True) -> None:
        self.simulate = simulate
        self._nodes: Dict[str, NodeConfig] = {}
        self._controllers: Dict[str, Controller] = {}
        self._health_cache: Dict[str, Dict] = {}
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_running = False

    # ── Registration ───────────────────────────────────────────────

    def register(self, node: NodeConfig) -> None:
        self._nodes[node.node_id] = node
        if node.role == NodeRole.TREX:
            self._controllers[node.node_id] = TrexController(node, self.simulate)
        else:
            self._controllers[node.node_id] = MHDDoSController(node, self.simulate)

    def register_all(self, nodes: List[NodeConfig]) -> None:
        for n in nodes:
            self.register(n)

    # ── Health Checks ──────────────────────────────────────────────

    def check_health_all(self) -> Dict[str, bool]:
        results: Dict[str, bool] = {}
        for nid, ctrl in self._controllers.items():
            ok = ctrl.connect()
            self._nodes[nid].status = NodeStatus.ONLINE if ok else NodeStatus.OFFLINE
            results[nid] = ok

            # If real mode, also cache health data
            if not self.simulate:
                health = ctrl.get_health()
                if health:
                    self._health_cache[nid] = health
        return results

    def get_node_health(self, node_id: str) -> Optional[Dict]:
        """Get cached health data for a single node."""
        if node_id in self._controllers and not self.simulate:
            health = self._controllers[node_id].get_health()
            if health:
                self._health_cache[node_id] = health
            return health
        return self._health_cache.get(node_id)

    # ── Heartbeat Thread ───────────────────────────────────────────

    def start_heartbeat(self, interval: int = 5) -> None:
        """Start background health-check thread (keeps agents' dead-man switches alive)."""
        if self._heartbeat_thread and self._heartbeat_running:
            return  # Already running

        self._heartbeat_running = True

        def _loop():
            while self._heartbeat_running:
                for nid, ctrl in self._controllers.items():
                    try:
                        health = ctrl.get_health()
                        if health and health.get("ok"):
                            self._nodes[nid].status = NodeStatus.ONLINE
                            self._health_cache[nid] = health
                            if health.get("running"):
                                self._nodes[nid].status = NodeStatus.BUSY
                        else:
                            self._nodes[nid].status = NodeStatus.OFFLINE
                    except Exception as exc:
                        self._nodes[nid].status = NodeStatus.ERROR
                        log.warning(f"Heartbeat failed for {nid}: {exc}")
                time.sleep(interval)

        self._heartbeat_thread = threading.Thread(target=_loop, daemon=True)
        self._heartbeat_thread.start()
        log.info(f"Heartbeat thread started (interval={interval}s)")

    def stop_heartbeat(self) -> None:
        """Stop the background heartbeat thread."""
        self._heartbeat_running = False
        self._heartbeat_thread = None
        log.info("Heartbeat thread stopped")

    # ── Queries ────────────────────────────────────────────────────

    def get_controller(self, node_id: str) -> Controller:
        return self._controllers[node_id]

    def get_node(self, node_id: str) -> NodeConfig:
        return self._nodes[node_id]

    def get_nodes_for_phase(self, phase: PhaseConfig) -> List[Tuple[NodeConfig, Controller]]:
        pairs = []
        for nid in phase.node_ids:
            if nid in self._nodes:
                pairs.append((self._nodes[nid], self._controllers[nid]))
        return pairs

    def get_all_stats(self) -> Dict[str, Dict]:
        return {nid: ctrl.get_stats() for nid, ctrl in self._controllers.items()}

    def get_all_health(self) -> Dict[str, Dict]:
        return dict(self._health_cache)

    @property
    def all_nodes(self) -> List[NodeConfig]:
        return list(self._nodes.values())
