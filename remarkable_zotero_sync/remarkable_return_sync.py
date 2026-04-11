from __future__ import annotations

from collections.abc import Callable
import re
import zipfile
from pathlib import Path

from remarkable_zotero_sync.annotated_import import (
    compute_file_fingerprint,
    matching_sync_records_for_visible_name,
    normalize_match_text,
    prepare_import_source,
)
from remarkable_zotero_sync.annotated_import_state import AnnotatedImportStateStore
from remarkable_zotero_sync.config import AppConfig
from remarkable_zotero_sync.models import ReturnSyncDecision, ReturnSyncReport
from remarkable_zotero_sync.remarkable_client import RemarkableClient
from remarkable_zotero_sync.sync_state import SyncRecord, SyncStateStore
from remarkable_zotero_sync.zotero_importer import ZoteroImporter


def sanitize_filename_component(value: str) -> str:
    cleaned = re.sub(r"[^\w.\- ]+", "_", value, flags=re.UNICODE).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "document"


class RemarkableReturnSyncService:
    def __init__(
        self,
        config: AppConfig,
        *,
        progress: Callable[[str], None] | None = None,
    ):
        self.config = config
        self.progress = progress
        self.sync_state = SyncStateStore(config.sync.state_path)
        self.sync_state.load()
        self.import_state = AnnotatedImportStateStore(config.annotated_import.state_path)
        self.import_state.load()
        self.client = RemarkableClient(config.remarkable)

    def emit_progress(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)

    def matching_duplicate_records(self, visible_name: str) -> list[SyncRecord]:
        matches = matching_sync_records_for_visible_name(self.sync_state, visible_name)
        if matches:
            return matches
        return sorted(
            [
                record
                for record in self.sync_state.records.values()
                if record.visible_name == visible_name
            ],
            key=lambda record: (record.updated_at, record.attachment_key),
        )

    def matching_duplicate_attachment_paths(
        self,
        visible_name: str,
        importer: ZoteroImporter,
    ) -> dict[str, Path]:
        matches = importer.matching_pdf_attachment_paths_for_parent_title(visible_name)
        for record in self.matching_duplicate_records(visible_name):
            matches.setdefault(record.attachment_key, Path(record.source_path))
        return matches

    @staticmethod
    def decision_group_key(decision: ReturnSyncDecision) -> str:
        return normalize_match_text(decision.source_visible_name)

    @staticmethod
    def decision_recency_key(decision: ReturnSyncDecision) -> tuple[int, str]:
        return (
            int(decision.remote_last_modified or "0"),
            decision.remote_uuid or "",
        )

    @staticmethod
    def local_paths_match_fingerprint(
        paths: list[Path],
        expected_fingerprint: str,
    ) -> bool:
        if not paths:
            return False
        for path in paths:
            if not path.exists():
                return False
            if compute_file_fingerprint(path) != expected_fingerprint:
                return False
        return True

    def build_report(self, *, dry_run: bool) -> ReturnSyncReport:
        report = ReturnSyncReport(dry_run=dry_run)
        if not self.sync_state.records:
            return report
        importer = ZoteroImporter(self.config.zotero, self.config.annotated_import)

        self.emit_progress("Pulling reMarkable metadata for return sync...")
        try:
            self.client.pull_metadata()
            items = self.client.fetch_metadata_items_local()
        except Exception as exc:
            report.errors.append(
                "Could not inspect reMarkable highlights for return sync: "
                f"{exc}"
            )
            return report

        try:
            self.emit_progress("Inspecting highlighted reMarkable documents...")
            highlighted_ids = self.client.list_document_ids_with_highlights()
        except Exception as exc:
            report.errors.append(
                "Could not inspect highlighted reMarkable documents for return sync; "
                "skipping automatic annotated-PDF import to avoid guessing. "
                f"{exc}"
            )
            return report

        for record in sorted(
            self.sync_state.records.values(),
            key=lambda item: item.visible_name.lower(),
        ):
            candidate = self._select_remote_candidate(record, items, highlighted_ids)
            if candidate is None:
                report.decisions.append(
                    ReturnSyncDecision(
                        source_attachment_key=record.attachment_key,
                        source_visible_name=record.visible_name,
                        remote_uuid=None,
                        status="no_highlights",
                        reason="no tracked highlighted reMarkable copy was found for this Zotero PDF",
                    )
                )
                continue

            remote_uuid, remote_last_modified = candidate
            existing_import = self.import_state.get(record.attachment_key)
            if (
                existing_import is not None
                and self._state_has_valid_import_artifact(existing_import.latest_import_source_path)
                and existing_import.latest_remote_uuid == remote_uuid
                and existing_import.latest_remote_last_modified == remote_last_modified
            ):
                duplicate_attachment_paths = self.matching_duplicate_attachment_paths(
                    record.visible_name,
                    importer,
                )
                latest_import_fingerprint = compute_file_fingerprint(
                    Path(existing_import.latest_import_source_path)
                )
                if not self.local_paths_match_fingerprint(
                    list(duplicate_attachment_paths.values()),
                    latest_import_fingerprint,
                ):
                    report.decisions.append(
                        ReturnSyncDecision(
                            source_attachment_key=record.attachment_key,
                            source_visible_name=record.visible_name,
                            remote_uuid=remote_uuid,
                            remote_last_modified=remote_last_modified,
                            status="ready",
                            reason=(
                                "matching Zotero duplicates do not yet match the latest "
                                "annotated version, so the newest reMarkable PDF will be "
                                "reapplied"
                            ),
                        )
                    )
                    continue
                report.decisions.append(
                    ReturnSyncDecision(
                        source_attachment_key=record.attachment_key,
                        source_visible_name=record.visible_name,
                        remote_uuid=remote_uuid,
                        remote_last_modified=remote_last_modified,
                        status="unchanged",
                        reason="reMarkable metadata lastModified matches the last imported annotated version",
                    )
                )
                continue

            report.decisions.append(
                ReturnSyncDecision(
                    source_attachment_key=record.attachment_key,
                    source_visible_name=record.visible_name,
                    remote_uuid=remote_uuid,
                    remote_last_modified=remote_last_modified,
                    status="ready",
                    reason="tracked reMarkable PDF has highlights and changed since the last Zotero import",
                )
            )

        return report

    def execute(self) -> ReturnSyncReport:
        report = self.build_report(dry_run=False)
        actionable = [decision for decision in report.decisions if decision.status == "ready"]
        grouped_actionable: dict[str, ReturnSyncDecision] = {}
        for decision in actionable:
            group_key = self.decision_group_key(decision)
            current = grouped_actionable.get(group_key)
            if current is None or self.decision_recency_key(decision) > self.decision_recency_key(current):
                grouped_actionable[group_key] = decision

        for decision in actionable:
            group_key = self.decision_group_key(decision)
            if grouped_actionable.get(group_key) is decision:
                continue
            decision.status = "superseded"
            decision.reason = (
                "a newer highlighted reMarkable duplicate for this paper was chosen "
                "and will be applied to all matching Zotero duplicates"
            )

        actionable = list(grouped_actionable.values())
        if not actionable:
            return report

        importer = ZoteroImporter(self.config.zotero, self.config.annotated_import)
        backup_created = False

        for index, decision in enumerate(actionable, start=1):
            record = self.sync_state.get(decision.source_attachment_key)
            if record is None:
                report.errors.append(
                    f"Return-sync state was missing for source attachment key {decision.source_attachment_key}."
                )
                continue

            if not decision.remote_uuid:
                continue

            try:
                self.emit_progress(
                    f"Importing annotated PDF {index}/{len(actionable)}: "
                    f"{record.visible_name}"
                )
                export_path = self._device_export_path(
                    visible_name=record.visible_name,
                    remote_uuid=decision.remote_uuid,
                )
                self.client.download_annotated_pdf(decision.remote_uuid, export_path)
                decision.export_path = export_path
                duplicate_records = self.matching_duplicate_records(record.visible_name)
                duplicate_attachment_paths = self.matching_duplicate_attachment_paths(
                    record.visible_name,
                    importer,
                )
                if not duplicate_records:
                    duplicate_records = [record]
                if not duplicate_attachment_paths:
                    duplicate_attachment_paths = {
                        record.attachment_key: Path(record.source_path)
                    }

                exported_fingerprint = compute_file_fingerprint(export_path)

                import_source_path = prepare_import_source(self.config, export_path)
                if import_source_path != export_path:
                    decision.repaired_output_path = import_source_path

                import_fingerprint = compute_file_fingerprint(import_source_path)
                if (
                    duplicate_attachment_paths
                    and self.local_paths_match_fingerprint(
                        list(duplicate_attachment_paths.values()),
                        import_fingerprint,
                    )
                ):
                    decision.status = "unchanged"
                    decision.reason = (
                        "all matching Zotero duplicate PDFs already match the repaired/importable version"
                    )
                    for target_record in duplicate_records:
                        self.import_state.record_observation(
                            source_attachment_key=target_record.attachment_key,
                            parent_item_key=target_record.parent_item_key or "",
                            export_path=export_path,
                            export_fingerprint=exported_fingerprint,
                            remote_uuid=(
                                decision.remote_uuid
                                if target_record.attachment_key == record.attachment_key
                                else None
                            ),
                            remote_last_modified=(
                                decision.remote_last_modified
                                if target_record.attachment_key == record.attachment_key
                                else None
                            ),
                            import_source_path=import_source_path,
                            import_file_fingerprint=import_fingerprint,
                        )
                    self.import_state.save()
                    continue

                if not record.parent_item_key:
                    raise RuntimeError(
                        "Matched Zotero PDF does not have a parent item key, so the "
                        "annotated copy cannot be attached safely."
                    )

                if not backup_created:
                    importer.ensure_safe_to_write()
                    report.db_backup_path = importer.backup_database_bundle()
                    backup_created = True

                if self.config.annotated_import.replace_source_attachment:
                    results = importer.replace_attachment_pdfs(
                        source_attachment_keys=list(duplicate_attachment_paths),
                        source_pdf_path=import_source_path,
                    )
                else:
                    from remarkable_zotero_sync.annotated_import import default_attachment_title

                    results = [
                        importer.import_stored_pdf(
                        parent_item_key=record.parent_item_key,
                        source_pdf_path=import_source_path,
                        attachment_title=default_attachment_title(self.config, export_path),
                        source_attachment_key=record.attachment_key,
                        )
                    ]
            except Exception as exc:
                report.errors.append(
                    f"Failed to pull/import annotations for {record.visible_name}: {exc}"
                )
                continue

            decision.status = "imported"
            primary_result = next(
                (
                    result
                    for result in results
                    if result.source_attachment_key == record.attachment_key
                ),
                results[0],
            )
            if self.config.annotated_import.replace_source_attachment:
                if len(results) == 1:
                    decision.reason = (
                        "annotated reMarkable PDF replaced the tracked Zotero PDF"
                    )
                else:
                    decision.reason = (
                        "annotated reMarkable PDF replaced "
                        f"{len(results)} matching Zotero duplicate PDFs"
                    )
            else:
                decision.reason = "annotated reMarkable PDF was imported back into Zotero"
            decision.imported_attachment_key = primary_result.imported_attachment_key
            report.imported.extend(results)
            results_by_key = {
                result.source_attachment_key: result
                for result in results
            }
            for target_record in duplicate_records:
                result = results_by_key.get(target_record.attachment_key)
                if result is None:
                    continue
                self.import_state.record_import(
                    source_attachment_key=target_record.attachment_key,
                    parent_item_key=result.parent_item_key,
                    export_path=export_path,
                    export_fingerprint=exported_fingerprint,
                    import_source_path=import_source_path,
                    import_file_fingerprint=import_fingerprint,
                    imported_attachment_key=result.imported_attachment_key,
                    remote_uuid=(
                        decision.remote_uuid
                        if target_record.attachment_key == record.attachment_key
                        else None
                    ),
                    remote_last_modified=(
                        decision.remote_last_modified
                        if target_record.attachment_key == record.attachment_key
                        else None
                    ),
                )
            self.import_state.save()

            updated_attachment_keys = {
                target_record.attachment_key
                for target_record in duplicate_records
            }
            for sibling_decision in report.decisions:
                if (
                    sibling_decision is decision
                    or sibling_decision.source_attachment_key not in updated_attachment_keys
                ):
                    continue
                sibling_decision.status = "updated_duplicate"
                sibling_decision.reason = (
                    "newest annotated reMarkable version for this paper was applied "
                    "to this matching Zotero duplicate"
                )
                sibling_decision.imported_attachment_key = sibling_decision.source_attachment_key

        return report

    def _select_remote_candidate(
        self,
        record: SyncRecord,
        items: dict[str, object],
        highlighted_ids: set[str],
    ) -> tuple[str, str] | None:
        candidate_uuids: list[str] = []
        for value in [record.remote_uuid, *reversed(record.remote_uuid_history)]:
            if value and value not in candidate_uuids:
                candidate_uuids.append(value)

        def add_candidate(
            bucket: list[tuple[str, int]],
            candidate_uuid: str,
        ) -> None:
            if not RemarkableClient.item_is_accessible(
                candidate_uuid,
                items,
                expected_type="DocumentType",
            ):
                return
            item = items[candidate_uuid]
            last_modified = int(str(item.raw.get("lastModified", "0")) or "0")
            bucket.append((candidate_uuid, last_modified))

        highlighted_candidates: list[tuple[str, int]] = []
        for candidate_uuid in candidate_uuids:
            if candidate_uuid in highlighted_ids:
                add_candidate(highlighted_candidates, candidate_uuid)

        visible_name_matches = [
            item
            for item in items.values()
            if RemarkableClient.item_is_accessible(
                item.uuid,
                items,
                expected_type="DocumentType",
            )
            and item.visible_name == record.visible_name
        ]
        if len(visible_name_matches) == 1:
            visible_name_uuid = visible_name_matches[0].uuid
            if visible_name_uuid in highlighted_ids:
                add_candidate(highlighted_candidates, visible_name_uuid)

        if highlighted_candidates:
            deduped: dict[str, int] = {}
            for remote_uuid, last_modified in highlighted_candidates:
                deduped[remote_uuid] = max(last_modified, deduped.get(remote_uuid, 0))
            ordered = sorted(deduped.items(), key=lambda item: item[1], reverse=True)
            remote_uuid, last_modified = ordered[0]
            return remote_uuid, str(last_modified)

        return None

    def _device_export_path(self, *, visible_name: str, remote_uuid: str) -> Path:
        base_name = sanitize_filename_component(visible_name)
        filename = f"{base_name} - annotated [{remote_uuid[:8]}].pdf"
        return self.config.annotated_import.downloaded_dir / filename

    @staticmethod
    def _state_has_valid_import_artifact(path_value: str) -> bool:
        if not path_value:
            return False

        path = Path(path_value)
        if not path.exists() or not path.is_file():
            return False

        try:
            if zipfile.is_zipfile(path):
                return False
            with path.open("rb") as handle:
                return handle.read(5) == b"%PDF-"
        except OSError:
            return False
