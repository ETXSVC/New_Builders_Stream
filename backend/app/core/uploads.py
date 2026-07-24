"""Byte-capped multipart reads.

Every upload route previously did a bare `await file.read()` — an
unbounded read into memory, and from there onto the documents volume: a
single oversized request was both a memory-exhaustion and a disk-fill
vector. This helper is the shared fix: reject early on a declared
Content-Length when the client sends one, then enforce the cap for real
during a chunked read (a lying or absent Content-Length never wins —
the read aborts the moment the running total crosses the limit, holding
at most limit + one chunk in memory).

Limits live on Settings (max_document_upload_bytes /
max_signature_upload_bytes) rather than as constants here, so tests can
shrink them per-route instead of shipping multi-megabyte payloads
through CI. The branding logo keeps its own pre-existing 2 MiB path in
document_storage.py.

The outer belt is the reverse proxy's request_body max_size (see
deploy/Caddyfile) — this helper is the application-layer guarantee that
holds even for traffic that never crossed the proxy.
"""
from fastapi import HTTPException, UploadFile, status

_CHUNK_SIZE = 1024 * 1024


def _too_large(max_bytes: int) -> HTTPException:
    return HTTPException(
        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        f"Uploaded file exceeds the {max_bytes} byte limit",
    )


async def read_upload_limited(file: UploadFile, max_bytes: int) -> bytes:
    if file.size is not None and file.size > max_bytes:
        raise _too_large(max_bytes)

    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(_CHUNK_SIZE):
        total += len(chunk)
        if total > max_bytes:
            raise _too_large(max_bytes)
        chunks.append(chunk)
    return b"".join(chunks)
