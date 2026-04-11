from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ZoteroAttachment:
    attachment_item_id: int
    attachment_key: str
    selected_attachment_key: str
    parent_item_id: int | None
    parent_item_key: str | None
    parent_title: str
    attachment_title: str | None
    visible_name: str
    pdf_path: Path
    original_filename: str
    tags: list[str]
    source_kind: str
    collection_path_parts: tuple[str, ...]
    date_added: str = ""
    date_modified: str = ""
    routing_error: str | None = None


@dataclass(frozen=True)
class RemarkableItem:
    uuid: str
    visible_name: str
    item_type: str
    parent: str
    deleted: bool
    raw: dict[str, Any]


@dataclass(frozen=True)
class UploadRequest:
    source_path: Path
    visible_name: str
    parent_uuid: str
    tags: list[str]


@dataclass(frozen=True)
class UploadResult:
    remote_uuid: str
    visible_name: str
    source_path: Path
    tags: list[str]


@dataclass(frozen=True)
class SyncDecision:
    attachment: ZoteroAttachment
    target_folder_path: str
    fingerprint: str | None
    status: str
    reason: str
    previous_remote_uuid: str | None = None


@dataclass
class SyncReport:
    collection_name: str
    dry_run: bool
    decisions: list[SyncDecision] = field(default_factory=list)
    uploaded: list[UploadResult] = field(default_factory=list)
    cleanup_actions: list[str] = field(default_factory=list)
    cleanup_preserved: list[str] = field(default_factory=list)
    metadata_updates: list[str] = field(default_factory=list)
    zotero_tag_updates: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    target_parent: str | None = None
    zotero_db_backup_path: Path | None = None
    xochitl_restarted: bool = False


@dataclass(frozen=True)
class HighlightRepairResult:
    input_path: Path
    output_path: Path | None
    status: str
    patched_extgstate_count: int
    pages_with_highlights: int
    parsed_highlight_rect_count: int
    added_highlight_annotation_count: int


@dataclass(frozen=True)
class ImportedAttachmentResult:
    source_attachment_key: str
    parent_item_key: str
    imported_attachment_key: str
    attachment_title: str
    source_path: Path
    zotero_storage_path: Path


@dataclass
class AnnotatedImportDecision:
    export_path: Path
    status: str
    reason: str
    source_attachment_key: str | None = None
    source_visible_name: str | None = None
    parent_item_key: str | None = None
    repaired_output_path: Path | None = None
    import_source_path: Path | None = None
    imported_attachment_key: str | None = None


@dataclass
class AnnotatedImportReport:
    dry_run: bool
    decisions: list[AnnotatedImportDecision] = field(default_factory=list)
    imported: list[ImportedAttachmentResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    db_backup_path: Path | None = None


@dataclass
class ReturnSyncDecision:
    source_attachment_key: str
    source_visible_name: str
    remote_uuid: str | None
    status: str
    reason: str
    remote_last_modified: str | None = None
    export_path: Path | None = None
    repaired_output_path: Path | None = None
    imported_attachment_key: str | None = None


@dataclass
class ReturnSyncReport:
    dry_run: bool
    decisions: list[ReturnSyncDecision] = field(default_factory=list)
    imported: list[ImportedAttachmentResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    db_backup_path: Path | None = None


@dataclass
class RoundTripSyncReport:
    dry_run: bool
    forward: SyncReport
    reverse: ReturnSyncReport
