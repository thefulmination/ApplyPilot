"""Windows-user-bound DPAPI vault for ATS tenant passwords."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import secrets
import string
import sys

from applypilot import config


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes):
    buffer = ctypes.create_string_buffer(data)
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def protect(data: bytes) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("DPAPI credential vault requires Windows")
    source, keepalive = _blob(data)
    output = _Blob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source), "ApplyPilot tenant credential", None, None, None, 0,
        ctypes.byref(output),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


def unprotect(data: bytes) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("DPAPI credential vault requires Windows")
    source, keepalive = _blob(data)
    output = _Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


def vault_path() -> Path:
    return Path(config.APP_DIR) / "tenant_credentials.dpapi.json"


def _load(path: Path) -> dict:
    if not path.is_file():
        return {"version": 1, "credentials": {}}
    data = json.loads(path.read_text(encoding="ascii"))
    if data.get("version") != 1 or not isinstance(data.get("credentials"), dict):
        raise ValueError("invalid tenant credential vault")
    return data


def _generate_password() -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%&*+-_"
    return (
        secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.choice("!@#$%&*+-_")
        + "".join(secrets.choice(alphabet) for _ in range(20))
    )


def get_or_create(host: str, *, path: Path | None = None,
                  protect_fn=protect, unprotect_fn=unprotect) -> str:
    path = path or vault_path()
    data = _load(path)
    encoded = data["credentials"].get(host)
    if encoded:
        return unprotect_fn(base64.b64decode(encoded)).decode("utf-8")
    password = _generate_password()
    data["credentials"][host] = base64.b64encode(protect_fn(password.encode("utf-8"))).decode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="ascii")
    temporary.replace(path)
    return password
