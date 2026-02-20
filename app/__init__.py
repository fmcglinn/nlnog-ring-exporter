import os
import subprocess
import logging
import threading

from flask import Flask

from core.config import (
    SSH_USERNAME, SSH_KEY_PATH, SSH_CONTROL_PATH_TEMPLATE,
    NLNOG_API, NLNOG_PARTICIPANTS_API, NLNOG_API_TIMEOUT,
    SSH_CONNECT_TIMEOUT, SSH_SUBPROCESS_TIMEOUT,
    PING_COUNT, PING_TIMEOUT, STARTUP_MAX_WORKERS,
    THREADS, CACHE_REFRESH_INTERVAL, LOG_LEVEL, DEBUG,
    FLASK_HOST, FLASK_PORT,
)
from core.session_manager import SSHSessionManager
from core.node_manager import NodeManager


def _validate_ssh_key():
    """Validate the SSH key exists and is usable. Returns True on success."""
    expanded = os.path.expanduser(SSH_KEY_PATH)
    logging.info("SSH key path: %s (expanded: %s)", SSH_KEY_PATH, expanded)

    if not os.path.exists(expanded):
        logging.error("SSH key file does not exist: %s", expanded)
        return False

    if not os.path.isfile(expanded):
        logging.error("SSH key path is not a regular file: %s", expanded)
        return False

    if not os.access(expanded, os.R_OK):
        logging.error("SSH key file is not readable (check permissions): %s", expanded)
        return False

    # Check file permissions (should be 600 or 400)
    mode = oct(os.stat(expanded).st_mode & 0o777)
    if os.stat(expanded).st_mode & 0o077:
        logging.warning("SSH key file has loose permissions (%s) — SSH may refuse it: %s", mode, expanded)
    else:
        logging.info("SSH key file permissions: %s", mode)

    # Try to get the key fingerprint via ssh-keygen
    try:
        result = subprocess.run(
            ["ssh-keygen", "-l", "-f", expanded],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            fingerprint_line = result.stdout.strip()
            logging.info("SSH key fingerprint: %s", fingerprint_line)
        else:
            logging.warning("ssh-keygen could not read key: %s", result.stderr.strip())
    except FileNotFoundError:
        logging.warning("ssh-keygen not found — cannot display key fingerprint")
    except subprocess.TimeoutExpired:
        logging.warning("ssh-keygen timed out reading key")

    return True


def _startup_banner():
    """Log configuration summary and validate prerequisites."""
    logging.info("=" * 60)
    logging.info("Starting NLNOG Ring Prometheus Exporter")
    logging.info("=" * 60)

    # Configuration summary
    logging.info("Configuration:")
    logging.info("  NLNOG API:            %s", NLNOG_API)
    logging.info("  NLNOG Participants:   %s", NLNOG_PARTICIPANTS_API)
    logging.info("  NLNOG API timeout:    %ds", NLNOG_API_TIMEOUT)
    logging.info("  SSH username:         %s", SSH_USERNAME)
    logging.info("  SSH connect timeout:  %ds", SSH_CONNECT_TIMEOUT)
    logging.info("  SSH command timeout:  %ds", SSH_SUBPROCESS_TIMEOUT)
    logging.info("  SSH control path:     %s", SSH_CONTROL_PATH_TEMPLATE)
    logging.info("  Ping count/timeout:   %d / %ds", PING_COUNT, PING_TIMEOUT)
    logging.info("  Startup max workers:  %d", STARTUP_MAX_WORKERS)
    logging.info("  Worker threads:       %d", THREADS)
    logging.info("  Cache refresh:        %ds", CACHE_REFRESH_INTERVAL)
    logging.info("  Log level:            %s", LOG_LEVEL)
    logging.info("  Debug mode:           %s", DEBUG)
    logging.info("  Listen:               %s:%d", FLASK_HOST, FLASK_PORT)

    # Validate SSH key
    logging.info("-" * 60)
    key_ok = _validate_ssh_key()
    logging.info("-" * 60)

    if not key_ok:
        logging.error("SSH key validation failed — SSH sessions will not work")
    else:
        logging.info("SSH key validation passed")

    logging.info("=" * 60)


def create_app():
    # Logging (module-level so it works under Gunicorn)
    logging.basicConfig(
        level=logging.DEBUG if DEBUG else getattr(logging, LOG_LEVEL, logging.INFO),
        format='[%(asctime)s] [%(levelname)s] %(message)s',
    )

    flask_app = Flask(__name__)

    # Create SSH session manager and node manager
    ssh_sessions = SSHSessionManager(
        control_path_template=SSH_CONTROL_PATH_TEMPLATE,
        username=SSH_USERNAME,
        key_path=SSH_KEY_PATH,
    )
    node_manager = NodeManager(ssh_sessions)

    # Store on app for route access
    flask_app.config["node_manager"] = node_manager

    # Register routes
    from app.routes import bp
    flask_app.register_blueprint(bp)

    # Startup banner + SSH key validation
    _startup_banner()

    # Start background cache thread
    cache_thread = threading.Thread(target=node_manager.run_cache_loop, daemon=True)
    cache_thread.start()
    logging.info("Node cache refresh thread started")

    return flask_app
