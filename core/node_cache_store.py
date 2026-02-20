import json
import os
import logging
import tempfile

_CACHE_PATH = "/tmp/ssh-control/node_cache.json"


def save_node_cache(nodes):
    """Atomically persist the node list to disk."""
    try:
        cache_dir = os.path.dirname(_CACHE_PATH)
        os.makedirs(cache_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=cache_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(nodes, f)
            os.replace(tmp_path, _CACHE_PATH)
            logging.debug("Persisted node cache (%d nodes) to %s", len(nodes), _CACHE_PATH)
        except BaseException:
            os.unlink(tmp_path)
            raise
    except Exception as e:
        logging.warning("Failed to persist node cache: %s", e)


def load_node_cache():
    """Load the persisted node list from disk. Returns None on any error."""
    try:
        with open(_CACHE_PATH, "r") as f:
            nodes = json.load(f)
        logging.info("Loaded persisted node cache (%d nodes) from %s", len(nodes), _CACHE_PATH)
        return nodes
    except Exception as e:
        logging.debug("Could not load persisted node cache: %s", e)
        return None
