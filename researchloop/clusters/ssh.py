"""SSH connection manager using asyncssh."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import asyncssh

logger = logging.getLogger(__name__)


class SSHConnection:
    """Manages a single SSH connection to a remote host."""

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        key_path: str,
        known_hosts: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.key_path = key_path
        self.known_hosts = known_hosts
        self._conn: asyncssh.SSHClientConnection | None = None

    async def connect(self) -> SSHConnection:
        """Establish the SSH connection and return self."""
        connect_kwargs: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "username": self.user,
            "client_keys": [self.key_path],
            "agent_path": None,  # Don't use SSH agent; we have explicit keys.
        }
        if self.known_hosts is not None:
            connect_kwargs["known_hosts"] = self.known_hosts
        else:
            # Disable host key checking when no known_hosts file is provided.
            connect_kwargs["known_hosts"] = None

        logger.info("Connecting to %s@%s:%d", self.user, self.host, self.port)
        self._conn = await asyncio.wait_for(
            asyncssh.connect(**connect_kwargs),
            timeout=30,
        )
        logger.info("Connected to %s@%s:%d", self.user, self.host, self.port)
        return self

    @property
    def connection(self) -> asyncssh.SSHClientConnection:
        if self._conn is None:
            raise RuntimeError(
                "SSH connection is not established. Call connect() first."
            )
        return self._conn

    async def run(self, command: str, timeout: float = 30) -> tuple[str, str, int]:
        """Run a command over SSH.

        Returns:
            A tuple of (stdout, stderr, exit_code).
        """
        logger.debug("Running command on %s: %s", self.host, command)
        try:
            result = await asyncio.wait_for(
                self.connection.run(command, check=False),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.error(
                "Command timed out after %.1fs on %s: %s",
                timeout,
                self.host,
                command,
            )
            raise

        stdout = str(result.stdout or "")
        stderr = str(result.stderr or "")
        exit_code = result.exit_status if result.exit_status is not None else -1

        logger.debug("Command on %s finished with exit_code=%d", self.host, exit_code)
        return stdout, stderr, exit_code

    async def upload_file(self, local_path: str, remote_path: str) -> None:
        """Upload a local file to the remote host via SFTP."""
        logger.info("Uploading %s -> %s:%s", local_path, self.host, remote_path)
        async with self.connection.start_sftp_client() as sftp:
            await sftp.put(local_path, remote_path)
        logger.info("Upload complete: %s -> %s:%s", local_path, self.host, remote_path)

    async def download_file(self, remote_path: str, local_path: str) -> None:
        """Download a file from the remote host via SFTP."""
        logger.info("Downloading %s:%s -> %s", self.host, remote_path, local_path)
        # Ensure local parent directory exists.
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        async with self.connection.start_sftp_client() as sftp:
            await sftp.get(remote_path, local_path)
        logger.info(
            "Download complete: %s:%s -> %s", self.host, remote_path, local_path
        )

    async def close(self) -> None:
        """Close the SSH connection."""
        if self._conn is not None:
            self._conn.close()
            await self._conn.wait_closed()
            logger.info(
                "Closed connection to %s@%s:%d", self.user, self.host, self.port
            )
            self._conn = None

    # --- Context manager support ---

    async def __aenter__(self) -> SSHConnection:
        return await self.connect()

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()


class SSHManager:
    """Manages a pool of SSH connections keyed by cluster configuration."""

    def __init__(self) -> None:
        self._connections: dict[str, SSHConnection] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _config_key(cluster_config: dict[str, Any]) -> str:
        """Derive a unique key from cluster configuration."""
        host = cluster_config["host"]
        port = cluster_config["port"]
        user = cluster_config["user"]
        return f"{user}@{host}:{port}"

    async def get_connection(self, cluster_config: dict[str, Any]) -> SSHConnection:
        """Return an existing connection or create a new one.

        ``cluster_config`` must contain at minimum::

            {
                "host": str,
                "port": int,
                "user": str,
                "key_path": str,
                "known_hosts": str | None,  # optional
            }
        """
        key = self._config_key(cluster_config)

        async with self._lock:
            existing = self._connections.get(key)
            if existing is not None and existing._conn is not None:
                logger.debug("Reusing existing SSH connection for %s", key)
                return existing

            # Create a fresh connection.
            conn = SSHConnection(
                host=cluster_config["host"],
                port=cluster_config["port"],
                user=cluster_config["user"],
                key_path=cluster_config["key_path"],
                known_hosts=cluster_config.get("known_hosts"),
            )
            await conn.connect()
            self._connections[key] = conn
            return conn

    async def close_all(self) -> None:
        """Close every managed SSH connection."""
        async with self._lock:
            for key, conn in self._connections.items():
                try:
                    await conn.close()
                except Exception:
                    logger.exception("Error closing SSH connection %s", key)
            self._connections.clear()
            logger.info("All SSH connections closed.")
