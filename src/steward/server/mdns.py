"""LAN discovery: advertise _steward._tcp.local via zeroconf.

Publishes a service instance named ``steward-<port>._steward._tcp.local.`` with
a TXT record carrying version, port, model and paired_count so a phone on the
same LAN can find the daemon without typing an IP.

The service is advertised under the hostname (SRV owner) with A records for
the primary LAN address plus loopback, so both a phone on the LAN and a
client on the same box can resolve it. On hosts where multicast on the
physical NIC is restricted the loopback copy still lets local tools browse.

zeroconf is imported lazily: when the dependency is missing the daemon prints
a warning and runs without mDNS rather than failing to boot. All zeroconf
calls happen on the caller's thread; the library is thread-safe enough for
register/update/unregister from a single owner (the daemon main thread).
"""

from __future__ import annotations

import socket
import threading
from typing import Callable

try:  # pragma: no cover - exercised in live runs only
    from zeroconf import (  # type: ignore[import-not-found, unused-ignore]
        ServiceInfo as _ServiceInfo,
        Zeroconf as _Zeroconf,
    )
    HAS_ZEROCONF = True
except ImportError:  # pragma: no cover - degrade path
    _ServiceInfo = None  # type: ignore[assignment]
    _Zeroconf = None  # type: ignore[assignment]
    HAS_ZEROCONF = False

SERVICE_TYPE = "_steward._tcp.local."


def lan_ip() -> str:
    """Best-effort primary LAN IPv4 for the pairing URL and A records.

    The UDP connect trick picks the interface that would carry outbound
    traffic without sending any packet. Falls back to the hostname lookup,
    then loopback.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


def instance_name(port: int) -> str:
    return f"steward-{port}.{SERVICE_TYPE}"


class Advertiser:
    """Publishes and refreshes the daemon's mDNS service record."""

    def __init__(self, *, port: int, model: str, version: str,
                 paired_count: Callable[[], int],
                 host_ip: str | None = None,
                 host_ips: list[str] | None = None,
                 service_name: str | None = None) -> None:
        self.port = port
        self.model = model
        self.version = version
        self._paired_count = paired_count
        # Advertise the LAN address plus loopback so phones on the LAN and
        # local tools both resolve the instance; dedupe and keep order.
        raw = list(host_ips) if host_ips else ([host_ip] if host_ip else [lan_ip()])
        raw.append("127.0.0.1")
        self.host_ips: list[str] = []
        for ip in raw:
            if ip and ip not in self.host_ips:
                self.host_ips.append(ip)
        host = (socket.gethostname() or "steward").replace(" ", "-")
        self.host_server = f"{host}.local."
        self.service_name = service_name or instance_name(port)
        self._zc = None
        self._info = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return HAS_ZEROCONF

    def _properties(self) -> dict:
        return {
            "version": str(self.version).encode("utf-8"),
            "port": str(self.port).encode("utf-8"),
            "model": str(self.model).encode("utf-8"),
            "paired_count": str(self._paired_count()).encode("utf-8"),
        }

    def _build_info(self):
        assert _ServiceInfo is not None
        return _ServiceInfo(
            SERVICE_TYPE,
            self.service_name,
            addresses=[socket.inet_aton(ip) for ip in self.host_ips],
            port=self.port,
            properties=self._properties(),
            server=self.host_server,
        )

    def start(self) -> bool:
        """Register the service. Returns False (after a warning) without
        the zeroconf dependency or when registration fails."""
        if not HAS_ZEROCONF or _Zeroconf is None:
            print("mDNS: zeroconf not installed, skipping _steward._tcp.local "
                  "advertisement (pip install zeroconf to enable it)")
            return False
        try:
            with self._lock:
                self._zc = _Zeroconf()
                self._info = self._build_info()
                self._zc.register_service(self._info)
            return True
        except Exception as exc:  # noqa: BLE001 - advertise, never crash the daemon
            print(f"mDNS: advertisement failed: {exc}")
            try:
                self.stop()
            except Exception:
                pass
            return False

    def update(self) -> bool:
        """Refresh the TXT record (paired_count changes after every pairing)."""
        if self._zc is None or self._info is None:
            return False
        try:
            with self._lock:
                fresh = self._build_info()
                self._zc.update_service(fresh)
                self._info = fresh
            return True
        except Exception:  # noqa: BLE001 - best effort
            return False

    def stop(self) -> None:
        with self._lock:
            zc, info, self._zc, self._info = self._zc, self._info, None, None
        if zc is not None:
            try:
                if info is not None:
                    zc.unregister_service(info)
            except Exception:  # noqa: BLE001 - already gone is fine
                pass
            try:
                zc.close()
            except Exception:  # noqa: BLE001 - close is best effort
                pass
