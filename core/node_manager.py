import threading
import logging
import random
import traceback
import concurrent.futures
import requests

from core.config import (
    NLNOG_API, NLNOG_PARTICIPANTS_API, NLNOG_API_TIMEOUT,
    THREADS, CACHE_REFRESH_INTERVAL, STARTUP_MAX_WORKERS, DEBUG,
    SSH_USERNAME, SSH_CONTROL_PATH_TEMPLATE, SSH_KEY_PATH,
    ssh_control_path,
)
from core.geo import get_continent
from core.node_cache_store import save_node_cache, load_node_cache
from core.session_manager import SSHSessionManager

import subprocess

# Fields available for filtering in /probe requests.
# Maps query-parameter name to the key in the node dict.
# "node" is special -- it matches against the short hostname.
NODE_FILTER_FIELDS = {
    "node": None,  # handled specially via hostname
    "asn": "asn",
    "city": "city",
    "countrycode": "countrycode",
    "continent": "continent",
    "company": "company",
}


def _node_field_value(node, field):
    """Get the value of a filter field from a node."""
    if field == "node":
        return node["hostname"].split('.')[0]
    return node.get(NODE_FILTER_FIELDS[field]) or ""


def _balanced_sample(nodes, limit, filters):
    """Randomly sample `limit` nodes, balanced across multi-value filter fields.

    Groups nodes by the combination of all filter fields that have more than
    one value, then distributes the quota evenly across groups.  If a group
    has fewer nodes than its share, the surplus is redistributed to the
    remaining groups.
    """
    # Identify which filter fields have multiple values
    balance_fields = [f for f, vals in filters.items() if len(vals) > 1]

    if not balance_fields:
        return random.sample(nodes, limit)

    # Group nodes by their combined balance-field values
    groups = {}
    for node in nodes:
        key = tuple(_node_field_value(node, f).lower() for f in balance_fields)
        groups.setdefault(key, []).append(node)

    # Distribute quota evenly, then redistribute any shortfall
    group_keys = list(groups.keys())
    random.shuffle(group_keys)

    base_quota, remainder = divmod(limit, len(group_keys))
    quotas = {
        key: base_quota + (1 if i < remainder else 0)
        for i, key in enumerate(group_keys)
    }

    result = []
    shortfall = 0
    for key in group_keys:
        group = groups[key]
        quota = quotas[key]
        take = min(quota, len(group))
        result.extend(random.sample(group, take))
        shortfall += quota - take

    # Fill shortfall from nodes not yet selected
    if shortfall > 0:
        selected = {id(n) for n in result}
        remaining = [n for n in nodes if id(n) not in selected]
        if remaining:
            result.extend(random.sample(remaining, min(shortfall, len(remaining))))

    return result


class NodeManager:
    def __init__(self, ssh_sessions: SSHSessionManager):
        self.ssh_sessions = ssh_sessions

        # Shared state with individual locks
        self.node_cache = []
        self.cache_lock = threading.Lock()

        self.session_health = {}
        self.session_health_lock = threading.Lock()

        self.last_node_status = {}
        self.last_node_status_lock = threading.Lock()

        self.shutdown_event = threading.Event()

        self._startup_done = False

    def fetch_participants(self):
        """Fetch participant list from NLNOG API and return {id: company} map."""
        try:
            response = requests.get(NLNOG_PARTICIPANTS_API, timeout=NLNOG_API_TIMEOUT)
            participants = response.json()["results"]["participants"]
            return {p["id"]: p["company"] for p in participants}
        except Exception as e:
            logging.warning("Failed to fetch participants: %s", e)
            return {}

    def filter_api_nodes(self, raw_nodes, participants=None):
        """Filter and transform raw NLNOG API nodes into the internal format."""
        if participants is None:
            participants = {}
        filtered = []
        for n in raw_nodes:
            if not (n["alive_ipv4"] and n["alive_ipv6"]):
                continue
            cc = n["countrycode"].upper()
            continent = get_continent(cc)
            company = participants.get(n.get("participant"), "Unknown")
            filtered.append({
                "hostname": n["hostname"],
                "asn": str(n["asn"]),
                "city": n["city"],
                "countrycode": cc,
                "continent": continent,
                "company": company,
            })
        return filtered

    def startup_restore_sessions(self):
        """Restore SSH sessions on startup, using cached nodes if API is unavailable."""
        # 1. Clean stale sockets
        self.ssh_sessions.cleanup_stale_sockets()

        # 2. Try loading persisted node list as fallback
        cached_nodes = load_node_cache()

        # 3. Try fetching fresh node list from API
        api_nodes = None
        try:
            participants = self.fetch_participants()
            response = requests.get(NLNOG_API, timeout=NLNOG_API_TIMEOUT)
            raw_nodes = response.json()["results"]["nodes"]
            api_nodes = self.filter_api_nodes(raw_nodes, participants)
            save_node_cache(api_nodes)
            logging.info("Fetched %d nodes from API during startup (%d participants loaded)", len(api_nodes), len(participants))
        except Exception as e:
            logging.warning("API unavailable during startup: %s", e)

        # 4. Use API list if available, otherwise fall back to persisted list
        nodes = api_nodes if api_nodes is not None else cached_nodes
        if nodes is None:
            logging.warning("No node list available (API down, no cache) — skipping startup restore")
            return

        # 5. Populate node_cache immediately
        with self.cache_lock:
            self.node_cache = nodes
        logging.info("Populated node cache with %d nodes", len(nodes))

        # 6. Start sessions in parallel with progress callback
        hostnames = {n["hostname"] for n in nodes}

        def _progress(hostname, success):
            if success:
                with self.session_health_lock:
                    self.session_health[hostname] = "healthy"

        self.ssh_sessions.start_sessions_parallel(
            hostnames, max_workers=STARTUP_MAX_WORKERS, progress_callback=_progress,
        )

        with self.session_health_lock:
            healthy_count = sum(1 for v in self.session_health.values() if v == "healthy")
        logging.info("Startup restore complete: %d/%d sessions healthy", healthy_count, len(hostnames))

    def check_and_manage_ssh_session(self, hostname):
        self.ssh_sessions.start_session(hostname)

        control_path = ssh_control_path(hostname)
        result = subprocess.run([
            "ssh", "-O", "check",
            "-o", "BatchMode=yes",
            "-o", f"ControlPath={control_path}",
            "-l", SSH_USERNAME,
            hostname
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        if result.returncode == 0:
            logging.debug(f"SSH session to {hostname} is healthy")
            with self.session_health_lock:
                self.session_health[hostname] = "healthy"
            return True

        reason = result.stderr.strip() or f"exit code {result.returncode}"
        logging.warning(f"SSH health check failed for {hostname}: {reason} — restarting session")
        self.ssh_sessions.stop_session(hostname)
        self.ssh_sessions.start_session(hostname)
        with self.session_health_lock:
            self.session_health[hostname] = "restarted"
        return False

    def run_cache_loop(self):
        if not self._startup_done:
            self.startup_restore_sessions()
            self._startup_done = True

        while not self.shutdown_event.is_set():
            try:
                participants = self.fetch_participants()
                response = requests.get(NLNOG_API, timeout=NLNOG_API_TIMEOUT)
                raw_nodes = response.json()["results"]["nodes"]
                filtered = self.filter_api_nodes(raw_nodes, participants)

                save_node_cache(filtered)

                with self.cache_lock:
                    self.node_cache = filtered

                hostnames = {n["hostname"] for n in filtered}
                with concurrent.futures.ThreadPoolExecutor(max_workers=THREADS) as executor:
                    executor.map(self.check_and_manage_ssh_session, sorted(hostnames))

                # Prune health entries and stop sessions for nodes no longer in cache
                with self.session_health_lock:
                    stale = set(self.session_health.keys()) - hostnames
                    for h in stale:
                        del self.session_health[h]
                for h in stale:
                    self.ssh_sessions.stop_session(h)

                with self.session_health_lock:
                    healthy_count = sum(1 for v in self.session_health.values() if v == "healthy")
                logging.info(f"Updated node cache with {len(filtered)} nodes ({healthy_count} healthy sessions)")

            except Exception as e:
                logging.error(f"Failed to update node cache: {e}")
                if DEBUG:
                    traceback.print_exc()

            self.shutdown_event.wait(timeout=CACHE_REFRESH_INTERVAL)

    def fetch_healthy_nodes(self, limit=None, filters=None):
        with self.cache_lock:
            nodes = self.node_cache.copy()
        with self.session_health_lock:
            healthy = [n for n in nodes if self.session_health.get(n["hostname"]) == "healthy"]

        if filters:
            for field, allowed_values in filters.items():
                healthy = [n for n in healthy if _node_field_value(n, field).lower() in allowed_values]

        if limit is not None and limit < len(healthy):
            if filters:
                return _balanced_sample(healthy, limit, filters)
            return random.sample(healthy, limit)
        return healthy

    def shutdown(self, signum, frame):
        logging.info("Received signal %s, shutting down...", signum)
        self.shutdown_event.set()
        logging.info("Cleaning up SSH sessions...")
        self.ssh_sessions.cleanup()
        logging.info("SSH session cleanup complete")
