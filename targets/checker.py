"""
Async target liveness checker.
"""
from __future__ import annotations
import asyncio, re, time
from config.models import CheckResult, TargetAsset


async def check_icmp(address: str, timeout: int = 3) -> CheckResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", str(timeout), address,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout + 2)
        out = stdout.decode(errors="replace")
        alive = proc.returncode == 0
        latency = 0.0
        m = re.search(r"time[=<]([\d.]+)", out)
        if m:
            latency = float(m.group(1))
        return CheckResult(address=address, alive=alive, latency_ms=latency,
                           method="icmp", detail=out.strip()[:200])
    except Exception as exc:
        return CheckResult(address=address, alive=False, latency_ms=0,
                           method="icmp", detail=str(exc))


async def check_tcp(address: str, port: int = 80, timeout: int = 3) -> CheckResult:
    t0 = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(address, port), timeout=timeout)
        latency = (time.monotonic() - t0) * 1000
        writer.close()
        await writer.wait_closed()
        return CheckResult(address=address, alive=True,
                           latency_ms=round(latency, 2), method="tcp",
                           detail=f"port {port} open")
    except Exception as exc:
        latency = (time.monotonic() - t0) * 1000
        return CheckResult(address=address, alive=False,
                           latency_ms=round(latency, 2), method="tcp",
                           detail=str(exc))


async def check_http(url: str, timeout: int = 5) -> CheckResult:
    import urllib.request
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, method="HEAD")
        loop = asyncio.get_event_loop()
        resp = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=timeout)),
            timeout=timeout + 2)
        latency = (time.monotonic() - t0) * 1000
        return CheckResult(address=url, alive=True, latency_ms=round(latency, 2),
                           method="http", detail=f"HTTP {resp.status}")
    except Exception as exc:
        latency = (time.monotonic() - t0) * 1000
        return CheckResult(address=url, alive=False, latency_ms=round(latency, 2),
                           method="http", detail=str(exc))


async def check_target(asset: TargetAsset, timeout: int = 5) -> CheckResult:
    if asset.asset_type == "url":
        return await check_http(asset.address, timeout=timeout)
    elif asset.asset_type in ("ipv4", "ipv6"):
        addr = asset.address.split(":")[0] if ":" in asset.address and asset.asset_type == "ipv4" else asset.address
        result = await check_tcp(addr, asset.port, timeout=timeout)
        if not result.alive:
            result = await check_icmp(addr, timeout=timeout)
        return result
    elif asset.asset_type == "hostname":
        addr = asset.address.split(":")[0]
        return await check_tcp(addr, asset.port, timeout=timeout)
    elif asset.asset_type == "cidr":
        addr = asset.address.split("/")[0]
        return await check_icmp(addr, timeout=timeout)
    else:
        return CheckResult(address=asset.address, alive=False, latency_ms=0,
                           method="none", detail="unsupported asset type")
