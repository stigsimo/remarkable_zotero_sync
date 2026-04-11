from __future__ import annotations

import argparse
import sys
from pathlib import Path

from remarkable_zotero_sync.config import load_config
from remarkable_zotero_sync.models import (
    AnnotatedImportDecision,
    AnnotatedImportReport,
    HighlightRepairResult,
    ReturnSyncDecision,
    ReturnSyncReport,
    RoundTripSyncReport,
    SyncDecision,
    SyncReport,
)
from remarkable_zotero_sync.public_bootstrap import (
    choose_and_save_library_scope,
    ensure_public_config,
    ensure_remarkable_password,
    reset_public_workspace,
    resolve_config_path,
)


def console_progress(message: str) -> None:
    print(f"[progress] {message}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zotero-sync",
        description="Sync PDFs between a local Zotero library and a reMarkable device.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser(
        "sync",
        help="Run a round-trip sync between Zotero and reMarkable.",
    )
    sync_parser.add_argument(
        "--config",
        default="config.toml",
        help="Path to the TOML config file. Default: %(default)s",
    )
    sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the round-trip sync without making changes.",
    )
    sync_parser.add_argument(
        "--apply",
        action="store_true",
        help="Compatibility flag. Sync now applies by default unless you pass --dry-run.",
    )
    sync_parser.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Reset the public zotero_sync folder to its default state, including "
            "the saved SSH password. You must type yes to confirm."
        ),
    )
    sync_parser.add_argument(
        "--choose-library-scope",
        action="store_true",
        help=(
            "Choose whether sync uses only 'reMarkable Sync' and its "
            "subcollections or your entire Zotero library. You must type yes "
            "to save the change."
        ),
    )

    import_parser = subparsers.add_parser(
        "import-annotated",
        help="Repair exported annotated PDFs and import changed copies back into Zotero.",
    )
    import_parser.add_argument(
        "--config",
        default="config.toml",
        help="Path to the TOML config file. Default: %(default)s",
    )
    import_parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually import changed annotated PDFs into Zotero. Without this flag the command is a dry run.",
    )
    import_parser.add_argument(
        "pdfs",
        nargs="*",
        help="Optional exported PDF paths. If omitted, the configured annotated-import inbox is scanned.",
    )

    repair_parser = subparsers.add_parser(
        "repair-highlights",
        help="Convert reMarkable yellow overlay highlights into real PDF highlight annotations.",
    )
    repair_parser.add_argument(
        "pdfs",
        nargs="+",
        help="One or more PDF paths to process.",
    )
    repair_parser.add_argument(
        "--output-dir",
        help="Optional output directory. Defaults to the input PDF directory.",
    )
    repair_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the default skip-if-output-exists behavior.",
    )
    repair_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-page highlight discovery details.",
    )

    return parser


def summarize_sync_decision(decision: SyncDecision) -> str:
    line = (
        f"[{decision.status.upper()}] {decision.attachment.visible_name} "
        f"({decision.attachment.pdf_path}) -> {decision.target_folder_path}"
    )
    if decision.previous_remote_uuid:
        line += f" previous_remote_uuid={decision.previous_remote_uuid}"
    return f"{line}\n    {decision.reason}"


def print_sync_report(report: SyncReport) -> None:
    if report.collection_name == "Entire Zotero library":
        print(f"{report.collection_name} produced {len(report.decisions)} PDF attachment(s).")
    else:
        print(
            f'Zotero collection "{report.collection_name}" produced '
            f"{len(report.decisions)} PDF attachment(s)."
        )

    counts: dict[str, int] = {}
    for decision in report.decisions:
        counts[decision.status] = counts.get(decision.status, 0) + 1

    if counts:
        summary = ", ".join(
            f"{status}={count}" for status, count in sorted(counts.items())
        )
        print(f"Summary: {summary}")

    if report.target_parent:
        print(f"Base reMarkable folder: {report.target_parent}")

    for decision in report.decisions:
        print(summarize_sync_decision(decision))

    if report.dry_run:
        print("\nDry run only. Re-run without --dry-run to upload files.")
    else:
        if report.zotero_tag_updates:
            print(
                f"\nAdded missing Zotero tags to {len(report.zotero_tag_updates)} item(s)."
            )
            for update in report.zotero_tag_updates:
                print(f"[TAGGED] {update}")
        if report.zotero_db_backup_path:
            print(f"Zotero DB backup: {report.zotero_db_backup_path}")
        if report.metadata_updates:
            print(
                f"Updated tags on {len(report.metadata_updates)} existing reMarkable document(s)."
            )
            for update in report.metadata_updates:
                print(f"[METADATA] {update}")
        print(f"\nUploaded {len(report.uploaded)} document(s).")
        for upload in report.uploaded:
            print(
                f"[UPLOADED] {upload.visible_name} -> remote_uuid={upload.remote_uuid}"
            )
        if report.cleanup_actions:
            print(f"Removed {len(report.cleanup_actions)} stale reMarkable copy/copies.")
            for action in report.cleanup_actions:
                print(f"[REMOVED] {action}")
        if report.cleanup_preserved:
            print(
                f"Preserved {len(report.cleanup_preserved)} stale reMarkable copy/copies "
                "because they still have highlights."
            )
            for action in report.cleanup_preserved:
                print(f"[PRESERVED] {action}")
        if report.xochitl_restarted:
            print("xochitl was restarted after upload.")

    if report.errors:
        print("\nErrors:")
        for error in report.errors:
            print(f"- {error}")


def run_sync(args: argparse.Namespace) -> int:
    from remarkable_zotero_sync.roundtrip_sync import RoundTripSyncService

    config_path = resolve_config_path(args.config)
    if args.reset:
        reset_public_workspace(config_path)
        return 0

    ensure_public_config(config_path)
    if args.choose_library_scope:
        choose_and_save_library_scope(config_path)
        return 0

    password_override = ensure_remarkable_password(config_path)
    config = load_config(config_path, password_override=password_override)
    service = RoundTripSyncService(config, progress=console_progress)

    report = service.build_report() if args.dry_run else service.execute()
    print_roundtrip_sync_report(report)
    return 1 if (report.forward.errors or report.reverse.errors) else 0


def print_repair_result(result: HighlightRepairResult) -> None:
    if result.status == "saved":
        print(
            f"[saved] {result.output_path} "
            f"(pages_with_highlights={result.pages_with_highlights}, "
            f"parsed_rects={result.parsed_highlight_rect_count}, "
            f"added_annots={result.added_highlight_annotation_count}, "
            f"patched_extgstate={result.patched_extgstate_count})"
        )
        return

    if result.status == "skipped_existing_output":
        print(f"[skip] Output already exists: {result.output_path}")
        return

    print(f"[info] No compatible reMarkable highlight overlay found: {result.input_path}")


def run_repair_highlights(args: argparse.Namespace) -> int:
    from remarkable_zotero_sync.highlight_repair import repair_pdf

    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else None
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)

    exit_code = 0
    for raw_path in args.pdfs:
        input_path = Path(raw_path).expanduser().resolve()
        output_path = (
            output_dir / f"{input_path.stem} real_highlights.pdf"
            if output_dir is not None
            else None
        )
        try:
            result = repair_pdf(
                input_path,
                output_path=output_path,
                skip_if_output_exists=not args.force,
                verbose=args.verbose,
            )
        except Exception as exc:
            print(f"[error] Failed to repair {input_path}: {exc}")
            exit_code = 1
            continue

        print_repair_result(result)

    return exit_code


def summarize_annotated_import_decision(decision: AnnotatedImportDecision) -> str:
    line = f"[{decision.status.upper()}] {decision.export_path}"
    if decision.source_visible_name:
        line += f" -> {decision.source_visible_name}"
    if decision.imported_attachment_key:
        line += f" imported_attachment_key={decision.imported_attachment_key}"
    return f"{line}\n    {decision.reason}"


def summarize_return_sync_decision(decision: ReturnSyncDecision) -> str:
    line = f"[{decision.status.upper()}] {decision.source_visible_name}"
    if decision.remote_uuid:
        line += f" remote_uuid={decision.remote_uuid}"
    if decision.imported_attachment_key:
        line += f" imported_attachment_key={decision.imported_attachment_key}"
    return f"{line}\n    {decision.reason}"


def print_return_sync_report(report: ReturnSyncReport) -> None:
    print("reMarkable -> Zotero")
    if not report.decisions:
        print("No tracked reMarkable documents were available for return sync.")
    else:
        counts: dict[str, int] = {}
        for decision in report.decisions:
            counts[decision.status] = counts.get(decision.status, 0) + 1
        if counts:
            summary = ", ".join(
                f"{status}={count}" for status, count in sorted(counts.items())
            )
            print(f"Summary: {summary}")

        for decision in report.decisions:
            print(summarize_return_sync_decision(decision))

    if report.dry_run:
        print("Dry run only for reMarkable -> Zotero.")
    else:
        if report.db_backup_path is not None:
            print(f"Zotero DB backup: {report.db_backup_path}")
        print(f"Imported {len(report.imported)} annotated PDF(s) back into Zotero.")

    if report.errors:
        print("Errors:")
        for error in report.errors:
            print(f"- {error}")


def print_roundtrip_sync_report(report: RoundTripSyncReport) -> None:
    print("Zotero -> reMarkable")
    print_sync_report(report.forward)
    print()
    print_return_sync_report(report.reverse)


def print_annotated_import_report(report: AnnotatedImportReport) -> None:
    if not report.decisions:
        print("No annotated PDFs were found to process.")
    else:
        print(f"Annotated import candidates: {len(report.decisions)}")
        counts: dict[str, int] = {}
        for decision in report.decisions:
            counts[decision.status] = counts.get(decision.status, 0) + 1

        if counts:
            summary = ", ".join(
                f"{status}={count}" for status, count in sorted(counts.items())
            )
            print(f"Summary: {summary}")

        for decision in report.decisions:
            print(summarize_annotated_import_decision(decision))

    if report.dry_run:
        print("\nDry run only. Re-run with --apply to import changed annotated PDFs.")
    else:
        if report.db_backup_path is not None:
            print(f"\nZotero DB backup: {report.db_backup_path}")
        print(f"Imported {len(report.imported)} annotated PDF(s).")

    if report.errors:
        print("\nErrors:")
        for error in report.errors:
            print(f"- {error}")


def run_import_annotated(args: argparse.Namespace) -> int:
    from remarkable_zotero_sync.annotated_import import AnnotatedImportService

    config_path = resolve_config_path(args.config)
    ensure_public_config(config_path)
    config = load_config(config_path)
    service = AnnotatedImportService(config, progress=console_progress)
    report = (
        service.execute(raw_paths=args.pdfs)
        if args.apply
        else service.build_report(raw_paths=args.pdfs, dry_run=True)
    )
    print_annotated_import_report(report)
    return 1 if report.errors else 0


def main(argv: list[str] | None = None) -> int:
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    if not effective_argv:
        effective_argv = ["sync"]
    elif effective_argv[0].startswith("-") and effective_argv[0] not in {"-h", "--help"}:
        effective_argv = ["sync", *effective_argv]

    parser = build_parser()
    args = parser.parse_args(effective_argv)

    if args.command == "sync":
        return run_sync(args)
    if args.command == "import-annotated":
        return run_import_annotated(args)
    if args.command == "repair-highlights":
        return run_repair_highlights(args)

    parser.error(f"Unknown command: {args.command}")
    return 2
