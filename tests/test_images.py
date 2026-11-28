# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Image attachment validation, staging and tamper detection."""
import base64

import pytest

from agentkit import images
from agentkit.core import AKError

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6nWQAAAAASUVORK5CYII=")


def test_image_attachment_stage_and_data_url(tmp_path):
    source = tmp_path / "reference.png"
    source.write_bytes(PNG)
    inspected = images.inspect_attachments(tmp_path, ["reference.png"])
    destination = tmp_path / ".agentkit" / "tasks" / "t123abc" / "attachments"
    staged = images.stage(tmp_path, inspected, destination)
    assert staged[0]["path"].startswith(".agentkit/tasks/t123abc/attachments/")
    assert staged[0]["mime"] == "image/png" and staged[0]["estimated_tokens"] > 0
    url = images.data_url(tmp_path, staged[0], base_dir=destination)
    assert url.startswith("data:image/png;base64,")
    assert "data:image" not in str(staged)
    (destination / staged[0]["name"]).write_bytes(b"tampered")
    with pytest.raises(AKError, match="attachment changed"):
        images.data_url(tmp_path, staged[0], base_dir=destination)


def test_image_attachment_rejects_bad_format_and_escape(tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_text("not an image", encoding="utf-8")
    with pytest.raises(AKError, match="invalid image content"):
        images.inspect_attachments(tmp_path, ["bad.png"])
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(PNG)
    with pytest.raises(AKError, match="outside repo"):
        images.inspect_attachments(tmp_path, [str(outside)])


def test_image_data_url_rejects_metadata_path_escape(tmp_path):
    source = tmp_path / "reference.png"
    source.write_bytes(PNG)
    item = images.inspect_attachments(tmp_path, ["reference.png"])[0]
    item["path"] = "reference.png"
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    with pytest.raises(AKError, match="outside allowed directory"):
        images.data_url(tmp_path, item, base_dir=allowed)
