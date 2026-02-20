import logging
import traceback
import concurrent.futures

from flask import Blueprint, request, Response, jsonify, current_app, render_template
from prometheus_client import Gauge, generate_latest, CollectorRegistry

from core.config import THREADS, DEBUG
from core.ping import is_valid_target, ping_from_node
from core.node_manager import NODE_FILTER_FIELDS, _node_field_value
from core.geo import get_country_name

bp = Blueprint("routes", __name__)


def _get_manager():
    return current_app.config["node_manager"]


@bp.route('/')
def index():
    return render_template('index.html', filter_fields=list(NODE_FILTER_FIELDS))


@bp.route('/probe')
def probe():
    manager = _get_manager()

    target = request.args.get('target')
    if not target:
        return "Missing target parameter", 400

    target = target.split('?')[0].strip()

    if not is_valid_target(target):
        return "Invalid target IP or hostname", 400

    limit_param = request.args.get('limit')
    try:
        limit = int(limit_param) if limit_param is not None else None
    except ValueError:
        return "Invalid limit parameter", 400

    # Build node filters from query parameters
    filters = {}
    for field in NODE_FILTER_FIELDS:
        value = request.args.get(field)
        if value:
            filters[field] = {v.strip().lower() for v in value.split(',')}

    registry = CollectorRegistry()
    labels = ['node', 'target', 'asn', 'city', 'countrycode', 'status', 'continent', 'company']

    rtt_min = Gauge('nlnog_ping_rtt_min_ms', 'Min RTT in ms', labels, registry=registry)
    rtt_avg = Gauge('nlnog_ping_rtt_avg_ms', 'Avg RTT in ms', labels, registry=registry)
    rtt_max = Gauge('nlnog_ping_rtt_max_ms', 'Max RTT in ms', labels, registry=registry)
    rtt_mdev = Gauge('nlnog_ping_rtt_mdev_ms', 'Mdev RTT in ms', labels, registry=registry)
    success = Gauge('nlnog_ping_success', 'Ping success (1) or failure (0)', labels, registry=registry)

    output_format = request.args.get('format')

    nodes = manager.fetch_healthy_nodes(limit=limit, filters=filters)

    if not nodes:
        if output_format == 'json':
            return jsonify({"error": "No nodes with healthy SSH sessions available."}), 503
        return Response(
            "No nodes with healthy SSH sessions available. "
            "The exporter may still be establishing connections.\n",
            status=503, mimetype="text/plain",
        )

    json_results = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=THREADS) as executor:
        futures = {
            executor.submit(ping_from_node, node["hostname"], target): node
            for node in nodes
        }
        for future in concurrent.futures.as_completed(futures):
            node_info = futures[future]
            node = node_info["hostname"]
            asn = node_info["asn"]
            city = node_info["city"]
            country = node_info["countrycode"]
            continent = node_info["continent"]
            company = node_info.get("company", "Unknown")
            node_short = node.split('.')[0]

            try:
                _, status, stats = future.result()
            except Exception as e:
                logging.error(f"Error in future for node {node}: {e}")
                if DEBUG:
                    traceback.print_exc()
                status = "future_error"
                stats = None

            label_kwargs = {
                "node": node_short,
                "target": target,
                "asn": asn,
                "city": city,
                "countrycode": country,
                "status": status,
                "continent": continent,
                "company": company,
            }

            success.labels(**label_kwargs).set(1 if status == "ok" else 0)
            if status == "ok":
                rtt_min.labels(**label_kwargs).set(stats["min"])
                rtt_avg.labels(**label_kwargs).set(stats["avg"])
                rtt_max.labels(**label_kwargs).set(stats["max"])
                rtt_mdev.labels(**label_kwargs).set(stats["mdev"])

            with manager.last_node_status_lock:
                manager.last_node_status[node_short] = {
                    "status": status,
                    "city": city,
                    "cc": country,
                    "asn": asn,
                    "continent": continent,
                    "company": company,
                }

            if output_format == 'json':
                result = {
                    "node": node_short,
                    "target": target,
                    "asn": asn,
                    "city": city,
                    "countrycode": country,
                    "continent": continent,
                    "company": company,
                    "status": status,
                    "rtt_min": stats["min"] if stats else None,
                    "rtt_avg": stats["avg"] if stats else None,
                    "rtt_max": stats["max"] if stats else None,
                    "rtt_mdev": stats["mdev"] if stats else None,
                }
                json_results.append(result)

    if output_format == 'json':
        return jsonify({"results": json_results})

    return Response(generate_latest(registry), mimetype="text/plain")


@bp.route('/api/filter-options')
def filter_options():
    manager = _get_manager()
    nodes = manager.fetch_healthy_nodes()

    options = {field: set() for field in NODE_FILTER_FIELDS}
    for node in nodes:
        for field in NODE_FILTER_FIELDS:
            val = _node_field_value(node, field)
            if val:
                options[field].add(val)

    result = {field: sorted(vals) for field, vals in options.items()}
    result["countryNames"] = {cc: get_country_name(cc) for cc in options.get("countrycode", set())}
    return jsonify(result)


@bp.route('/health')
def health():
    manager = _get_manager()

    with manager.cache_lock:
        cache_size = len(manager.node_cache)
    with manager.session_health_lock:
        total_sessions = len(manager.session_health)
        healthy_sessions = sum(1 for v in manager.session_health.values() if v == "healthy")
    data = {
        "node_cache_size": cache_size,
        "sessions_total": total_sessions,
        "sessions_healthy": healthy_sessions,
    }
    if cache_size > 0 and healthy_sessions > 0:
        data["status"] = "healthy"
        return jsonify(data), 200
    data["status"] = "unhealthy"
    return jsonify(data), 503


@bp.route('/sessions')
def sessions():
    manager = _get_manager()

    with manager.session_health_lock:
        nodes = dict(manager.session_health)
    healthy = sum(1 for v in nodes.values() if v == "healthy")
    restarted = sum(1 for v in nodes.values() if v == "restarted")
    error = sum(1 for v in nodes.values() if v == "error")
    return jsonify({
        "summary": {"healthy": healthy, "restarted": restarted, "error": error, "total": len(nodes)},
        "nodes": nodes,
    })


@bp.route('/debug')
def debug_view():
    manager = _get_manager()

    grouped = {}

    with manager.cache_lock:
        nodes = manager.node_cache.copy()
    with manager.session_health_lock:
        health_map = dict(manager.session_health)

    for node in nodes:
        hostname = node["hostname"]
        status = health_map.get(hostname, "unknown")
        grouped.setdefault(status, []).append((hostname, node))

    status_order = {"healthy": 0, "restarted": 1, "error": 2, "unknown": 3}
    sorted_statuses = sorted(grouped.keys(), key=lambda s: (status_order.get(s, 99), s))

    lines = []
    for status in sorted_statuses:
        lines.append(f"=== {status} ({len(grouped[status])}) ===")
        for hostname, info in sorted(grouped[status], key=lambda x: x[0]):
            short = hostname.split('.')[0]
            lines.append(
                f"{short:30} [{info.get('company', 'Unknown')}, {info['city']}, "
                f"{get_country_name(info['countrycode'])}, ASN {info['asn']}, {info['continent']}]"
            )
        lines.append("")

    return Response("\n".join(lines), mimetype="text/plain")
