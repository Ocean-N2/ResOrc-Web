# Remote Agent Daemon — Deployment Guide

## Overview
The remote agent daemon runs on each traffic-generation node.
It accepts commands from the central orchestrator, manages the local
traffic generator (TRex or MHDDoS), and reports telemetry.

## Prerequisites

### TRex Node
- DPDK-compatible NIC (Intel X710, Mellanox ConnectX-5, etc.)
- TRex installed and running as daemon
- Python 3.8+

### MHDDoS Node
- Any Linux VM/VPS
- MHDDoS installed (authorized internal copy)
- Python 3.8+

## Installation
```bash
pip install -r requirements.txt
```

## Usage

### TRex Agent
```bash
python agent.py \
    --node-id trex-01 \
    --role trex \
    --port 4501 \
    --auth-token trex01-secret-token \
    --max-rate-mbps 1000 \
    --heartbeat-timeout 30 \
    --trex-daemon-port 4501
```

### MHDDoS Agent
```bash
python agent.py \
    --node-id mhddos-01 \
    --role mhddos \
    --port 5501 \
    --auth-token mhddos01-secret-token \
    --max-rate-mbps 800 \
    --heartbeat-timeout 30 \
    --mhddos-path /opt/mhddos
```

## API Endpoints

| Method | Endpoint  | Description                            |
|--------|-----------|----------------------------------------|
| GET    | /health   | Node health + system metrics           |
| POST   | /start    | Start traffic generation               |
| POST   | /stop     | Graceful stop                          |
| POST   | /kill     | Force kill (emergency)                 |
| PUT    | /rate     | Update traffic rate on the fly         |
| GET    | /stats    | Current TX/RX, PPS, CPU metrics        |
| GET    | /status   | Running state, config, heartbeat info  |

## Authentication
Every request must include `X-Auth-Token` header matching the `--auth-token`.

## Dead-Man's Switch
If no request arrives from the orchestrator within `--heartbeat-timeout` seconds,
the agent automatically stops all traffic and enters safe idle state.

## Simulated Mode
If the actual TRex/MHDDoS binary is not found, the agent operates in
simulated mode, generating synthetic metrics. This allows testing the
agent independently without real traffic generators.
