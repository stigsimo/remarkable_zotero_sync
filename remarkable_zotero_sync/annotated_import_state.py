from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

STATE_VERSION = 1


@dataclass
class AnnotatedImportRecord:
    source_attachment_key: str
    parent_item_key: str
    latest_export_path: str
    latest_export_fingerprint: str
    latest_import_source_path: str
    latest_import_file_fingerprint: str
    latest_imported_attachment_key: str
    imported_attachment_keys: list[str]
    updated_at: str
    latest_remote_uuid: str | None = None
    latest_remote_last_modified: str | None = None


class AnnotatedImportStateStore:
    def __init__(self, path: Path):
        self.path = path
        self.records: dict[str, AnnotatedImportRecord] = {}

    def load(self) -> None:
        if not self.path.exists():
            self.records = {}
            return

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("version") != STATE_VERSION:
            raise ValueError(
                f"Unsupported annotated import state version in {self.path}: "
                f"{payload.get('version')}"
            )

        self.records = {
            key: AnnotatedImportRecord(**value)
            for key, value in payload.get("records", {}).items()
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
        self.path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def get(self, source_attachment_key: str) -> AnnotatedImportRecord | None:
        return self.records.get(source_attachment_key)

    def record_import(
        self,
        *,
        source_attachment_key: str,
        parent_item_key: str,
        export_path: Path,
        export_fingerprint: str,
        import_source_path: Path,
        import_file_fingerprint: str,
        imported_attachment_key: str,
        remote_uuid: str | None = None,
        remote_last_modified: str | None = None,
    ) -> None:
        existing = self.records.get(source_attachment_key)
        history = list(existing.imported_attachment_keys) if existing else []
        if imported_attachment_key not in history:
            history.append(imported_attachment_key)

        self.records[source_attachment_key] = AnnotatedImportRecord(
            source_attachment_key=source_attachment_key,
            parent_item_key=parent_item_key,
            latest_export_path=str(export_path),
            latest_export_fingerprint=export_fingerprint,
            latest_import_source_path=str(import_source_path),
            latest_import_file_fingerprint=import_file_fingerprint,
            latest_imported_attachment_key=imported_attachment_key,
            imported_attachment_keys=history,
            updated_at=datetime.now(timezone.utc).isoformat(),
            latest_remote_uuid=remote_uuid,
            latest_remote_last_modified=remote_last_modified,
        )

    def record_observation(
        self,
        *,
        source_attachment_key: str,
        parent_item_key: str,
        export_path: Path,
        export_fingerprint: str,
        remote_uuid: str | None = None,
        remote_last_modified: str | None = None,
        import_source_path: Path | None = None,
        import_file_fingerprint: str | None = None,
    ) -> None:
        existing = self.records.get(source_attachment_key)
        self.records[source_attachment_key] = AnnotatedImportRecord(
            source_attachment_key=source_attachment_key,
            parent_item_key=parent_item_key,
            latest_export_path=str(export_path),
            latest_export_fingerprint=export_fingerprint,
            latest_import_source_path=str(
                import_source_path
                if import_source_path is not None
                else (
                    existing.latest_import_source_path
                    if existing is not None
                    else ""
                )
            ),
            latest_import_file_fingerprint=(
                import_file_fingerprint
                if import_file_fingerprint is not None
                else (
                    existing.latest_import_file_fingerprint
                    if existing is not None
                    else ""
                )
            ),
            latest_imported_attachment_key=(
                existing.latest_imported_attachment_key
                if existing is not None
                else ""
            ),
            imported_attachment_keys=(
                list(existing.imported_attachment_keys)
                if existing is not None
                else []
            ),
            updated_at=datetime.now(timezone.utc).isoformat(),
            latest_remote_uuid=remote_uuid,
            latest_remote_last_modified=remote_last_modified,
        )
