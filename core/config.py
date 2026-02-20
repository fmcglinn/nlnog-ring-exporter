import os
from dotenv import load_dotenv

load_dotenv()


def _bool(value):
    return str(value).lower() in ("true", "1", "yes")


# NLNOG API
NLNOG_API = os.getenv("NLNOG_API", "https://api.ring.nlnog.net/1.0/nodes/active")
NLNOG_PARTICIPANTS_API = os.getenv("NLNOG_PARTICIPANTS_API", "https://api.ring.nlnog.net/1.0/participants")
NLNOG_API_TIMEOUT = int(os.getenv("NLNOG_API_TIMEOUT", "10"))

# SSH settings
SSH_USERNAME = os.getenv("SSH_USERNAME", "rise")
SSH_CONNECT_TIMEOUT = int(os.getenv("SSH_CONNECT_TIMEOUT", "5"))
SSH_SUBPROCESS_TIMEOUT = int(os.getenv("SSH_SUBPROCESS_TIMEOUT", "15"))
SSH_KEY_PATH = os.getenv("SSH_KEY_PATH", "/app/ssh/nlnog")
SSH_CONTROL_PATH_TEMPLATE = os.getenv("SSH_CONTROL_PATH_TEMPLATE", "/tmp/ssh-control/nlnog-%r@%h:%p")

# Ping settings
PING_COUNT = int(os.getenv("PING_COUNT", "10"))
PING_TIMEOUT = int(os.getenv("PING_TIMEOUT", "5"))

# Startup settings
STARTUP_MAX_WORKERS = int(os.getenv("STARTUP_MAX_WORKERS", "50"))

# Application settings
THREADS = int(os.getenv("THREADS", "100"))
CACHE_REFRESH_INTERVAL = int(os.getenv("CACHE_REFRESH_INTERVAL", "300"))
FLASK_HOST = os.getenv("FLASK_HOST", "0.0.0.0")
FLASK_PORT = int(os.getenv("FLASK_PORT", "8000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
DEBUG = _bool(os.getenv("DEBUG", "false"))


def ssh_control_path(hostname):
    """Expand SSH control path template for a given hostname."""
    path = SSH_CONTROL_PATH_TEMPLATE.replace("%r", SSH_USERNAME).replace("%h", hostname).replace("%p", "22")
    return os.path.expanduser(path)
