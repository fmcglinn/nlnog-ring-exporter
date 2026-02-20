import os
import subprocess
import socket
import logging
import traceback

from core.config import (
    SSH_CONNECT_TIMEOUT, SSH_SUBPROCESS_TIMEOUT, SSH_KEY_PATH,
    SSH_USERNAME, PING_COUNT, PING_TIMEOUT, DEBUG, ssh_control_path,
)


def is_valid_target(host):
    try:
        socket.getaddrinfo(host, None)
        return True
    except socket.gaierror:
        return False


def ping_from_node(node, target):
    control_path = ssh_control_path(node)
    ssh_cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
        "-o", "StrictHostKeyChecking=accept-new",
        "-i", os.path.expanduser(SSH_KEY_PATH),
        "-l", SSH_USERNAME,
    ]

    ssh_cmd += ["-o", f"ControlPath={control_path}"]
    ssh_cmd += [node, f"ping -c{PING_COUNT} -W{PING_TIMEOUT} {target}"]
    logging.debug(f"Running SSH ping from {node} to {target}")
    try:
        output = subprocess.check_output(
            ssh_cmd, stderr=subprocess.STDOUT,
            timeout=SSH_SUBPROCESS_TIMEOUT, text=True,
        )
        logging.debug(f"Output from {node}:\n{output}")

        if "rtt" in output:
            rtt_line = next((l for l in output.splitlines() if l.startswith("rtt")), None)
            if not rtt_line:
                logging.warning(f"No RTT line found in output from {node}")
                return node, "no_rtt", None
            parts = rtt_line.split('=')[1].split()[0]
            min_rtt, avg_rtt, max_rtt, mdev_rtt = map(float, parts.split('/'))
            return node, "ok", {
                "min": min_rtt,
                "avg": avg_rtt,
                "max": max_rtt,
                "mdev": mdev_rtt
            }

        logging.warning(f"No RTT output in ping from {node}")
        return node, "no_rtt", None

    except subprocess.TimeoutExpired:
        logging.warning(f"SSH to {node} timed out")
        return node, "ssh_timeout", None
    except subprocess.CalledProcessError as e:
        logging.warning(f"Ping command failed on {node}: {e}")
        if DEBUG:
            logging.debug(e.output)
        return node, "ping_error", None
    except Exception as e:
        logging.error(f"Error pinging from {node}: {e}")
        if DEBUG:
            traceback.print_exc()
        return node, "exception", None
