from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import re

from remarkable_zotero_sync.annotated_import import compute_file_fingerprint
from remarkable_zotero_sync.annotated_import_state import AnnotatedImportStateStore
from remarkable_zotero_sync.config import AppConfig
from remarkable_zotero_sync.models import (
    RemarkableItem,
    SyncDecision,
    SyncReport,
    UploadRequest,
)
from remarkable_zotero_sync.remarkable_client import RemarkableClient
from remarkable_zotero_sync.sync_state import (
    SyncStateStore,
    compute_attachment_content_fingerprint,
    compute_attachment_fingerprint,
)
from remarkable_zotero_sync.zotero_importer import ZoteroImporter
from remarkable_zotero_sync.zotero_local import ZoteroLibrary


class SyncService:
    def __init__(
        self,
        config: AppConfig,
        *,
        progress: Callable[[str], None] | None = None,
    ):
        self.config = config
        self.progress = progress
        self.state = SyncStateStore(config.sync.state_path)
        self.state.load()
        self.import_state = AnnotatedImportStateStore(config.annotated_import.state_path)
        self.import_state.load()

    def emit_progress(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)

    def base_target_folder_path(self) -> str:
        return RemarkableClient.normalize_folder_path(self.config.remarkable.folder_path)

    def imported_attachment_source_keys(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for record in self.import_state.records.values():
            for imported_attachment_key in record.imported_attachment_keys:
                mapping[imported_attachment_key] = record.source_attachment_key
        return mapping

    @staticmethod
    def merge_tag_lists(*tag_lists: list[str]) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for tag_list in tag_lists:
            for raw_tag in tag_list:
                tag = raw_tag.strip()
                if not tag:
                    continue
                folded = tag.casefold()
                if folded in seen:
                    continue
                seen.add(folded)
                merged.append(tag)
        return merged

    def target_folder_path_for_attachment(self, attachment) -> str:
        base_path = self.base_target_folder_path()
        if not attachment.collection_path_parts:
            return base_path

        suffix = "/".join(part.strip() for part in attachment.collection_path_parts if part.strip())
        if not suffix:
            return base_path
        if base_path == "/":
            return f"/{suffix}"
        return f"{base_path}/{suffix}"

    @staticmethod
    def canonical_group_key_for_attachment(attachment) -> str:
        normalized = attachment.visible_name.casefold()
        normalized = re.sub(r"[^0-9a-z]+", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized or attachment.visible_name.casefold()

    def canonical_decision_rank(self, decision: SyncDecision) -> tuple[object, ...]:
        record = self.state.get(decision.attachment.attachment_key)
        import_record = self.import_state.get(decision.attachment.attachment_key)
        return (
            decision.status != "error",
            decision.attachment.routing_error is None,
            import_record is not None,
            (
                import_record.latest_remote_last_modified
                if import_record is not None and import_record.latest_remote_last_modified
                else ""
            ),
            import_record.updated_at if import_record is not None else "",
            decision.attachment.date_modified,
            record.updated_at if record is not None else "",
            decision.attachment.date_added,
            decision.attachment.selected_attachment_key,
        )

    def collapse_duplicate_title_decisions(self, report: SyncReport) -> None:
        grouped: dict[str, list[int]] = {}
        for index, decision in enumerate(report.decisions):
            group_key = self.canonical_group_key_for_attachment(decision.attachment)
            grouped.setdefault(group_key, []).append(index)

        for indexes in grouped.values():
            if len(indexes) <= 1:
                continue

            canonical_index = max(
                indexes,
                key=lambda index: self.canonical_decision_rank(report.decisions[index]),
            )
            canonical_decision = report.decisions[canonical_index]
            if len(indexes) > 1 and canonical_decision.status != "error":
                report.decisions[canonical_index] = replace(
                    canonical_decision,
                    reason=(
                        f"{canonical_decision.reason} This Zotero copy was chosen as the "
                        "canonical reMarkable version for this paper."
                    ),
                )

            for index in indexes:
                if index == canonical_index:
                    continue
                decision = report.decisions[index]
                report.decisions[index] = replace(
                    decision,
                    status="shadowed",
                    reason=(
                        "Another Zotero duplicate of this paper was chosen as the "
                        "canonical reMarkable copy, so this copy will not be kept as "
                        "a separate document on the device."
                    ),
                )

    def matches_legacy_root_fingerprint(
        self,
        *,
        attachment,
        record,
        legacy_fingerprint: str,
    ) -> bool:
        return (
            not attachment.collection_path_parts
            and record.fingerprint == legacy_fingerprint
            and record.target_folder_path in {"", "/"}
        )

    @staticmethod
    def stale_remote_uuids(record) -> list[str]:
        stale: list[str] = []
        seen: set[str] = set()
        for remote_uuid in record.remote_uuid_history:
            if not remote_uuid or remote_uuid == record.remote_uuid or remote_uuid in seen:
                continue
            seen.add(remote_uuid)
            stale.append(remote_uuid)
        return stale

    def highlighted_stale_copy_is_safe_to_remove(
        self,
        *,
        attachment,
        stale_remote_uuid: str,
        current_local_fingerprint: str | None = None,
    ) -> bool:
        import_record = self.import_state.get(attachment.attachment_key)
        if import_record is None or not import_record.latest_import_file_fingerprint:
            return False

        if current_local_fingerprint is None:
            try:
                current_local_fingerprint = compute_file_fingerprint(attachment.pdf_path)
            except OSError:
                return False

        if current_local_fingerprint != import_record.latest_import_file_fingerprint:
            return False

        if import_record.latest_remote_uuid:
            return stale_remote_uuid == import_record.latest_remote_uuid

        return True

    @staticmethod
    def is_legacy_tag_only_change(
        *,
        attachment,
        record,
        target_folder_path: str,
    ) -> bool:
        return (
            record.content_fingerprint is None
            and record.source_path == str(attachment.pdf_path)
            and record.visible_name == attachment.visible_name
            and record.target_folder_path == target_folder_path
            and sorted(record.tags) != sorted(attachment.tags)
        )

    @staticmethod
    def is_title_only_change(
        *,
        attachment,
        record,
        target_folder_path: str,
    ) -> bool:
        return (
            record.source_path == str(attachment.pdf_path)
            and record.target_folder_path == target_folder_path
            and sorted(record.tags) == sorted(attachment.tags)
            and record.visible_name != attachment.visible_name
        )

    def reconcile_missing_remote_documents(
        self,
        report: SyncReport,
        *,
        items: dict[str, object],
        existing_document_file_ids: set[str] | None = None,
    ) -> None:
        for index, decision in enumerate(report.decisions):
            if decision.status in {"error", "shadowed"}:
                continue
            record = self.state.get(decision.attachment.attachment_key)
            if record is None or not record.remote_uuid:
                continue
            remote_item = items.get(record.remote_uuid)
            if (
                RemarkableClient.item_is_accessible(
                    record.remote_uuid,
                    items,
                    expected_type="DocumentType",
                )
                and (
                    existing_document_file_ids is None
                    or record.remote_uuid in existing_document_file_ids
                )
            ):
                continue
            if remote_item is None:
                reason = (
                    "tracked reMarkable document is missing from the device, so it "
                    "will be uploaded again"
                )
            elif not RemarkableClient.item_is_accessible(
                record.remote_uuid,
                items,
                expected_type="DocumentType",
            ):
                reason = (
                    "tracked reMarkable document is in the trash or under a trashed "
                    "folder, so it will be uploaded again"
                )
            else:
                reason = (
                    "tracked reMarkable metadata still exists, but the source PDF file "
                    "is missing from xochitl, so it will be uploaded again"
                )
            report.decisions[index] = replace(
                decision,
                status="new",
                reason=reason,
            )

    def build_report(self, *, dry_run: bool) -> SyncReport:
        report = SyncReport(
            collection_name=(
                "Entire Zotero library"
                if self.config.zotero.library_scope == "library"
                else self.config.zotero.collection_name
            ),
            dry_run=dry_run,
        )
        report.target_parent = self.base_target_folder_path()
        if self.config.zotero.library_scope == "library":
            self.emit_progress("Loading the entire Zotero library...")
        else:
            self.emit_progress(
                f'Loading Zotero collection "{self.config.zotero.collection_name}"...'
            )

        with ZoteroLibrary(self.config.zotero) as library:
            report.collection_name = library.source_scope_label()
            attachments = library.list_sync_target_pdfs(
                imported_attachment_source_keys=self.imported_attachment_source_keys(),
            )

        self.emit_progress(
            f"Evaluating {len(attachments)} Zotero PDF(s) for reMarkable sync..."
        )

        for attachment in attachments:
            target_folder_path = self.target_folder_path_for_attachment(attachment)
            if attachment.routing_error:
                report.decisions.append(
                    SyncDecision(
                        attachment=attachment,
                        target_folder_path=target_folder_path,
                        fingerprint=None,
                        status="error",
                        reason=attachment.routing_error,
                    )
                )
                continue

            try:
                fingerprint = compute_attachment_fingerprint(
                    attachment,
                    target_folder_path=target_folder_path,
                )
                content_fingerprint = compute_attachment_content_fingerprint(
                    attachment,
                    target_folder_path=target_folder_path,
                )
                legacy_fingerprint = compute_attachment_fingerprint(attachment)
            except Exception as exc:
                report.decisions.append(
                    SyncDecision(
                        attachment=attachment,
                        target_folder_path=target_folder_path,
                        fingerprint=None,
                        status="error",
                        reason=str(exc),
                    )
                )
                continue

            record = self.state.get(attachment.attachment_key)
            if record is None:
                report.decisions.append(
                    SyncDecision(
                        attachment=attachment,
                        target_folder_path=target_folder_path,
                        fingerprint=fingerprint,
                        status="new",
                        reason="not present in local sync state",
                    )
                )
                continue

            if (
                record.fingerprint == fingerprint
                or self.matches_legacy_root_fingerprint(
                    attachment=attachment,
                    record=record,
                    legacy_fingerprint=legacy_fingerprint,
                )
            ):
                if not dry_run and record.fingerprint != fingerprint:
                    record.fingerprint = fingerprint
                    record.content_fingerprint = content_fingerprint
                    record.target_folder_path = target_folder_path
                    record.source_path = str(attachment.pdf_path)
                    record.visible_name = attachment.visible_name
                    record.tags = list(attachment.tags)
                    self.state.save()

                report.decisions.append(
                    SyncDecision(
                        attachment=attachment,
                        target_folder_path=target_folder_path,
                        fingerprint=fingerprint,
                        status="unchanged",
                        reason="fingerprint matches previous sync state",
                        previous_remote_uuid=record.remote_uuid,
                    )
                )
                continue

            if (
                record.content_fingerprint == content_fingerprint
                and record.target_folder_path == target_folder_path
                and record.visible_name == attachment.visible_name
            ) or self.is_legacy_tag_only_change(
                attachment=attachment,
                record=record,
                target_folder_path=target_folder_path,
            ) or self.is_title_only_change(
                attachment=attachment,
                record=record,
                target_folder_path=target_folder_path,
            ):
                report.decisions.append(
                    SyncDecision(
                        attachment=attachment,
                        target_folder_path=target_folder_path,
                        fingerprint=fingerprint,
                        status="metadata_changed",
                        reason=(
                            "title or tags changed; the tracked reMarkable document "
                            "metadata will be updated in place"
                        ),
                        previous_remote_uuid=record.remote_uuid,
                    )
                )
                continue

            if self.config.sync.upload_changed_as_new_copy:
                reason = (
                    "source PDF or tags changed; this version will upload as the new "
                    "tracked reMarkable copy and older tracked copies will be removed"
                )
            else:
                reason = (
                    "source PDF or tags changed, but upload_changed_as_new_copy is disabled"
                )

            report.decisions.append(
                SyncDecision(
                    attachment=attachment,
                    target_folder_path=target_folder_path,
                    fingerprint=fingerprint,
                    status="changed",
                    reason=reason,
                    previous_remote_uuid=record.remote_uuid,
                )
            )

        self.collapse_duplicate_title_decisions(report)
        report.decisions.sort(
            key=lambda decision: (
                decision.status,
                decision.attachment.visible_name.lower(),
            )
        )
        return report

    def execute(self) -> SyncReport:
        report = self.build_report(dry_run=False)
        self.persist_missing_zotero_tags(report)
        client: RemarkableClient | None = None
        items: dict[str, object] = {}

        def ensure_client_loaded() -> tuple[RemarkableClient, dict[str, object]]:
            nonlocal client, items
            if client is None:
                client = RemarkableClient(self.config.remarkable)
                items = client.load_metadata_items()
                report.target_parent = self.base_target_folder_path()
                self.emit_progress("Connected to reMarkable metadata.")
            return client, items

        if any(
            decision.status != "error"
            and self.state.get(decision.attachment.attachment_key) is not None
            for decision in report.decisions
        ):
            loaded_client, loaded_items = ensure_client_loaded()
            existing_document_file_ids: set[str] | None = None
            try:
                existing_document_file_ids = loaded_client.list_existing_document_file_ids()
            except Exception as exc:
                report.errors.append(
                    "Could not verify existing reMarkable PDF files; metadata-only "
                    f"presence checks will be used instead: {exc}"
                )
            self.reconcile_missing_remote_documents(
                report,
                items=loaded_items,
                existing_document_file_ids=existing_document_file_ids,
            )

        uploadable = [
            decision
            for decision in report.decisions
            if decision.status == "new"
            or (
                decision.status == "changed"
                and self.config.sync.upload_changed_as_new_copy
            )
        ]
        metadata_only = [
            decision
            for decision in report.decisions
            if decision.status in {"metadata_changed", "unchanged"}
        ]

        cleanup_targets = [
            decision
            for decision in report.decisions
            if self.state.get(decision.attachment.attachment_key) is not None
            and (
                self.stale_remote_uuids(self.state.get(decision.attachment.attachment_key))
                or decision.status == "shadowed"
            )
        ]

        if not uploadable and not cleanup_targets and not metadata_only:
            return report

        client, items = ensure_client_loaded()

        if metadata_only and self.config.remarkable.tag_metadata_field:
            self.emit_progress("Reconciling reMarkable document tags...")
            for decision in metadata_only:
                record = self.state.get(decision.attachment.attachment_key)
                if record is None or not record.remote_uuid:
                    continue
                if not RemarkableClient.item_is_accessible(
                    record.remote_uuid,
                    items,
                    expected_type="DocumentType",
                ):
                    continue
                try:
                    updated = client.update_document_tags(
                        record.remote_uuid,
                        tags=decision.attachment.tags,
                        visible_name=decision.attachment.visible_name,
                        items=items,
                    )
                except Exception as exc:
                    report.errors.append(
                        f"Failed to update tags for {decision.attachment.visible_name}: {exc}"
                    )
                    continue

                if not updated:
                    continue

                record.tags = list(decision.attachment.tags)
                if decision.fingerprint is not None:
                    record.fingerprint = decision.fingerprint
                record.content_fingerprint = compute_attachment_content_fingerprint(
                    decision.attachment,
                    target_folder_path=decision.target_folder_path,
                )
                record.target_folder_path = decision.target_folder_path
                record.source_path = str(decision.attachment.pdf_path)
                record.visible_name = decision.attachment.visible_name
                self.state.save()
                report.metadata_updates.append(
                    f"{decision.attachment.visible_name} remote_uuid={record.remote_uuid}"
                )

        base_parent_uuid = ""
        if uploadable:
            self.emit_progress("Ensuring reMarkable destination folders exist...")
            if self.config.remarkable.folder_uuid:
                base_parent_uuid = client.ensure_existing_folder_uuid(
                    self.config.remarkable.folder_uuid,
                    items=items,
                )
            else:
                base_parent_uuid = client.ensure_folder_path(
                    self.base_target_folder_path(),
                    items=items,
                )

        for index, decision in enumerate(uploadable, start=1):
            if decision.fingerprint is None:
                report.errors.append(
                    f"Skipped {decision.attachment.visible_name}: missing fingerprint."
                )
                continue

            try:
                parent_uuid = client.ensure_child_folders(
                    base_parent_uuid,
                    decision.attachment.collection_path_parts,
                    items=items,
                )
            except Exception as exc:
                report.errors.append(
                    f"Failed to prepare target folder {decision.target_folder_path} "
                    f"for {decision.attachment.visible_name}: {exc}"
                )
                continue

            self.emit_progress(
                f"Uploading {index}/{len(uploadable)}: "
                f"{decision.attachment.visible_name} -> {decision.target_folder_path}"
            )
            request = UploadRequest(
                source_path=decision.attachment.pdf_path,
                visible_name=decision.attachment.visible_name,
                parent_uuid=parent_uuid,
                tags=decision.attachment.tags,
            )

            try:
                upload_result = client.upload_pdf(request)
            except Exception as exc:
                report.errors.append(
                    f"Failed to upload {decision.attachment.visible_name}: {exc}"
                )
                continue

            self.state.record_upload(
                attachment=decision.attachment,
                fingerprint=decision.fingerprint,
                content_fingerprint=compute_attachment_content_fingerprint(
                    decision.attachment,
                    target_folder_path=decision.target_folder_path,
                ),
                upload_result=upload_result,
                target_folder_path=decision.target_folder_path,
            )
            metadata = {
                "deleted": False,
                "parent": parent_uuid,
                "type": "DocumentType",
                "visibleName": request.visible_name,
            }
            if request.tags and self.config.remarkable.tag_metadata_field:
                metadata[self.config.remarkable.tag_metadata_field] = list(request.tags)
            items[upload_result.remote_uuid] = RemarkableItem(
                uuid=upload_result.remote_uuid,
                visible_name=request.visible_name,
                item_type="DocumentType",
                parent=parent_uuid,
                deleted=False,
                raw=metadata,
            )
            self.state.save()
            report.uploaded.append(upload_result)

        cleanup_candidates = sum(
            len(self.stale_remote_uuids(record))
            for record in self.state.records.values()
        )
        highlighted_document_ids: set[str] | None = set()
        if cleanup_candidates:
            self.emit_progress("Cleaning up stale reMarkable copies...")
            try:
                highlighted_document_ids = client.list_document_ids_with_highlights()
            except Exception as exc:
                highlighted_document_ids = None
                report.errors.append(
                    "Could not inspect reMarkable highlights before cleaning up stale "
                    "copies, so older copies were preserved to avoid deleting "
                    f"annotated documents: {exc}"
                )
        for decision in report.decisions:
            record = self.state.get(decision.attachment.attachment_key)
            if record is None:
                continue
            if not RemarkableClient.item_is_accessible(
                record.remote_uuid,
                items,
                expected_type="DocumentType",
                ):
                    continue

            remote_cleanup_candidates = list(self.stale_remote_uuids(record))
            if decision.status == "shadowed" and record.remote_uuid:
                remote_cleanup_candidates = [
                    record.remote_uuid,
                    *[
                        remote_uuid
                        for remote_uuid in remote_cleanup_candidates
                        if remote_uuid != record.remote_uuid
                    ],
                ]

            current_local_fingerprint: str | None = None
            if (
                highlighted_document_ids is not None
                and any(
                    candidate_uuid in highlighted_document_ids
                    for candidate_uuid in remote_cleanup_candidates
                )
            ):
                try:
                    current_local_fingerprint = compute_file_fingerprint(
                        decision.attachment.pdf_path
                    )
                except OSError:
                    current_local_fingerprint = None

            for stale_remote_uuid in remote_cleanup_candidates:
                if highlighted_document_ids is None:
                    continue
                if stale_remote_uuid in highlighted_document_ids:
                    if self.highlighted_stale_copy_is_safe_to_remove(
                        attachment=decision.attachment,
                        stale_remote_uuid=stale_remote_uuid,
                        current_local_fingerprint=current_local_fingerprint,
                    ):
                        pass
                    else:
                        report.cleanup_preserved.append(
                            f"{decision.attachment.visible_name} remote_uuid={stale_remote_uuid}"
                        )
                        continue
                try:
                    deleted = client.soft_delete_document(
                        stale_remote_uuid,
                        items=items,
                    )
                except Exception as exc:
                    report.errors.append(
                        f"Failed to remove stale reMarkable copy {stale_remote_uuid} "
                        f"for {decision.attachment.visible_name}: {exc}"
                    )
                    continue

                if deleted:
                    report.cleanup_actions.append(
                        f"{decision.attachment.visible_name} remote_uuid={stale_remote_uuid}"
                    )

        if (
            report.uploaded
            or report.cleanup_actions
            or report.metadata_updates
        ) and self.config.remarkable.restart_xochitl_after_sync:
            self.emit_progress("Restarting xochitl on the reMarkable...")
            try:
                client.restart_xochitl()
            except Exception as exc:
                report.errors.append(
                    "Sync changes succeeded, but xochitl restart failed: "
                    f"{exc}"
                )
            else:
                report.xochitl_restarted = True

        return report

    def persist_missing_zotero_tags(self, report: SyncReport) -> None:
        if not self.config.sync.persist_author_tags_to_zotero:
            return

        tag_targets: dict[str, list[str]] = {}
        for decision in report.decisions:
            attachment = decision.attachment
            item_key = attachment.parent_item_key or attachment.selected_attachment_key
            if not item_key:
                continue
            tag_targets[item_key] = self.merge_tag_lists(
                tag_targets.get(item_key, []),
                attachment.tags,
            )

        if not tag_targets:
            return

        writer = ZoteroImporter(self.config.zotero, self.config.annotated_import)
        pending: list[tuple[str, list[str]]] = []
        for item_key, tags in sorted(tag_targets.items()):
            missing = writer.missing_item_tags(
                item_key=item_key,
                desired_tags=tags,
            )
            if missing:
                pending.append((item_key, missing))

        if not pending:
            return

        writer.ensure_safe_to_write()
        report.zotero_db_backup_path = writer.backup_database_bundle()
        self.emit_progress(
            f"Adding missing derived Zotero tags to {len(pending)} item(s)..."
        )

        for item_key, _ in pending:
            added = writer.add_item_tags(
                item_key=item_key,
                desired_tags=tag_targets[item_key],
            )
            if added:
                report.zotero_tag_updates.append(
                    f'{item_key}: {", ".join(added)}'
                )
