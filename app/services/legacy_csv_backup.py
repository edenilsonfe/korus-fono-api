"""User-bound encrypted baseline, without plaintext clinical backup files (Windows)."""

import ctypes
import json
import os
from pathlib import Path

from app.services.legacy_csv_import import (
    TargetSnapshot,
    json_value,
    save_json,
)
from app.services.legacy_csv_source import CsvImportError, digest


def _dpapi(payload: bytes, *, decrypt=False) -> bytes:
    if os.name != "nt":
        raise CsvImportError(
            "Configure um destino de backup criptografado; o adaptador padrão requer Windows DPAPI"
        )
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]

    buffer = ctypes.create_string_buffer(payload)
    source = Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    result = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        success = crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)
        )
    else:
        success = crypt32.CryptProtectData(
            ctypes.byref(source),
            "KorusFono migration baseline",
            None,
            None,
            None,
            1,
            ctypes.byref(result),
        )
    if not success:
        raise CsvImportError("Falha ao proteger ou ler o backup DPAPI")
    try:
        return ctypes.string_at(result.data, result.length)
    finally:
        kernel32.LocalFree(result.data)


class ProtectedBackup:
    def __init__(self, directory: Path):
        self.directory = directory

    def __call__(self, target: TargetSnapshot, preview_sha256: str) -> None:
        data = {
            "professional": target.professional,
            "rows": target.rows,
            "baselineSha256": target.sha256,
            "previewSha256": preview_sha256,
        }
        payload = json.dumps(
            json_value(data), ensure_ascii=False, sort_keys=True
        ).encode()
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{preview_sha256}.baseline.dpapi"
        if path.exists():
            if _dpapi(path.read_bytes(), decrypt=True) != payload:
                raise CsvImportError("Backup existente não corresponde ao estado atual")
        else:
            encrypted = _dpapi(payload)
            if _dpapi(encrypted, decrypt=True) != payload:
                raise CsvImportError("A verificação do backup criptografado falhou")
            with path.open("xb") as stream:
                stream.write(encrypted)
        save_json(
            path.with_suffix(".json"),
            {
                "previewSha256": preview_sha256,
                "baselineSha256": target.sha256,
                "encryption": "Windows DPAPI, current user",
                "encryptedSha256": digest(path.read_bytes()),
                "counts": {k: len(v) for k, v in target.rows.items()},
            },
        )
