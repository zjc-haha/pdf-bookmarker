"""Keep the DeepSeek API key encrypted for the current Windows account.

The cache file contains only a Windows DPAPI blob. DPAPI uses the current
user's credentials; a key saved by another Windows account cannot be read.
"""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path

from .storage import data_root


class KeyCacheError(RuntimeError):
    """The cached key could not be encrypted or decrypted."""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


_ENTROPY = b"PDFBookmarker.DeepSeek.ApiKey.v1"
_UI_FORBIDDEN = 0x01


def _cache_path() -> Path:
    return data_root() / "deepseek-api-key.dpapi"


def _legacy_cache_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "PDFBookmarker" / "deepseek-api-key.dpapi"


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    # Keep the backing buffer alive until the Windows call returns.
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _windows_api():
    if sys.platform != "win32":
        raise KeyCacheError("Windows DPAPI is required to cache the DeepSeek API key")

    crypt32 = ctypes.WinDLL("Crypt32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.c_wchar_p, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _transform(data: bytes, *, decrypt: bool) -> bytes:
    crypt32, kernel32 = _windows_api()
    input_blob, input_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(_ENTROPY)
    output_blob = _DataBlob()
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    try:
        succeeded = operation(
            ctypes.byref(input_blob), None, ctypes.byref(entropy_blob),
            None, None, _UI_FORBIDDEN, ctypes.byref(output_blob),
        )
        if not succeeded:
            action = "decrypt" if decrypt else "encrypt"
            raise KeyCacheError(
                f"Windows could not {action} the cached DeepSeek API key "
                f"(error {ctypes.get_last_error()})"
            )
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if not decrypt and input_blob.cbData:
            ctypes.memset(input_blob.pbData, 0, input_blob.cbData)
        # The decrypt result is plaintext in a buffer allocated by Windows.
        if output_blob.pbData:
            if decrypt and output_blob.cbData:
                ctypes.memset(output_blob.pbData, 0, output_blob.cbData)
            kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))
        # Keep both input buffers alive for the duration of the call.
        del input_buffer, entropy_buffer


def _protect(data: bytes) -> bytes:
    return _transform(data, decrypt=False)


def _unprotect(data: bytes) -> bytes:
    return _transform(data, decrypt=True)


def _decoded_key(encrypted: bytes) -> str:
    if not encrypted:
        raise KeyCacheError("The cached DeepSeek API key is empty or damaged")
    try:
        return _unprotect(encrypted).decode("utf-8")
    except UnicodeDecodeError as error:
        raise KeyCacheError("The cached DeepSeek API key is damaged") from error


def _remove_legacy_cache() -> None:
    legacy = _legacy_cache_path()
    try:
        legacy.unlink(missing_ok=True)
    except OSError:
        # Once the installation-local copy is available, failure to tidy up
        # an old profile copy must not make that valid key appear missing.
        return
    try:
        legacy.parent.rmdir()
    except OSError:
        # Other application data in this folder must be preserved.
        pass


def _write_encrypted(path: Path, encrypted: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=".deepseek-api-key-",
            suffix=".tmp", delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(encrypted)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def load_key() -> str:
    """Return the cached key, migrating a valid legacy DPAPI blob if needed."""
    path = _cache_path()
    try:
        encrypted = path.read_bytes()
    except FileNotFoundError:
        try:
            encrypted = _legacy_cache_path().read_bytes()
        except FileNotFoundError:
            return ""
        key = _decoded_key(encrypted)
        _write_encrypted(path, encrypted)
        _remove_legacy_cache()
        return key

    key = _decoded_key(encrypted)
    # A previous migration may have written the new cache but been interrupted
    # before removing its old copy.
    _remove_legacy_cache()
    return key


def save_key(key: str) -> None:
    """Encrypt and atomically save a nonblank key beside the application.

    An empty GUI field can arise during editing or window shutdown.  It must
    never erase the saved key; removing the cache file is an explicit manual
    action by the user.
    """
    value = key.strip()
    if not value:
        return

    path = _cache_path()
    encrypted = _protect(value.encode("utf-8"))
    _write_encrypted(path, encrypted)
    _remove_legacy_cache()
