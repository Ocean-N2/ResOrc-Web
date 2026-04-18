"""
RTF target-list reader.
"""
from __future__ import annotations
import re
from typing import List
from config.models import AssetStatus, TargetAsset


def parse_rtf_file(path: str) -> str:
    with open(path, "r", errors="replace") as fh:
        raw = fh.read()
    try:
        from striprtf.striprtf import rtf_to_text
        return rtf_to_text(raw)
    except ImportError:
        pass
    text = re.sub(r"\{\\[^}]*\}", "", raw)
    text = re.sub(r"\\[a-z]+\d*\s?", " ", text)
    text = text.replace("{", "").replace("}", "")
    return text


_CIDR_RE  = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3}/\d{1,2})\b")
_URL_RE   = re.compile(r"(https?://[^\s}\\]+)")
_IPV6_RE  = re.compile(r"\b([0-9a-fA-F:]{3,39}(?:::[0-9a-fA-F]{1,4})*)\b")
_IPV4P_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?)\b")
_HOST_RE  = re.compile(
    r"\b([a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?)*"
    r"\.[a-zA-Z]{2,})"
    r"(?::(\d{1,5}))?\b"
)


def _is_valid_ipv4(s: str) -> bool:
    parts = s.split(".")
    if len(parts) != 4:
        return False
    return all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def _classify_ipv6(s: str) -> bool:
    return "::" in s or s.count(":") >= 2


def extract_targets(text: str) -> List[TargetAsset]:
    seen: set = set()
    assets: List[TargetAsset] = []

    def _add(address: str, atype: str, port: int = 80) -> None:
        key = address.lower()
        if key not in seen:
            seen.add(key)
            assets.append(TargetAsset(address=address, asset_type=atype,
                                      port=port, status=AssetStatus.UNKNOWN))

    for m in _CIDR_RE.finditer(text):
        _add(m.group(1), "cidr")
    for m in _URL_RE.finditer(text):
        url = m.group(1).rstrip("/")
        port = 443 if url.startswith("https") else 80
        port_m = re.search(r":(\d{1,5})(?=/|$)", url.split("://", 1)[-1])
        if port_m:
            port = int(port_m.group(1))
        _add(url, "url", port)
    for m in _IPV6_RE.finditer(text):
        candidate = m.group(1)
        if _classify_ipv6(candidate) and candidate.lower() not in seen:
            _add(candidate, "ipv6")
    for m in _IPV4P_RE.finditer(text):
        raw = m.group(1)
        if any(raw.split(":")[0] in a.address for a in assets if a.asset_type == "cidr"):
            continue
        if ":" in raw:
            ip, port_s = raw.rsplit(":", 1)
            port = int(port_s) if port_s.isdigit() else 80
        else:
            ip, port = raw, 80
        if _is_valid_ipv4(ip):
            _add(raw, "ipv4", port)
    for m in _HOST_RE.finditer(text):
        hostname = m.group(1)
        port = int(m.group(2)) if m.group(2) else 80
        full = f"{hostname}:{port}" if port != 80 else hostname
        if not any(hostname in a.address for a in assets):
            _add(full, "hostname", port)
    return assets
