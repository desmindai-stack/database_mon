"""Testlerde ağ gecikmesi/aksaması üreten gerçek TCP vekili (scripts/tcp_delay_proxy.py) — Faz 31 Commit 10a."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

PROXY_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "tcp_delay_proxy.py"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Proxy:
    """Ayrı süreçte çalışan gerçek TCP vekili (scripts/tcp_delay_proxy.py): sabit RTT ve/veya periyodik aksama."""

    def __init__(self, rtt_ms: float, target_port: int, *, stall_ms: float = 0, stall_every_s: float = 0) -> None:
        self.port = _free_port()
        self.process = subprocess.Popen(
            [sys.executable, str(PROXY_SCRIPT), "--rtt-ms", str(rtt_ms), "--stall-ms", str(stall_ms),
             "--stall-every-s", str(stall_every_s), f"{self.port}={target_port}"],
            stdout=subprocess.PIPE, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},  # Windows: konsol kod sayfasından bağımsız
        )
        assert self.process.stdout.readline().strip() == "hazır"

    def close(self) -> None:
        self.process.terminate()
        self.process.wait(timeout=10)
