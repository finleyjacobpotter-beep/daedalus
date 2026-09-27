"""Report basic host facts as JSON. Stdlib only so it runs on any target box."""

from __future__ import annotations

import platform
import socket
from pathlib import Path


def parse_os_release(text: str) -> dict[str, str]:
    """Parse /etc/os-release style KEY=value lines."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key] = value.strip().strip('"').strip("'")
    return out


def collect(os_release: Path = Path("/etc/os-release")) -> dict[str, str]:
    info = parse_os_release(os_release.read_text()) if os_release.exists() else {}
    return {
        "hostname": socket.gethostname(),
        "os": info.get("PRETTY_NAME", platform.system()),
        "os_id": info.get("ID", ""),
        "kernel": platform.release(),
        "python": platform.python_version(),
    }
