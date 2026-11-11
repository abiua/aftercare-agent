"""Content-bound local BGE loading and portable float32 evidence."""

import hashlib
import json
import math
import os
import stat
import struct
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock

from aftercare.business.rules import digest

MODEL_ID = "BAAI/bge-small-zh-v1.5"
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
DIMENSIONS = 512
MAX_SEQUENCE_LENGTH = 512
BGE_FILES = (
    "1_Pooling/config.json",
    "config.json",
    "config_sentence_transformers.json",
    "model.safetensors",
    "modules.json",
    "sentence_bert_config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.txt",
)
MODULES = (
    ("", "sentence_transformers.models.Transformer"),
    ("1_Pooling", "sentence_transformers.models.Pooling"),
    # The pinned BGE artifact has a parameter-free, fileless normalization module.
    ("2_Normalize", "sentence_transformers.models.Normalize"),
)
ENCODING = {
    "backend": "sentence_transformers",
    "device": "cpu",
    "precision": "float32",
    "normalize_embeddings": True,
    "batch_size": 32,
    "max_sequence_length": MAX_SEQUENCE_LENGTH,
    "dimensions": DIMENSIONS,
    "prompt": "explicit_empty",
}
CHUNK_BYTES = 1024 * 1024
_LOAD_LOCK = Lock()


@contextmanager
def _file(root, relative):
    # Names are exclusively from BGE_FILES; don't traverse arbitrary paths or hub cache metadata.
    name = Path(relative)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(root, directory_flags)
    try:
        fd = (
            root_fd
            if name.parent == Path(".")
            else os.open(name.parent, directory_flags, dir_fd=root_fd)
        )
        try:
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            with os.fdopen(os.open(name.name, flags, dir_fd=fd), "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError("BGE_FILE_NOT_REGULAR")
                yield source
        finally:
            if fd != root_fd:
                os.close(fd)
    finally:
        os.close(root_fd)


def _stream_hash(source, target=None):
    sha = hashlib.sha256()
    for chunk in iter(lambda: source.read(CHUNK_BYTES), b""):
        sha.update(chunk)
        if target is not None:
            target.write(chunk)
    return sha.hexdigest()


def encoder_manifest(model_path):
    """Only manifest the supported runtime files, never cache metadata or local paths."""
    if model_path is None:
        raise ValueError("BGE_SNAPSHOT_REQUIRED")
    try:
        files = {}
        for relative in BGE_FILES:
            with _file(model_path, relative) as source:
                files[relative] = _stream_hash(source)
    except OSError:
        raise ValueError("BGE_SNAPSHOT_UNAVAILABLE") from None
    body = {"model_id": MODEL_ID, "files": files, "encoding": dict(ENCODING)}
    return {**body, "sha256": digest(body)}


@lru_cache(maxsize=2)
def _bound_model(model_path, file_hashes):
    from sentence_transformers import SentenceTransformer

    temporary = TemporaryDirectory(prefix="aftercare-bge-")
    root = Path(temporary.name)
    try:
        for relative, expected in file_hashes:
            target = root / relative
            target.parent.mkdir(mode=0o700, exist_ok=True)
            with _file(model_path, relative) as source, target.open("xb") as output:
                if _stream_hash(source, output) != expected:
                    raise ValueError("BGE_SNAPSHOT_CHANGED")
        modules = json.loads((root / "modules.json").read_text())
        if not isinstance(modules, list) or any(not isinstance(row, dict) for row in modules):
            raise ValueError("BGE_MODULE_PROFILE_UNSUPPORTED")
        if tuple((row.get("path"), row.get("type")) for row in modules) != MODULES:
            raise ValueError("BGE_MODULE_PROFILE_UNSUPPORTED")
        model = SentenceTransformer(
            str(root),
            device="cpu",
            local_files_only=True,
            trust_remote_code=False,
            model_kwargs={"use_safetensors": True},
        )
        model.max_seq_length = MAX_SEQUENCE_LENGTH
        return model, temporary
    except (OSError, ValueError, TypeError, RuntimeError, KeyError):
        temporary.cleanup()
        raise ValueError("BGE_BOUND_LOAD_FAILED") from None


def bound_encoder(model_path, manifest):
    file_hashes = tuple(sorted(manifest["files"].items()))
    if (
        tuple(name for name, _ in file_hashes) != BGE_FILES
        or manifest["model_id"] != MODEL_ID
        or manifest["encoding"] != ENCODING
        or manifest["sha256"] != digest({k: v for k, v in manifest.items() if k != "sha256"})
    ):
        raise ValueError("BGE_MANIFEST_INVALID")

    def encode(texts):
        with _LOAD_LOCK:
            model, _temporary = _bound_model(str(model_path), file_hashes)
        vectors = model.encode(
            texts,
            prompt="",
            batch_size=32,
            precision="float32",
            normalize_embeddings=True,
            show_progress_bar=False,
        ).tolist()
        if len(vectors) != len(texts) or any(
            len(vector) != DIMENSIONS
            or not all(math.isfinite(value) for value in vector)
            or not any(vector)
            for vector in vectors
        ):
            raise ValueError("BGE_BOUND_VECTOR_INVALID")
        return vectors

    return encode


def vector_digest(vector):
    return hashlib.sha256(struct.pack(f"!{DIMENSIONS}f", *vector)).hexdigest()


def vector_index_evidence(rows):
    """Hash actual stored vectors by key and encoder, independent of read order."""
    projected = sorted((key, encoder, vector_digest(vector)) for key, encoder, vector in rows)
    return {
        "keys": sorted({key for key, _, _ in projected}),
        "encoders": sorted({encoder for _, encoder, _ in projected}),
        "content_sha256": digest(projected),
    }
