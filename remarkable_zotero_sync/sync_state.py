from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from remarkable_zotero_sync.models import UploadResult, ZoteroAttachment

STATE_VERSION = 1


@dataclass
class SyncRecord:
    attachment_key: str
    parent_item_key: str | None
    source_path: str
    visible_name: str
    tags: list[str]
    fingerprint: str
    remote_uuid: str
    remote_uuid_history: list[str]
    updated_at: str
    target_folder_path: str = "/"
    content_fingerprint: str | None = None


class SyncStateStore:
    def __init__(self, path: Path):
        self.path = path
        self.records: dict[str, SyncRecord] = {}

    def load(self) -> None:
        if not self.path.exists():
            self.records = {}
            return

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("version") != STATE_VERSION:
            raise ValueError(
                f"Unsupported sync state version in {self.path}: {payload.get('version')}"
            )

        raw_records = payload.get("records", {})
        self.records = {
            key: SyncRecord(**({"content_fingerprint": None} | value))
            for key, value in raw_records.items()
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "records": {
                key: asdict(record)
                for key, record in sorted(self.records.items())
            },
        }
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def get(self, attachment_key: str) -> SyncRecord | None:
        return self.records.get(attachment_key)

    def record_upload(
        self,
        *,
        attachment: ZoteroAttachment,
        fingerprint: str,
        content_fingerprint: str,
        upload_result: UploadResult,
        target_folder_path: str,
    ) -> None:
        existing = self.records.get(attachment.attachment_key)
        history = list(existing.remote_uuid_history) if existing else []
        if upload_result.remote_uuid not in history:
            history.append(upload_result.remote_uuid)

        self.records[attachment.attachment_key] = SyncRecord(
            attachment_key=attachment.attachment_key,
            parent_item_key=attachment.parent_item_key,
            source_path=str(attachment.pdf_path),
            visible_name=attachment.visible_name,
            tags=list(attachment.tags),
            fingerprint=fingerprint,
            content_fingerprint=content_fingerprint,
            remote_uuid=upload_result.remote_uuid,
            remote_uuid_history=history,
            target_folder_path=target_folder_path,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )


def compute_attachment_fingerprint(
    attachment: ZoteroAttachment,
    *,
    target_folder_path: str | None = None,
) -> str:
    digest = hashlib.sha256()
    if not attachment.pdf_path.exists():
        raise FileNotFoundError(f"Source PDF not found: {attachment.pdf_path}")

    with attachment.pdf_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)

    digest.update(attachment.visible_name.encode("utf-8"))
    digest.update(b"\0")
    digest.update(json.dumps(sorted(attachment.tags), ensure_ascii=True).encode("utf-8"))
    digest.update(b"\0")
    digest.update(attachment.source_kind.encode("utf-8"))
    digest.update(b"\0")
    digest.update(attachment.original_filename.encode("utf-8"))
    if target_folder_path is not None:
        digest.update(b"\0")
        digest.update(target_folder_path.encode("utf-8"))
    return digest.hexdigest()


def compute_attachment_content_fingerprint(
    attachment: ZoteroAttachment,
    *,
    target_folder_path: str | None = None,
) -> str:
    digest = hashlib.sha256()
    update_attachment_content_digest(
        digest,
        attachment,
        target_folder_path=target_folder_path,
    )
    return digest.hexdigest()


def update_attachment_content_digest(
    digest: hashlib._Hash,
    attachment: ZoteroAttachment,
    *,
    target_folder_path: str | None = None,
) -> None:
    if not attachment.pdf_path.exists():
        raise FileNotFoundError(f"Source PDF not found: {attachment.pdf_path}")

    with attachment.pdf_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)

    digest.update(attachment.visible_name.encode("utf-8"))
    digest.update(b"\0")
    digest.update(attachment.source_kind.encode("utf-8"))
    digest.update(b"\0")
    digest.update(attachment.original_filename.encode("utf-8"))
    if target_folder_path is not None:
        digest.update(b"\0")
        digest.update(target_folder_path.encode("utf-8"))
