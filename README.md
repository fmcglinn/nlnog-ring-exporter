# NLNOG Ring Prometheus Exporter

A Prometheus exporter that pings targets from [NLNOG Ring](https://ring.nlnog.net/) nodes worldwide. It maintains persistent SSH sessions to ring nodes and executes pings on demand, returning latency metrics per node.

## How it works

1. On startup, the exporter fetches the active node list from the NLNOG Ring API and opens persistent SSH master connections to each node.
2. A background thread refreshes the node list and health-checks SSH sessions every 5 minutes (configurable).
3. When Prometheus (or a user) hits `/probe?target=1.1.1.1`, the exporter SSHes into healthy nodes in parallel and runs `ping`, returning RTT metrics in Prometheus exposition format.

## Quick start

```bash
cp .env.example .env
# Edit .env — at minimum set SSH_KEY_PATH to your NLNOG Ring SSH key
```

### Docker (recommended)

```bash
# Place your SSH private key at ./nlring/nlnog (or adjust the volume mount)
docker compose up -d
```

### Local

```bash
pip install -r requirements.txt
gunicorn -c gunicorn.conf.py wsgi:app
```

The exporter listens on port `8000` by default.

## Endpoints

| Endpoint | Description |
|---|---|
| `/` | Web UI with interactive probe builder |
| `/probe?target=<host>` | Ping target from ring nodes (Prometheus metrics) |
| `/probe?target=<host>&format=json` | Same, but returns JSON for the web UI |
| `/health` | Health check (JSON) |
| `/sessions` | SSH session status summary (JSON) |
| `/debug` | Node list grouped by session status (plain text) |
| `/api/filter-options` | Available filter values for the probe builder (JSON) |

## Probe parameters

| Parameter | Description |
|---|---|
| `target` | **(required)** IP address or hostname to ping |
| `limit` | Maximum number of nodes to probe (randomly sampled) |
| `node` | Filter by node short hostname (comma-separated) |
| `asn` | Filter by ASN (comma-separated) |
| `city` | Filter by city (comma-separated) |
| `countrycode` | Filter by country code (comma-separated) |
| `continent` | Filter by continent (comma-separated) |
| `company` | Filter by hosting company (comma-separated) |
| `format` | Set to `json` for JSON output; omit for Prometheus format |

When `limit` is used with multi-value filters, nodes are sampled with balanced representation across the filter groups.

## Prometheus scrape config

```yaml
scrape_configs:
  - job_name: nlnog-ring
    scrape_interval: 5m
    scrape_timeout: 2m
    metrics_path: /probe
    params:
      target: [1.1.1.1]
    static_configs:
      - targets: ['localhost:8000']
```

### Metrics

| Metric | Description |
|---|---|
| `nlnog_ping_success` | `1` if ping succeeded, `0` otherwise |
| `nlnog_ping_rtt_min_ms` | Minimum RTT in milliseconds |
| `nlnog_ping_rtt_avg_ms` | Average RTT in milliseconds |
| `nlnog_ping_rtt_max_ms` | Maximum RTT in milliseconds |
| `nlnog_ping_rtt_mdev_ms` | RTT standard deviation in milliseconds |

All metrics carry labels: `node`, `target`, `asn`, `city`, `countrycode`, `continent`, `company`, `status`.

## Web UI

The probe builder at `/` provides:

- **Target and filter selection** with multi-select dropdowns populated from live node data
- **Human readable mode** (default): results displayed as a colour-coded HTML table with a summary line
- **Prometheus mode**: opens the raw `/probe?...` URL in a new tab, giving you a copyable scrape URL

## Configuration

All settings are configured via environment variables (or a `.env` file). See [`.env.example`](.env.example) for the full list.

| Variable | Default | Description |
|---|---|---|
| `SSH_USERNAME` | `rise` | SSH user for ring nodes |
| `SSH_KEY_PATH` | `/app/ssh/nlnog` | Path to SSH private key |
| `SSH_CONNECT_TIMEOUT` | `5` | SSH connection timeout (seconds) |
| `SSH_SUBPROCESS_TIMEOUT` | `15` | SSH command execution timeout (seconds) |
| `SSH_CONTROL_PATH_TEMPLATE` | `/tmp/ssh-control/nlnog-%r@%h:%p` | SSH multiplexing socket path |
| `PING_COUNT` | `10` | Number of ping packets per probe |
| `PING_TIMEOUT` | `5` | Ping timeout (seconds) |
| `STARTUP_MAX_WORKERS` | `50` | Parallel SSH sessions during startup |
| `THREADS` | `100` | Parallel workers for probes and session checks |
| `CACHE_REFRESH_INTERVAL` | `300` | Node list refresh interval (seconds) |
| `FLASK_PORT` | `8000` | HTTP listen port |
| `LOG_LEVEL` | `INFO` | Log level |
| `DEBUG` | `false` | Enable debug logging and tracebacks |

## Architecture

```
wsgi.py                     Gunicorn entrypoint
app/
  __init__.py               Flask app factory, startup banner, background thread
  routes.py                 HTTP endpoints
  templates/index.html      Probe builder UI
core/
  config.py                 Environment-based configuration
  session_manager.py        SSH master session lifecycle (start/stop/check/cleanup)
  node_manager.py           Node cache, API sync, health checks, filtering
  node_cache_store.py       Atomic JSON persistence of node list
  ping.py                   SSH ping execution and RTT parsing
  geo.py                    Country code to continent mapping
```

The application runs as a single Gunicorn worker (required for in-process shared state) with multiple threads. SSH sessions are managed via OpenSSH `ControlMaster` multiplexing — one persistent master connection per node, reused by all probe requests.

## License

MIT
