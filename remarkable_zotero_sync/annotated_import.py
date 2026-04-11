from __future__ import annotations

from collections.abc import Callable
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from remarkable_zotero_sync.annotated_import_state import AnnotatedImportStateStore
from remarkable_zotero_sync.config import AppConfig
from remarkable_zotero_sync.models import AnnotatedImportDecision, AnnotatedImportReport
from remarkable_zotero_sync.sync_state import SyncStateStore, SyncRecord
from remarkable_zotero_sync.zotero_importer import ZoteroImporter

HIGHLIGHT_REPAIR_SUFFIX = " real_highlights"
ANNOTATED_SUFFIX_PATTERNS = [
    r"\s*-\s*annotated$",
    r"\s+annotated$",
    r"\s*-\s*exported$",
    r"\s+exported$",
    r"\s+real_highlights$",
]


@dataclass(frozen=True)
class MatchResult:
    record: SyncRecord
    match_basis: str


def compute_file_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def strip_export_suffixes(stem: str) -> str:
    stripped = stem
    changed = True
    while changed:
        changed = False
        for pattern in ANNOTATED_SUFFIX_PATTERNS:
            updated = re.sub(pattern, "", stripped, flags=re.IGNORECASE)
            if updated != stripped:
                stripped = updated
                changed = True
    return stripped.strip()


def normalize_match_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.casefold()
    normalized = re.sub(r"[^0-9a-z]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def record_match_candidates(record: SyncRecord) -> dict[str, str]:
    source_stem = Path(record.source_path).stem
    return {
        normalize_match_text(record.visible_name): "visible_name",
        normalize_match_text(source_stem): "source_filename",
    }


def matching_sync_records_for_visible_name(
    sync_state: SyncStateStore,
    visible_name: str,
) -> list[SyncRecord]:
    normalized_visible_name = normalize_match_text(visible_name)
    if not normalized_visible_name:
        return []

    matches = [
        record
        for record in sync_state.records.values()
        if normalize_match_text(record.visible_name) == normalized_visible_name
    ]
    return sorted(matches, key=lambda record: (record.updated_at, record.attachment_key))


def preferred_sync_record(records: list[SyncRecord]) -> SyncRecord:
    if not records:
        raise ValueError("preferred_sync_record() requires at least one SyncRecord.")
    return max(records, key=lambda record: (record.updated_at, record.attachment_key))


def match_export_to_sync_record(
    export_path: Path,
    sync_state: SyncStateStore,
) -> MatchResult:
    export_base = strip_export_suffixes(export_path.stem)
    normalized_export = normalize_match_text(export_base)
    if not normalized_export:
        raise ValueError(f"Could not derive a matchable title from {export_path.name}")

    matches: list[MatchResult] = []
    for record in sync_state.records.values():
        for candidate, basis in record_match_candidates(record).items():
            if candidate == normalized_export:
                matches.append(MatchResult(record=record, match_basis=basis))

    if not matches:
        raise ValueError(
            f"No prior synced Zotero document matched exported PDF {export_path.name}. "
            "The import step currently matches by uploaded visible name or source filename."
        )

    if len(matches) == 1:
        return matches[0]

    exact_matches = [
        match
        for match in matches
        if export_base.casefold()
        in {
            match.record.visible_name.casefold(),
            Path(match.record.source_path).stem.casefold(),
        }
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]
    if exact_matches:
        same_visible_name_group = matching_sync_records_for_visible_name(
            sync_state,
            exact_matches[0].record.visible_name,
        )
        if len(same_visible_name_group) >= len(exact_matches):
            return MatchResult(
                record=preferred_sync_record(same_visible_name_group),
                match_basis="duplicate_visible_name_group",
            )

    candidate_keys = ", ".join(sorted({match.record.attachment_key for match in matches}))
    raise ValueError(
        f"Exported PDF {export_path.name} matched multiple synced Zotero items "
        f"({candidate_keys}). Rename the file more specifically or import it explicitly later."
    )


def default_repaired_output_path(config: AppConfig, export_path: Path) -> Path:
    return config.annotated_import.repaired_dir / f"{export_path.stem}{HIGHLIGHT_REPAIR_SUFFIX}.pdf"


def default_attachment_title(config: AppConfig, export_path: Path) -> str:
    return config.annotated_import.attachment_title


def prepare_import_source(config: AppConfig, export_path: Path) -> Path:
    if export_path.stem.endswith(HIGHLIGHT_REPAIR_SUFFIX):
        return export_path

    config.annotated_import.repaired_dir.mkdir(parents=True, exist_ok=True)
    from remarkable_zotero_sync.highlight_repair import repair_pdf

    result = repair_pdf(
        export_path,
        output_path=default_repaired_output_path(config, export_path),
        skip_if_output_exists=False,
        verbose=False,
    )
    if result.status in {"saved", "skipped_existing_output"} and result.output_path is not None:
        return result.output_path
    return export_path


class AnnotatedImportService:
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

    def emit_progress(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)

    def matching_duplicate_records(self, visible_name: str) -> list[SyncRecord]:
        matches = matching_sync_records_for_visible_name(self.sync_state, visible_name)
        if matches:
            return matches

        for record in self.sync_state.records.values():
            if record.visible_name == visible_name:
                return [record]
        return []

    def matching_duplicate_attachment_paths(
        self,
        visible_name: str,
        importer: ZoteroImporter,
    ) -> dict[str, Path]:
        matches = importer.matching_pdf_attachment_paths_for_parent_title(visible_name)
        for record in self.matching_duplicate_records(visible_name):
            matches.setdefault(record.attachment_key, Path(record.source_path))
        return matches

    def collect_exports(self, raw_paths: list[str] | None = None) -> list[Path]:
        if raw_paths:
            return [Path(value).expanduser().resolve() for value in raw_paths]

        inbox_dir = self.config.annotated_import.inbox_dir
        if not inbox_dir.exists():
            return []

        exports = []
        for path in sorted(inbox_dir.glob("*.pdf")):
            if path.stem.endswith(HIGHLIGHT_REPAIR_SUFFIX):
                continue
            exports.append(path.resolve())
        return exports

    def build_report(self, *, raw_paths: list[str] | None = None, dry_run: bool) -> AnnotatedImportReport:
        report = AnnotatedImportReport(dry_run=dry_run)
        export_paths = self.collect_exports(raw_paths)
        self.emit_progress(f"Inspecting {len(export_paths)} annotated PDF export(s)...")

        for export_path in export_paths:
            if not export_path.exists():
                report.decisions.append(
                    AnnotatedImportDecision(
                        export_path=export_path,
                        status="error",
                        reason="exported PDF path does not exist",
                    )
                )
                continue

            try:
                match = match_export_to_sync_record(export_path, self.sync_state)
            except Exception as exc:
                report.decisions.append(
                    AnnotatedImportDecision(
                        export_path=export_path,
                        status="unmatched",
                        reason=str(exc),
                    )
                )
                continue

            if not match.record.parent_item_key:
                report.decisions.append(
                    AnnotatedImportDecision(
                        export_path=export_path,
                        status="unsupported",
                        reason=(
                            "matched Zotero PDF does not have a parent item key, so this "
                            "first return-sync version cannot attach the annotated copy safely"
                        ),
                        source_attachment_key=match.record.attachment_key,
                        source_visible_name=match.record.visible_name,
                    )
                )
                continue

            export_fingerprint = compute_file_fingerprint(export_path)
            existing_import = self.import_state.get(match.record.attachment_key)
            if (
                existing_import is not None
                and existing_import.latest_export_fingerprint == export_fingerprint
            ):
                report.decisions.append(
                    AnnotatedImportDecision(
                        export_path=export_path,
                        status="unchanged",
                        reason="exported annotated PDF matches the last imported version",
                        source_attachment_key=match.record.attachment_key,
                        source_visible_name=match.record.visible_name,
                        parent_item_key=match.record.parent_item_key,
                    )
                )
                continue

            report.decisions.append(
                AnnotatedImportDecision(
                    export_path=export_path,
                    status="ready",
                    reason=(
                        "exported PDF matched a previously synced Zotero item and will be "
                        "repaired, then imported as a new child attachment"
                    ),
                    source_attachment_key=match.record.attachment_key,
                    source_visible_name=match.record.visible_name,
                    parent_item_key=match.record.parent_item_key,
                    repaired_output_path=default_repaired_output_path(self.config, export_path),
                )
            )

        return report

    def execute(self, *, raw_paths: list[str] | None = None) -> AnnotatedImportReport:
        report = self.build_report(raw_paths=raw_paths, dry_run=False)
        actionable = [decision for decision in report.decisions if decision.status == "ready"]
        if not actionable:
            return report

        importer = ZoteroImporter(self.config.zotero, self.config.annotated_import)
        importer.ensure_safe_to_write()
        report.db_backup_path = importer.backup_database_bundle()

        for index, decision in enumerate(actionable, start=1):
            try:
                self.emit_progress(
                    f"Importing annotated export {index}/{len(actionable)}: "
                    f"{decision.export_path.name}"
                )
                finalized_import_source = self._prepare_import_source(decision.export_path)
                import_fingerprint = compute_file_fingerprint(finalized_import_source)
                duplicate_attachment_paths = self.matching_duplicate_attachment_paths(
                    decision.source_visible_name or "",
                    importer,
                )
                duplicate_records = self.matching_duplicate_records(
                    decision.source_visible_name or ""
                )
                if not duplicate_records and decision.source_attachment_key:
                    record = self.sync_state.get(decision.source_attachment_key)
                    if record is not None:
                        duplicate_records = [record]

                if (
                    not duplicate_attachment_paths
                    and decision.source_attachment_key
                ):
                    source_record = self.sync_state.get(decision.source_attachment_key)
                    if source_record is not None:
                        duplicate_attachment_paths = {
                            source_record.attachment_key: Path(source_record.source_path)
                        }

                target_records = duplicate_records or []
                if (
                    duplicate_attachment_paths
                    and all(
                        path.exists()
                        and compute_file_fingerprint(path) == import_fingerprint
                        for path in duplicate_attachment_paths.values()
                    )
                ):
                    decision.status = "unchanged"
                    decision.reason = (
                        "all matching Zotero duplicate PDFs already match the repaired/importable version"
                    )
                    decision.import_source_path = finalized_import_source
                    continue

                if self.config.annotated_import.replace_source_attachment:
                    results = importer.replace_attachment_pdfs(
                        source_attachment_keys=list(duplicate_attachment_paths)
                        or [decision.source_attachment_key or ""],
                        source_pdf_path=finalized_import_source,
                    )
                else:
                    results = [
                        importer.import_stored_pdf(
                        parent_item_key=decision.parent_item_key or "",
                        source_pdf_path=finalized_import_source,
                        attachment_title=default_attachment_title(
                            self.config,
                            decision.export_path,
                        ),
                        source_attachment_key=decision.source_attachment_key or "",
                        )
                    ]
            except Exception as exc:
                report.errors.append(f"Failed on {decision.export_path.name}: {exc}")
                continue

            decision.import_source_path = finalized_import_source
            decision.imported_attachment_key = results[0].imported_attachment_key
            decision.status = "imported"
            if self.config.annotated_import.replace_source_attachment:
                if len(results) == 1:
                    decision.reason = (
                        "repaired/imported annotated PDF replaced the tracked Zotero PDF"
                    )
                else:
                    decision.reason = (
                        "repaired/imported annotated PDF replaced "
                        f"{len(results)} matching Zotero duplicate PDFs"
                    )
            else:
                decision.reason = "repaired/imported annotated PDF was added to Zotero"

            report.imported.extend(results)
            for result in results:
                self.import_state.record_import(
                    source_attachment_key=result.source_attachment_key,
                    parent_item_key=result.parent_item_key,
                    export_path=decision.export_path,
                    export_fingerprint=compute_file_fingerprint(decision.export_path),
                    import_source_path=finalized_import_source,
                    import_file_fingerprint=import_fingerprint,
                    imported_attachment_key=result.imported_attachment_key,
                )
            self.import_state.save()

        return report

    def _prepare_import_source(self, export_path: Path) -> Path:
        return prepare_import_source(self.config, export_path)
