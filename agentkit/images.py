# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Validated image attachments and evidence artifacts; never logs image bytes."""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from .core import AKError, atomic_write

MIME_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 10 * 1024 * 1024
ESTIMATED_IMAGE_TOKENS = 1000


def _mime(path: Path) -> str:
    mime = MIME_TYPES.get(path.suffix.lower())
    if not mime:
        raise AKError("unsupported image format", f"{path.name}: use png, jpg, jpeg or webp",
                      "convert the image to a supported format", code="AK-USAGE")
    return mime


def _valid_magic(data: bytes, mime: str) -> bool:
    if mime == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def inspect(path: Path, *, source: str = "") -> dict:
    mime = _mime(path)
    size = path.stat().st_size
    if size <= 0 or size > MAX_IMAGE_BYTES:
        raise AKError("invalid image size", f"{path.name}: {size} bytes; maximum {MAX_IMAGE_BYTES}",
                      "shrink the image before attaching it", code="AK-USAGE")
    data = path.read_bytes()
    if not _valid_magic(data, mime):
        raise AKError("invalid image content", f"{path.name}: content does not match {mime}",
                      "use a real png/jpeg/webp file", code="AK-USAGE")
    return {"name": path.name, "source": source or str(path), "mime": mime, "bytes": size,
            "sha256": hashlib.sha256(data).hexdigest(), "estimated_tokens": ESTIMATED_IMAGE_TOKENS}


def inspect_attachments(root: Path, paths: list[str]) -> list[dict]:
    items = []
    total = 0
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = root / path
        path = path.resolve()
        try:
            source = path.relative_to(root.resolve()).as_posix()
        except ValueError as e:
            raise AKError("attachment outside repo", str(raw), "attach an image inside the repository", code="AK-PATH") from e
        if not path.is_file():
            raise AKError("attachment not found", str(raw), "check the image path", code="AK-PATH")
        item = inspect(path, source=source)
        total += item["bytes"]
        if total > MAX_TOTAL_BYTES:
            raise AKError("attachment total too large", f"maximum {MAX_TOTAL_BYTES} bytes per task",
                          "attach fewer or smaller images", code="AK-USAGE")
        items.append(item)
    return items


def stage(root: Path, items: list[dict], destination: Path) -> list[dict]:
    destination.mkdir(parents=True, exist_ok=True)
    staged = []
    for index, item in enumerate(items):
        name = f"{index:02d}-{item['name']}"
        path = destination / name
        data = (root / item["source"]).read_bytes()
        atomic_write(path, data, mode=0o600)
        staged.append({**item, "name": name, "path": path.relative_to(root).as_posix()})
    return staged


def image_bytes(root: Path, item: dict, *, base_dir: Path | None = None) -> bytes:
    path = root / item["path"]
    if base_dir is not None:
        try:
            path = path.resolve()
            path.relative_to(base_dir.resolve())
        except ValueError as e:
            raise AKError("image path outside allowed directory", item.get("path", "image"),
                          "restore the original image metadata", code="AK-PATH") from e
    data = path.read_bytes()
    if len(data) != item.get("bytes") or hashlib.sha256(data).hexdigest() != item.get("sha256"):
        raise AKError("attachment changed", item.get("path", "image"), "restore the original attachment", code="AK-CONFIG")
    return data


def data_url(root: Path, item: dict, *, base_dir: Path | None = None) -> str:
    return f"data:{item['mime']};base64," + base64.b64encode(image_bytes(root, item, base_dir=base_dir)).decode("ascii")


def scan_artifacts(worktree: Path) -> list[dict]:
    base = worktree / "artifacts"
    if not base.is_dir():
        return []
    items = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in MIME_TYPES:
            continue
        try:
            path.resolve().relative_to(worktree.resolve())
        except ValueError:
            continue
        try:
            item = inspect(path, source=path.relative_to(worktree).as_posix())
        except AKError:
            continue
        items.append({**item, "path": path.relative_to(worktree).as_posix()})
    return items
