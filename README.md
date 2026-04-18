# Resilience Orchestrator v2.2 — Web Edition with Remote Agents

> Enterprise-grade web-based DDoS simulation & resilience-testing orchestrator
> with real remote agent control and dead-man's switch safety.

## Architecture

```
Orchestrator (Flask)  ←→  Remote Agent (TRex)   [Dead-Man Switch]
                      ←→  Remote Agent (MHDDoS)  [Dead-Man Switch]
```

Controllers use HTTP to talk to agents: /start, /stop, /kill, /rate, /stats, /health

## Quick Start

### Orchestrator
```bash
pip install -r requirements.txt
python app.py
# Open http://localhost:5000
```

### Remote Agent
```bash
cd remote_agent/
pip install -r requirements.txt
python agent.py --node-id trex-01 --role trex --port 4501 --auth-token SECRET --max-rate-mbps 1000
```

## Dead-Man's Switch
If no request arrives within --heartbeat-timeout seconds (default: 30), agent auto-stops all traffic.

## Kill Switch (3 Layers)
1. Automatic: Aggregate rate > abort threshold
2. Manual: Operator clicks Kill Switch in UI
3. Dead-Man: No heartbeat → agent self-stops
