from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any

from mcp.types import ImageContent


MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
ATTACHMENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
EXTENSIONS = {".png", ".apng", ".jpg", ".jpeg", ".webp", ".gif"}
MIME_ALIASES = {"image/jpg": "image/jpeg", "image/x-png": "image/png"}


class LoungeAttachmentError(ValueError):
    pass


def _png_info(data: bytes) -> tuple[str, int, int, int]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) < 33:
        raise LoungeAttachmentError("invalid PNG signature")
    if data[12:16] != b"IHDR":
        raise LoungeAttachmentError("invalid PNG header")
    width, height = struct.unpack(">II", data[16:24])
    offset, frames = 8, 1
    animated = False
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        if length > len(data) - offset - 12:
            raise LoungeAttachmentError("truncated PNG chunk")
        kind = data[offset + 4:offset + 8]
        if kind == b"acTL":
            if length != 8:
                raise LoungeAttachmentError("invalid APNG animation control")
            frames = max(1, struct.unpack(">I", data[offset + 8:offset + 12])[0])
            animated = frames > 1
        offset += length + 12
        if kind == b"IEND":
            break
    return ("image/apng" if animated else "image/png"), width, height, frames


def _gif_info(data: bytes) -> tuple[str, int, int, int]:
    if len(data) < 13 or data[:6] not in (b"GIF87a", b"GIF89a"):
        raise LoungeAttachmentError("invalid GIF signature")
    width, height = struct.unpack("<HH", data[6:10])
    frames = max(1, data.count(b"\x2c"))
    return "image/gif", width, height, frames


def _jpeg_info(data: bytes) -> tuple[str, int, int, int]:
    if len(data) < 4 or not data.startswith(b"\xff\xd8\xff"):
        raise LoungeAttachmentError("invalid JPEG signature")
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        offset += 2
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            break
        length = struct.unpack(">H", data[offset:offset + 2])[0]
        if length < 2 or offset + length > len(data):
            raise LoungeAttachmentError("truncated JPEG segment")
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            if length < 7:
                raise LoungeAttachmentError("invalid JPEG frame")
            height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
            return "image/jpeg", width, height, 1
        offset += length
    raise LoungeAttachmentError("JPEG dimensions not found")


def _webp_info(data: bytes) -> tuple[str, int, int, int]:
    if len(data) < 16 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise LoungeAttachmentError("invalid WebP signature")
    animated = b"ANIM" in data[12:]
    frames = max(1, data[12:].count(b"ANMF"))
    kind = data[12:16]
    if kind == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
    elif kind == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        width = struct.unpack("<H", data[26:28])[0] & 0x3FFF
        height = struct.unpack("<H", data[28:30])[0] & 0x3FFF
    elif kind == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
    else:
        raise LoungeAttachmentError("unsupported WebP header")
    return "image/webp", width, height, frames if animated else 1


def inspect_image(data: bytes) -> tuple[str, int, int, int]:
    if data.startswith(b"\x89PNG"):
        result = _png_info(data)
    elif data.startswith((b"GIF87a", b"GIF89a")):
        result = _gif_info(data)
    elif data.startswith(b"\xff\xd8\xff"):
        result = _jpeg_info(data)
    elif data.startswith(b"RIFF"):
        result = _webp_info(data)
    else:
        raise LoungeAttachmentError("unsupported or unsafe image signature")
    _, width, height, _ = result
    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
        raise LoungeAttachmentError("image dimensions exceed safety limits")
    return result


class LoungeAttachmentStore:
    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.root = (self.project_root / "attachments").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _safe_path(self, attachment_id: str, suffix: str) -> Path:
        if not ATTACHMENT_ID_RE.fullmatch(attachment_id) or suffix not in {
            ".png", ".apng", ".jpg", ".webp", ".gif", ".first.png", ".contact.jpg", ".json"
        }:
            raise LoungeAttachmentError("invalid attachment identifier")
        path = (self.root / f"{attachment_id}{suffix}").resolve()
        if path.parent != self.root:
            raise LoungeAttachmentError("attachment path escaped storage root")
        return path

    def save(self, original_name: str, claimed_mime: str, data: bytes) -> dict[str, Any]:
        if not data or len(data) > MAX_ATTACHMENT_BYTES:
            raise LoungeAttachmentError(f"image must be between 1 byte and {MAX_ATTACHMENT_BYTES} bytes")
        suffix = Path(original_name or "").suffix.lower()
        if suffix not in EXTENSIONS:
            raise LoungeAttachmentError("allowed extensions: PNG/JPG/JPEG/WebP/GIF/APNG")
        claimed = MIME_ALIASES.get((claimed_mime or "").split(";", 1)[0].strip().lower(), (claimed_mime or "").split(";", 1)[0].strip().lower())
        actual_mime, width, height, frames = inspect_image(data)
        allowed_claims = {actual_mime}
        if actual_mime == "image/apng":
            allowed_claims.add("image/png")
        if claimed not in allowed_claims:
            raise LoungeAttachmentError("declared MIME does not match image content")
        if actual_mime == "image/jpeg" and suffix not in {".jpg", ".jpeg"}:
            raise LoungeAttachmentError("file extension does not match JPEG content")
        if actual_mime in {"image/png", "image/apng"} and suffix not in {".png", ".apng"}:
            raise LoungeAttachmentError("file extension does not match PNG/APNG content")
        if actual_mime == "image/gif" and suffix != ".gif":
            raise LoungeAttachmentError("file extension does not match GIF content")
        if actual_mime == "image/webp" and suffix != ".webp":
            raise LoungeAttachmentError("file extension does not match WebP content")
        canonical = {"image/jpeg": ".jpg", "image/png": ".png", "image/apng": ".apng", "image/gif": ".gif", "image/webp": ".webp"}[actual_mime]
        attachment_id = secrets.token_hex(16)
        path = self._safe_path(attachment_id, canonical)
        temp = path.with_suffix(path.suffix + f".{secrets.token_hex(4)}.tmp")
        try:
            with temp.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        try:
            width, height, frames = self._probe_image(path, width, height, frames)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        animated = frames > 1
        metadata = {"id": attachment_id, "mime": actual_mime, "width": width, "height": height, "size": len(data), "animated": animated, "frame_count": frames}
        try:
            if animated:
                self._make_contact_sheet(path, attachment_id, frames)
            if actual_mime == "image/apng":
                self._make_first_frame(path, attachment_id)
            sidecar = self._safe_path(attachment_id, ".json")
            sidecar.write_text(json.dumps(metadata, separators=(",", ":")), encoding="utf-8")
        except Exception:
            path.unlink(missing_ok=True)
            self._safe_path(attachment_id, ".first.png").unlink(missing_ok=True)
            self._safe_path(attachment_id, ".contact.jpg").unlink(missing_ok=True)
            self._safe_path(attachment_id, ".json").unlink(missing_ok=True)
            raise
        return metadata

    @staticmethod
    def _probe_image(path: Path, width: int, height: int, frames: int) -> tuple[int, int, int]:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return width, height, frames
        result = subprocess.run(
            [ffprobe, "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries", "stream=width,height,nb_read_frames", "-of", "json", str(path)],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise LoungeAttachmentError("image decoder rejected the file")
        try:
            stream = json.loads(result.stdout.decode("utf-8"))["streams"][0]
            probed_width = int(stream["width"])
            probed_height = int(stream["height"])
            probed_frames = int(stream.get("nb_read_frames") or frames)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LoungeAttachmentError("image stream metadata is invalid") from exc
        if probed_width != width or probed_height != height or probed_frames < 1:
            raise LoungeAttachmentError("image header and decoded stream do not agree")
        return probed_width, probed_height, probed_frames

    def _make_contact_sheet(self, source: Path, attachment_id: str, frames: int) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise LoungeAttachmentError("animated images require ffmpeg for model preview")
        samples = sorted({0, max(0, frames // 5), max(0, frames * 2 // 5), max(0, frames * 3 // 5), max(0, frames * 4 // 5), max(0, frames - 1)})
        select = "+".join(f"eq(n\\,{index})" for index in samples)
        target = self._safe_path(attachment_id, ".contact.jpg")
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-vf", f"select='{select}',scale=280:280:force_original_aspect_ratio=decrease,pad=280:280:(ow-iw)/2:(oh-ih)/2:color=black,tile=3x2", "-frames:v", "1", str(target)]
        result = subprocess.run(command, capture_output=True, timeout=30, check=False)
        if result.returncode != 0 or not target.exists():
            target.unlink(missing_ok=True)
            raise LoungeAttachmentError("could not generate animation contact sheet")

    def _make_first_frame(self, source: Path, attachment_id: str) -> Path:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise LoungeAttachmentError("APNG model preview requires ffmpeg")
        target = self._safe_path(attachment_id, ".first.png")
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-frames:v", "1", str(target)],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0 or not target.exists():
            target.unlink(missing_ok=True)
            raise LoungeAttachmentError("could not generate APNG first-frame preview")
        return target

    def resolve(self, metadata: dict[str, Any]) -> Path:
        attachment_id = str(metadata.get("id", ""))
        mime = str(metadata.get("mime", ""))
        suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/apng": ".apng", "image/gif": ".gif", "image/webp": ".webp"}.get(mime)
        if suffix is None:
            raise LoungeAttachmentError("unsupported attachment metadata")
        path = self._safe_path(attachment_id, suffix)
        if not path.is_file():
            raise LoungeAttachmentError("attachment not found")
        return path

    def get_metadata(self, attachment_id: str) -> dict[str, Any]:
        sidecar = self._safe_path(attachment_id, ".json")
        try:
            metadata = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception as exc:
            raise LoungeAttachmentError("attachment metadata not found") from exc
        self.resolve(metadata)
        return metadata

    def find_posted(self, attachment_id: str) -> dict[str, Any]:
        if not ATTACHMENT_ID_RE.fullmatch(attachment_id or ""):
            raise LoungeAttachmentError("invalid attachment identifier")
        messages = self.project_root / ".lounge" / "messages.jsonl"
        if messages.exists():
            for line in reversed(messages.read_text(encoding="utf-8", errors="ignore").splitlines()):
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                for item in row.get("attachments", []) if isinstance(row, dict) else []:
                    if isinstance(item, dict) and item.get("id") == attachment_id:
                        return item
        raise LoungeAttachmentError("attachment is not referenced by a lounge message")

    def image_content(self, attachment_id: str) -> list[ImageContent]:
        metadata = self.find_posted(attachment_id)
        source = self.resolve(metadata)
        primary = source
        primary_mime = metadata["mime"]
        if primary_mime == "image/apng":
            primary = self._safe_path(attachment_id, ".first.png")
            if not primary.is_file():
                primary = self._make_first_frame(source, attachment_id)
            primary_mime = "image/png"
        contents = [ImageContent(type="image", data=base64.b64encode(primary.read_bytes()).decode("ascii"), mimeType=primary_mime)]
        contact = self._safe_path(attachment_id, ".contact.jpg")
        if metadata.get("animated") and contact.is_file():
            contents.append(ImageContent(type="image", data=base64.b64encode(contact.read_bytes()).decode("ascii"), mimeType="image/jpeg"))
        return contents

