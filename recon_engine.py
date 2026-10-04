"""
Safe Reconnaissance Engine for RAVEN
Provides async, bounded subprocess wrappers for authorized local testing only.
"""

import asyncio
import ipaddress
import re
import os
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse


class SafetyError(Exception):
    """Raised when a target fails safety checks"""
    pass


# Ranges that `ipaddress.is_private` reports as private but that must never be
# reachable through this engine:
#
#   169.254.0.0/16  link-local, and 169.254.169.254 is the cloud instance
#                   metadata endpoint on AWS/GCP/Azure/DO. Reading it leaks
#                   the node's IAM credentials.
#   0.0.0.0/8       "this host" on Linux, i.e. an unintended loopback route.
#   100.64.0.0/10   CGNAT, spans provider-internal infrastructure.
#   192.0.0.0/24    IETF protocol assignments.
#   198.18.0.0/15   benchmarking.
#   203.0.113.0/24  TEST-NET-3.
#   240.0.0.0/4     reserved, includes 255.255.255.255 broadcast.
#
# The remainder of is_private is RFC1918 (10/8, 172.16/12, 192.168/16) plus
# loopback, which is what this engine is actually for.
_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "169.254.0.0/16",
        "0.0.0.0/8",
        "100.64.0.0/10",
        "192.0.0.0/24",
        "198.18.0.0/15",
        "203.0.113.0/24",
        "240.0.0.0/4",
    )
)


class ReconEngine:
    """Safe reconnaissance engine with strict bounds checking"""

    def __init__(self):
        # Load pre-approved domains from environment
        self.pre_approved_domains = self._load_pre_approved_domains()

    def _load_pre_approved_domains(self) -> List[str]:
        """Load pre-approved domains from environment variable"""
        domains_str = os.getenv("RAVEN_PRE_APPROVED_DOMAINS", "")
        if not domains_str:
            return []
        # Split by comma and clean up whitespace
        return [domain.strip().lower() for domain in domains_str.split(",") if domain.strip()]

    @staticmethod
    def _strip_port(host: str) -> str:
        """Drop a trailing :port without mangling a bare IPv6 literal.

        `split(":")` turns "::1" into "" and "fe80::1" into "fe80", which
        silently rejected every IPv6-local target.
        """
        host = host.strip()
        if host.startswith("["):                 # [::1] or [::1]:8080
            end = host.find("]")
            return host[1:end] if end != -1 else host[1:]
        if host.count(":") == 1:                 # host:port
            return host.split(":", 1)[0]
        return host                               # bare IPv6, keep intact

    def is_local_ip(self, host: str) -> bool:
        """True only for addresses this engine is meant to probe.

        Loopback plus RFC1918. Explicitly not `is_private`, which is far wider
        and includes the cloud metadata endpoint.
        """
        host = self._strip_port(host)
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        for net in _BLOCKED_NETWORKS:
            if ip.version == net.version and ip in net:
                return False
        return bool(ip.is_loopback or ip.is_private)

    def _resolves_to_loopback(self, host: str) -> bool:
        """True when every address `host` resolves to is loopback.

        Lets "localhost" work without trusting the name outright, since a
        poisoned hosts entry could otherwise point it off-box.
        """
        try:
            import socket

            infos = socket.getaddrinfo(host, None)
        except OSError:
            return False
        if not infos:
            return False
        for info in infos:
            try:
                if not ipaddress.ip_address(info[4][0]).is_loopback:
                    return False
            except ValueError:
                return False
        return True

    def is_approved_domain(self, host: str) -> bool:
        """Check if host is in pre-approved domains list"""
        host_lower = self._strip_port(host).lower()
        if host_lower in ("localhost", "localhost.localdomain"):
            return self._resolves_to_loopback(host_lower)
        if not self.pre_approved_domains:
            return False
        return host_lower in self.pre_approved_domains

    def validate_target(self, target: str) -> str:
        """
        Validate target for safety.
        Returns a normalized, port-free host if valid, raises SafetyError if not.
        """
        host = target.strip()
        if "://" in host:
            parsed = urlparse(host)
            host = parsed.netloc or parsed.path
        else:
            host = host.split("/")[0]

        # Credentials in a URL are not part of the host we probe.
        if "@" in host:
            host = host.rsplit("@", 1)[1]

        host = self._strip_port(host)
        if not host:
            raise SafetyError(f"Target '{target}' is not a usable host.")

        if not (self.is_local_ip(host) or self.is_approved_domain(host)):
            raise SafetyError(
                f"Target '{target}' is not authorized. "
                f"Only loopback and RFC1918 addresses (127.0.0.1, 10.x.x.x, "
                f"172.16.x.x, 192.168.x.x) or hosts listed in "
                f"RAVEN_PRE_APPROVED_DOMAINS are allowed."
            )

        return host

    async def bounded_subprocess(self, cmd: List[str], timeout: int = 10) -> Dict[str, Any]:
        """
        Execute a subprocess with strict bounds and timeout.
        Returns dict with stdout, stderr, returncode.
        """
        try:
            # Create subprocess with limited permissions
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Limit resources
                limit=1024*1024  # 1MB max output
            )

            # Wait for completion with timeout
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout
                )

                return {
                    "stdout": stdout.decode('utf-8', errors='replace'),
                    "stderr": stderr.decode('utf-8', errors='replace'),
                    "returncode": process.returncode,
                    "timeout": False
                }
            except asyncio.TimeoutError:
                # Kill process on timeout
                try:
                    process.kill()
                    await process.wait()
                except ProcessLookupError:
                    pass  # Process already died

                return {
                    "stdout": "",
                    "stderr": f"Command timed out after {timeout} seconds",
                    "returncode": -1,
                    "timeout": True
                }

        except FileNotFoundError:
            return {
                "stdout": "",
                "stderr": f"Command not found: {cmd[0]}",
                "returncode": -2,
                "timeout": False
            }
        except Exception as e:
            return {
                "stdout": "",
                "stderr": f"Error executing command: {str(e)}",
                "returncode": -3,
                "timeout": False
            }

    async def fetch_http_headers(self, host: str, port: int = 80, use_ssl: bool = False) -> Dict[str, Any]:
        """
        Safely fetch HTTP headers from a local target.
        Non-destructive, read-only operation.
        """
        # Validate host is local
        validated_host = self.validate_target(host)

        # Build target string for logging
        protocol = "https" if use_ssl else "http"
        target_str = f"{protocol}://{validated_host}:{port}"

        # Use netcat or similar for header fetching (safer than full HTTP client)
        # We'll use a simple socket connection to get headers
        try:
            if use_ssl:
                # For SSL, we'll skip for safety in this basic implementation
                # In a full implementation, you might use ssl.create_default_context()
                return {
                    "error": "SSL not implemented in basic safety version",
                    "target": target_str
                }

            # Open TCP connection and send basic GET request
            reader, writer = await asyncio.open_connection(validated_host, port)

            # Send minimal HTTP GET request
            request = f"GET / HTTP/1.1\r\nHost: {validated_host}\r\nUser-Agent: RAVEN-Recon/1.0\r\nConnection: close\r\n\r\n"
            writer.write(request.encode())
            await writer.drain()

            # Read response with timeout
            try:
                response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
                headers = response.decode('utf-8', errors='replace')

                # Read a bit more to get some content (optional)
                content = b""
                try:
                    content = await asyncio.wait_for(reader.read(1024), timeout=2.0)
                except asyncio.TimeoutError:
                    pass  # No content or slow response

                writer.close()
                await writer.wait_closed()

                return {
                    "target": target_str,
                    "headers": headers,
                    "content_length": len(content),
                    "status": "success"
                }
            except asyncio.TimeoutError:
                writer.close()
                await writer.wait_closed()
                return {
                    "target": target_str,
                    "error": "Timeout reading response",
                    "status": "timeout"
                }

        except ConnectionRefusedError:
            return {
                "target": target_str,
                "error": "Connection refused",
                "status": "connection_refused"
            }
        except Exception as e:
            return {
                "target": target_str,
                "error": f"Connection error: {str(e)}",
                "status": "error"
            }

    async def port_scan(self, host: str, ports: List[int], timeout: int = 3) -> Dict[str, Any]:
        """
        Perform a safe port scan on local target only.
        """
        # Validate host is local
        validated_host = self.validate_target(host)

        open_ports = []
        closed_ports = []

        for port in ports:
            try:
                # Try to connect with short timeout
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(validated_host, port),
                    timeout=timeout
                )
                # If we get here, port is open
                open_ports.append(port)
                writer.close()
                await writer.wait_closed()
            except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
                # Port is closed or unreachable
                closed_ports.append(port)
            except Exception:
                # Other error
                closed_ports.append(port)

        return {
            "target": validated_host,
            "open_ports": open_ports,
            "closed_ports": closed_ports,
            "scanned_ports": ports,
            "status": "completed"
        }

    async def ping_host(self, host: str, count: int = 2) -> Dict[str, Any]:
        """
        Safely ping a local host using system ping command.
        """
        # Validate host is local
        validated_host = self.validate_target(host)

        # Use system ping with bounds
        if os.name == 'nt':  # Windows
            cmd = ["ping", "-n", str(count), "-w", "2000", validated_host]
        else:  # Unix/Linux/macOS
            cmd = ["ping", "-c", str(count), "-W", "2", validated_host]

        result = await self.bounded_subprocess(cmd, timeout=10)

        return {
            "target": validated_host,
            "ping_result": result
        }


# Convenience functions for direct use
async def safe_http_headers(target: str, port: int = 80, use_ssl: bool = False) -> Dict[str, Any]:
    """Convenience function to fetch HTTP headers"""
    engine = ReconEngine()
    return await engine.fetch_http_headers(target, port, use_ssl)


async def safe_port_scan(target: str, ports: List[int], timeout: int = 3) -> Dict[str, Any]:
    """Convenience function for port scanning"""
    engine = ReconEngine()
    return await engine.port_scan(target, ports, timeout)


async def safe_ping(target: str, count: int = 2) -> Dict[str, Any]:
    """Convenience function for pinging"""
    engine = ReconEngine()
    return await engine.ping_host(target, count)