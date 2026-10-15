"""Atomic local files and immutable, content-addressed library objects."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Callable
import uuid

from astro_pipeline.library.identity import canonical_json


class BlobIntegrityError(RuntimeError):
    pass


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fsync_directory(path: Path | str) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_file(path: Path | str, writer: Callable[[Path], None]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        writer(temporary)
        # Windows' fsync implementation requires a writable file descriptor.
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
        return path
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path | str, value) -> Path:
    payload = (canonical_json(value) + "\n").encode("utf-8")
    return atomic_bytes(path, payload)


def atomic_bytes(path: Path | str, payload: bytes) -> Path:
    return atomic_file(path, lambda temporary: temporary.write_bytes(payload))


def atomic_copy(source: Path | str, destination: Path | str) -> Path:
    return atomic_file(destination, lambda temporary: shutil.copyfile(source, temporary))


@dataclass(frozen=True)
class Blob:
    sha256: str
    relative_path: str
    size_bytes: int
    content_type: str
    extension: str

    def to_dict(self) -> dict:
        return asdict(self)


class BlobStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.objects = self.root / "objects" / "sha256"
        self.staging = self.root / "staging"

    def path(self, blob: Blob | dict) -> Path:
        relative = blob.relative_path if isinstance(blob, Blob) else blob["relative_path"]
        path = self.root / relative
        if not path.resolve().is_relative_to(self.objects.resolve()):
            raise BlobIntegrityError("Library object path escapes the object store")
        return path

    def verify(self, blob: Blob | dict) -> Path:
        record = blob.to_dict() if isinstance(blob, Blob) else blob
        path = self.path(record)
        if not path.is_file() or path.stat().st_size != record["size_bytes"]:
            raise BlobIntegrityError(f"Library object missing or truncated: {record['sha256']}")
        if sha256_file(path) != record["sha256"]:
            raise BlobIntegrityError(f"Library object checksum mismatch: {record['sha256']}")
        return path

    def publish(
        self, source: Path | str, *, extension: str | None = None,
        content_type: str = "application/octet-stream", repair: bool = False,
    ) -> Blob:
        source = Path(source)
        extension = (extension or source.suffix or "bin").lower().lstrip(".")
        if not re.fullmatch(r"[a-z0-9]+(?:\.[a-z0-9]+)*", extension):
            raise ValueError("Invalid object extension")
        self.staging.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix="publish-", suffix=".part", dir=self.staging)
        os.close(descriptor)
        temporary = Path(name)
        try:
            shutil.copyfile(source, temporary)
            with temporary.open("r+b") as stream:
                os.fsync(stream.fileno())
            digest = sha256_file(temporary)
            destination = self.objects / digest[:2] / f"{digest}.{extension}"
            blob = Blob(
                sha256=digest, relative_path=str(destination.relative_to(self.root)),
                size_bytes=temporary.stat().st_size, content_type=content_type,
                extension=extension,
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                try:
                    self.verify(blob)
                    return blob
                except BlobIntegrityError:
                    if not repair:
                        raise
                    quarantine = self.staging / f"damaged-{digest}-{uuid.uuid4().hex}"
                    shutil.copyfile(destination, quarantine)
            os.chmod(temporary, 0o444)
            os.replace(temporary, destination)
            directory = destination.parent
            while directory.is_relative_to(self.root):
                fsync_directory(directory)
                directory = directory.parent
            fsync_directory(self.root.parent)
            self.verify(blob)
            return blob
        finally:
            temporary.unlink(missing_ok=True)

    def export(self, blob: Blob | dict, destination: Path | str) -> Path:
        source = self.verify(blob)
        destination = Path(destination)
        if source.resolve() == destination.resolve():
            raise ValueError("Cannot export over a shared library object")
        return atomic_copy(source, destination)
