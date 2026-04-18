"""
TargetManager – load, check and query targets.
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from typing import Dict, List
from config.models import AssetStatus, CheckResult, TargetAsset
from targets.checker import check_target
from targets.reader import extract_targets, parse_rtf_file


class TargetManager:
    def __init__(self) -> None:
        self.targets: List[TargetAsset] = []

    def load_from_rtf(self, path: str) -> int:
        text = parse_rtf_file(path)
        new = extract_targets(text)
        existing = {t.address for t in self.targets}
        added = [t for t in new if t.address not in existing]
        self.targets.extend(added)
        return len(added)

    def load_from_list(self, addresses: List[str]) -> int:
        text = "\n".join(addresses)
        new = extract_targets(text)
        existing = {t.address for t in self.targets}
        added = [t for t in new if t.address not in existing]
        self.targets.extend(added)
        return len(added)

    async def check_all(self, timeout: int = 5) -> List[CheckResult]:
        tasks = [check_target(t, timeout=timeout) for t in self.targets]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        now = datetime.now(timezone.utc).isoformat()
        out: List[CheckResult] = []
        for target, res in zip(self.targets, results):
            if isinstance(res, Exception):
                cr = CheckResult(address=target.address, alive=False,
                                 latency_ms=0, method="error", detail=str(res))
            else:
                cr = res
            target.status = AssetStatus.LIVE if cr.alive else AssetStatus.DEAD
            target.latency_ms = cr.latency_ms
            target.last_checked = now
            out.append(cr)
        return out

    def get_live(self) -> List[TargetAsset]:
        return [t for t in self.targets if t.status == AssetStatus.LIVE]

    def get_dead(self) -> List[TargetAsset]:
        return [t for t in self.targets if t.status == AssetStatus.DEAD]

    def get_summary(self) -> Dict[str, int]:
        return {
            "total": len(self.targets),
            "live": sum(1 for t in self.targets if t.status == AssetStatus.LIVE),
            "dead": sum(1 for t in self.targets if t.status == AssetStatus.DEAD),
            "unknown": sum(1 for t in self.targets if t.status == AssetStatus.UNKNOWN),
        }
