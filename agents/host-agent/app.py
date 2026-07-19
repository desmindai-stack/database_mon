from __future__ import annotations

import os
import re
import subprocess
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic_settings import BaseSettings

UNIT_MAP = {
    "etcd": os.environ.get("UNIT_ETCD", "etcd"),
    "patroni": os.environ.get("UNIT_PATRONI", "patroni"),
    "postgresql": os.environ.get("UNIT_POSTGRESQL", "postgresql"),
    "keepalived": os.environ.get("UNIT_KEEPALIVED", "keepalived"),
    "haproxy": os.environ.get("UNIT_HAPROXY", "haproxy"),
}


class Settings(BaseSettings):
    agent_token: str = "change-me"
    keepalived_vip: str | None = None
    bind_host: str = "0.0.0.0"
    bind_port: int = 9105


settings = Settings()
app = FastAPI(title="pgwatch host-agent", version="1.0.0")


def _authorize(token: str | None) -> None:
    expected = settings.agent_token
    if not expected or expected == "change-me":
        # Still require header match when default is used in non-dev; allow empty only if explicitly empty.
        pass
    if token != expected:
        raise HTTPException(status_code=401, detail="Invalid agent token")


def _run(cmd: list[str], timeout: float = 5.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", "command not found"
    except Exception as exc:
        return 1, "", str(exc)


@app.get("/v1/health")
async def health(x_agent_token: str | None = Header(default=None)) -> dict[str, Any]:
    _authorize(x_agent_token)
    return {"status": "ok", "service": "pgwatch-host-agent"}


@app.get("/v1/services")
async def services(x_agent_token: str | None = Header(default=None)) -> dict[str, Any]:
    _authorize(x_agent_token)
    result: dict[str, Any] = {}
    for name, unit in UNIT_MAP.items():
        code, out, err = _run(["systemctl", "is-active", unit])
        active = out or "unknown"
        result[name] = {
            "unit": unit,
            "active": active,
            "ok": active == "active",
            "error": err if code not in (0, 3) else None,
        }
    return {"services": result}


@app.get("/v1/keepalived")
async def keepalived(x_agent_token: str | None = Header(default=None)) -> dict[str, Any]:
    _authorize(x_agent_token)
    vip = settings.keepalived_vip or os.environ.get("KEEPALIVED_VIP")
    code, out, err = _run(["ip", "-br", "addr"])
    addresses: list[str] = []
    holds_vip = False
    if code == 0 and out:
        for line in out.splitlines():
            # Example: eth0 UP 10.0.0.5/24 10.0.0.50/32
            parts = line.split()
            for token in parts[2:]:
                ip = token.split("/")[0]
                if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip) or ":" in ip:
                    addresses.append(ip)
                    if vip and ip == vip:
                        holds_vip = True
    pid_exists = os.path.exists("/var/run/keepalived.pid") or os.path.exists("/run/keepalived.pid")
    unit_code, unit_out, _ = _run(["systemctl", "is-active", UNIT_MAP["keepalived"]])
    return {
        "vip": vip,
        "holds_vip": holds_vip if vip else None,
        "addresses": addresses,
        "pid_exists": pid_exists,
        "systemd_active": unit_out or "unknown",
        "error": err if code != 0 else None,
    }


@app.get("/v1/logs")
async def logs(
    service: str = Query(default="patroni"),
    lines: int = Query(default=100, ge=10, le=500),
    x_agent_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _authorize(x_agent_token)
    unit = UNIT_MAP.get(service)
    if not unit:
        raise HTTPException(status_code=400, detail=f"Unknown service: {service}")
    code, out, err = _run(["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso"], timeout=8.0)
    log_lines = out.splitlines() if out else []
    return {
        "service": service,
        "unit": unit,
        "lines": log_lines,
        "error": err if code != 0 and not log_lines else None,
    }
