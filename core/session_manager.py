import os
import getpass
import subprocess
import threading
import logging
import concurrent.futures
from typing import Set, Callable, Optional

from core.config import SSH_CONNECT_TIMEOUT, SSH_CONTROL_PATH_TEMPLATE, ssh_control_path

# Common SSH options applied to all SSH invocations (master, check, exit).
_SSH_COMMON_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=No",
    "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
]


class SSHSessionManager:
    def __init__(self, control_path_template: str = SSH_CONTROL_PATH_TEMPLATE, username: str = None, key_path: str = None):
        self.control_path_template = control_path_template
        self.username = username or os.getenv("SSH_USERNAME", getpass.getuser())
        self.key_path = os.path.expanduser(key_path) if key_path else None
        self.active_sessions: Set[str] = set()
        self.lock = threading.RLock()

    def _control_path(self, hostname: str) -> str:
        return ssh_control_path(hostname)

    def start_session(self, hostname: str) -> bool:
        """Start an SSH master session. Returns True on success."""
        with self.lock:
            if hostname in self.active_sessions:
                return True
            # Optimistic add — prevents duplicate concurrent attempts
            self.active_sessions.add(hostname)

        # SSH subprocess runs outside the lock for true parallelism
        try:
            logging.debug(f"Starting persistent SSH session to {hostname} as {self.username}")
            cmd = [
                "ssh", "-MNf",
                *_SSH_COMMON_OPTS,
                "-o", "ControlMaster=auto",
                "-o", f"ControlPath={self._control_path(hostname)}",
                "-o", "ControlPersist=yes",
            ]
            if self.key_path:
                cmd += ["-i", self.key_path]
            cmd.append(f"{self.username}@{hostname}")
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if result.returncode != 0:
                reason = result.stderr.strip() or f"exit code {result.returncode}"
                logging.warning(f"SSH session start failed for {hostname}: {reason}")
                with self.lock:
                    self.active_sessions.discard(hostname)
                return False
            return True
        except Exception as e:
            logging.warning(f"SSH session start error for {hostname}: {e}")
            with self.lock:
                self.active_sessions.discard(hostname)
            return False

    def stop_session(self, hostname: str):
        """Stop an SSH master session."""
        with self.lock:
            if hostname not in self.active_sessions:
                return
            self.active_sessions.discard(hostname)

        # SSH exit runs outside the lock
        try:
            logging.debug(f"Stopping SSH session for {hostname}")
            result = subprocess.run([
                "ssh", "-O", "exit",
                *_SSH_COMMON_OPTS,
                "-o", f"ControlPath={self._control_path(hostname)}",
                f"{self.username}@{hostname}"
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if result.returncode != 0:
                reason = result.stderr.strip() or f"exit code {result.returncode}"
                logging.warning(f"SSH session stop failed for {hostname}: {reason}")
        except Exception as e:
            logging.warning(f"SSH session stop error for {hostname}: {e}")

    def cleanup_stale_sockets(self):
        """Scan for stale SSH control sockets and clean up dead ones.

        Live sockets are recovered into active_sessions.
        Dead sockets are removed from disk.
        """
        # Derive the control directory from the template
        sample_path = self.control_path_template.replace("%r", self.username).replace("%h", "x").replace("%p", "22")
        control_dir = os.path.dirname(sample_path)

        if not os.path.isdir(control_dir):
            logging.info("Control socket directory does not exist: %s", control_dir)
            return

        # Derive filename prefix (everything before the first %-token)
        basename_template = os.path.basename(self.control_path_template)
        prefix = basename_template.split("%")[0]  # e.g. "nlnog-"

        recovered = 0
        removed = 0

        for entry in os.listdir(control_dir):
            if not entry.startswith(prefix):
                continue

            socket_path = os.path.join(control_dir, entry)

            # Parse hostname from filename format: nlnog-rise@hostname:22
            try:
                remainder = entry[len(prefix):]  # e.g. "rise@hostname:22"
                at_idx = remainder.index("@")
                colon_idx = remainder.rindex(":")
                hostname = remainder[at_idx + 1:colon_idx]
            except (ValueError, IndexError):
                logging.debug("Could not parse hostname from socket file: %s", entry)
                continue

            # Check if the master process is alive
            try:
                result = subprocess.run([
                    "ssh", "-O", "check",
                    *_SSH_COMMON_OPTS,
                    "-o", f"ControlPath={socket_path}",
                    f"{self.username}@{hostname}"
                ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)

                if result.returncode == 0:
                    logging.info("Recovered live session from socket: %s", hostname)
                    with self.lock:
                        self.active_sessions.add(hostname)
                    recovered += 1
                else:
                    logging.debug("Removing stale socket for %s", hostname)
                    os.remove(socket_path)
                    removed += 1
            except subprocess.TimeoutExpired:
                logging.debug("Socket check timed out for %s, removing", hostname)
                try:
                    os.remove(socket_path)
                except OSError:
                    pass
                removed += 1
            except OSError as e:
                logging.warning("Error checking/removing socket %s: %s", socket_path, e)

        logging.info("Socket cleanup: %d recovered, %d stale removed", recovered, removed)

    def start_sessions_parallel(self, hostnames: Set[str], max_workers: int = 50,
                                progress_callback: Optional[Callable[[str, bool], None]] = None):
        """Start SSH sessions in parallel for a set of hostnames."""
        with self.lock:
            to_start = set(hostnames) - self.active_sessions

        if not to_start:
            logging.info("All %d sessions already active", len(hostnames))
            return

        logging.info("Starting %d SSH sessions (max_workers=%d)", len(to_start), max_workers)
        completed = 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_host = {executor.submit(self.start_session, host): host for host in sorted(to_start)}
            for future in concurrent.futures.as_completed(future_to_host):
                hostname = future_to_host[future]
                try:
                    success = future.result()
                except Exception as e:
                    logging.warning("Session start exception for %s: %s", hostname, e)
                    success = False

                completed += 1
                if progress_callback:
                    progress_callback(hostname, success)

                if completed % 50 == 0 or completed == len(to_start):
                    logging.info("Session startup progress: %d/%d", completed, len(to_start))

    def sync_sessions(self, desired_hostnames: Set[str]):
        with self.lock:
            current = self.active_sessions.copy()
            to_add = desired_hostnames - current
            to_remove = current - desired_hostnames

        for host in to_add:
            self.start_session(host)
        for host in to_remove:
            self.stop_session(host)

    def cleanup(self):
        with self.lock:
            hosts = list(self.active_sessions)
        for host in hosts:
            self.stop_session(host)
        with self.lock:
            self.active_sessions.clear()
