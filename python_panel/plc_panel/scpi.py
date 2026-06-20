"""Minimal SCPI-over-TCP helper used by scan-sequence ``scpi`` steps."""

from __future__ import annotations

import socket

_TIMEOUT_S = 3.0
_RECV_BYTES = 4096


def send_command(command: str, ip: str, port: int) -> str:
    """Send one SCPI command and return the reply for queries.

    A query (command containing ``?``) waits for a single response line;
    otherwise the call returns once the command has been written.

    Raises :class:`OSError` on any socket failure so the caller can react.
    """
    payload = (command.rstrip("\r\n") + "\n").encode("ascii")
    with socket.create_connection((ip, port), timeout=_TIMEOUT_S) as sock:
        sock.sendall(payload)
        if "?" not in command:
            return f"SCPI «{command}» отправлена на {ip}:{port}"
        reply = sock.recv(_RECV_BYTES).decode("ascii", errors="replace").strip()
        return f"SCPI ответ: {reply}"
