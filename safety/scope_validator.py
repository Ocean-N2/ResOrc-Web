"""
Scope validator – CIDR whitelist.
"""
from __future__ import annotations
import ipaddress, re, socket
from typing import List, Optional, Tuple
from urllib.parse import urlparse
from config.models import TargetAsset


class ScopeValidator:
    def __init__(self, allowed_cidrs: Optional[List[str]] = None) -> None:
        self._networks: list = []
        for cidr in (allowed_cidrs or []):
            self.add_cidr(cidr)

    def add_cidr(self, cidr_str: str) -> None:
        self._networks.append(ipaddress.ip_network(cidr_str, strict=False))

    @property
    def permissive(self) -> bool:
        return len(self._networks) == 0

    @staticmethod
    def _extract_ip(address: str) -> Optional[str]:
        if address.startswith(("http://", "https://")):
            host = urlparse(address).hostname
        else:
            host = re.split(r"[:/]", address)[0]
        if not host: return None
        try:
            ipaddress.ip_address(host); return host
        except ValueError: pass
        try:
            info = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            if info: return info[0][4][0]
        except (socket.gaierror, OSError): pass
        return None

    def is_in_scope(self, address: str) -> bool:
        if self.permissive: return True
        if "/" in address and not address.startswith("http"):
            try:
                target_net = ipaddress.ip_network(address, strict=False)
                return any(net.supernet_of(target_net) if net.version == target_net.version else False
                           for net in self._networks)
            except ValueError: pass
        ip_str = self._extract_ip(address)
        if not ip_str: return False
        try: ip = ipaddress.ip_address(ip_str)
        except ValueError: return False
        return any(ip in net for net in self._networks)

    def validate_targets(self, targets: List[TargetAsset]) -> Tuple[List[TargetAsset], List[TargetAsset]]:
        in_s, out_s = [], []
        for t in targets:
            (in_s if self.is_in_scope(t.address) else out_s).append(t)
        return in_s, out_s
