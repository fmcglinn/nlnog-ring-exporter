import logging
import os

bind = f"0.0.0.0:{os.getenv('FLASK_PORT', '8000')}"
workers = 1          # Required: app uses in-process shared state
threads = 4          # Concurrent request handling
worker_class = "gthread"
timeout = 120        # /probe can take ~15s per node
accesslog = "-"
errorlog = "-"

# --- Unified log format ---------------------------------------------------
# Match the application's [datetime] [LEVEL] message format so all lines
# (Gunicorn access, Gunicorn error, and application) look identical.

_log_level = os.getenv("LOG_LEVEL", "INFO").upper()
if str(os.getenv("DEBUG", "false")).lower() in ("true", "1", "yes"):
    _log_level = "DEBUG"

access_log_format = '%(h)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s"'

logconfig_dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "[%(asctime)s] [%(levelname)s] %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "stream": "ext://sys.stderr",
        },
    },
    "root": {
        "level": _log_level,
        "handlers": ["console"],
    },
    "loggers": {
        "gunicorn.error": {
            "level": _log_level,
            "handlers": ["console"],
            "propagate": False,
        },
        "gunicorn.access": {
            "level": "INFO",
            "handlers": ["console"],
            "propagate": False,
        },
    },
}


# --- Graceful shutdown -----------------------------------------------------
# Let Gunicorn manage signals; clean up SSH sessions when the worker exits.

def worker_exit(server, worker):
    """Called by Gunicorn after a worker process has exited."""
    app = worker.app.wsgi()
    node_manager = app.config.get("node_manager")
    if node_manager:
        logging.info("Worker exiting — cleaning up SSH sessions...")
        node_manager.shutdown_event.set()
        node_manager.ssh_sessions.cleanup()
        logging.info("SSH session cleanup complete")
