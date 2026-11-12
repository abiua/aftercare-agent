"""Deterministic image bytes for evaluation runner fixture operations."""

import hashlib
import struct
import zlib


def material_bytes(case_id: str, asset_id: str) -> bytes:
    """Deterministic, decodable fixture image. It carries no visual labels or real photos."""
    if asset_id == "invalid-bytes":
        return b"synthetic malformed image"
    color = hashlib.sha256(f"{case_id}/{asset_id}".encode()).digest()[:3]

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    pixels = b"".join(b"\0" + color * 8 for _ in range(8))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">2I5B", 8, 8, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )
