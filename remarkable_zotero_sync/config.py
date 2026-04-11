from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ZoteroConfig:
    db_path: Path
    storage_dir: Path
    collection_name: str
    library_scope: str
    linked_attachment_base_dir: Path | None


@dataclass(frozen=True)
class RemarkableConfig:
    host: str
    user: str
    password: str
    xochitl_dir: str
    metadata_cache_dir: Path
    folder_uuid: str
    folder_path: str
    tag_metadata_field: str
    restart_xochitl_after_sync: bool
    web_download_timeout_seconds: int
    ssh_connect_timeout_seconds: int
    xochitl_ready_timeout_seconds: int
    xochitl_settle_seconds: int


@dataclass(frozen=True)
class SyncConfig:
    state_path: Path
    upload_changed_as_new_copy: bool
    persist_author_tags_to_zotero: bool


@dataclass(frozen=True)
class AnnotatedImportConfig:
    inbox_dir: Path
    downloaded_dir: Path
    repaired_dir: Path
    state_path: Path
    replace_source_attachment: bool
    attachment_title: str
    require_zotero_closed: bool
    db_backup_dir: Path


@dataclass(frozen=True)
class AppConfig:
    raw_path: Path
    zotero: ZoteroConfig
    remarkable: RemarkableConfig
    sync: SyncConfig
    annotated_import: AnnotatedImportConfig


def _resolve_local_path(raw_value: str, base_dir: Path) -> Path:
    path = Path(raw_value).expanduser()
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return path


def _resolve_optional_local_path(raw_value: str, base_dir: Path) -> Path | None:
    if not raw_value:
        return None
    return _resolve_local_path(raw_value, base_dir)


def load_config(
    path: str | Path,
    *,
    password_override: str | None = None,
) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}. Run the script once to create a "
            "starter config from config.template.toml."
        )

    with config_path.open("rb") as handle:
        data = tomllib.load(handle)

    base_dir = config_path.parent
    zotero_data = data.get("zotero", {})
    remarkable_data = data.get("remarkable", {})
    sync_data = data.get("sync", {})
    annotated_import_data = data.get("annotated_import", {})

    password = (
        password_override
        if password_override is not None
        else remarkable_data.get("password", "")
    )

    zotero = ZoteroConfig(
        db_path=_resolve_local_path(zotero_data["db_path"], base_dir),
        storage_dir=_resolve_local_path(zotero_data["storage_dir"], base_dir),
        collection_name=zotero_data.get("collection_name", "reMarkable Sync"),
        library_scope=str(zotero_data.get("library_scope", "collection")).strip().lower()
        or "collection",
        linked_attachment_base_dir=_resolve_optional_local_path(
            zotero_data.get("linked_attachment_base_dir", ""),
            base_dir,
        ),
    )

    if zotero.library_scope not in {"collection", "library"}:
        raise ValueError(
            "zotero.library_scope must be either 'collection' or 'library'."
        )

    remarkable = RemarkableConfig(
        host=remarkable_data.get("host", "10.11.99.1"),
        user=remarkable_data.get("user", "root"),
        password=password,
        xochitl_dir=remarkable_data.get(
            "xochitl_dir",
            "/home/root/.local/share/remarkable/xochitl",
        ),
        metadata_cache_dir=_resolve_local_path(
            remarkable_data.get("metadata_cache_dir", ".cache/remarkable_metadata"),
            base_dir,
        ),
        folder_uuid=remarkable_data.get("folder_uuid", "").strip(),
        folder_path=remarkable_data.get("folder_path", "/").strip() or "/",
        tag_metadata_field=remarkable_data.get("tag_metadata_field", "tags").strip(),
        restart_xochitl_after_sync=bool(
            remarkable_data.get("restart_xochitl_after_sync", True)
        ),
        web_download_timeout_seconds=int(
            remarkable_data.get("web_download_timeout_seconds", 120)
        ),
        ssh_connect_timeout_seconds=int(
            remarkable_data.get("ssh_connect_timeout_seconds", 10)
        ),
        xochitl_ready_timeout_seconds=int(
            remarkable_data.get("xochitl_ready_timeout_seconds", 45)
        ),
        xochitl_settle_seconds=int(
            remarkable_data.get("xochitl_settle_seconds", 8)
        ),
    )

    sync = SyncConfig(
        state_path=_resolve_local_path(
            sync_data.get("state_path", ".cache/sync_state.json"),
            base_dir,
        ),
        upload_changed_as_new_copy=bool(
            sync_data.get("upload_changed_as_new_copy", True)
        ),
        persist_author_tags_to_zotero=bool(
            sync_data.get("persist_author_tags_to_zotero", True)
        ),
    )

    annotated_import = AnnotatedImportConfig(
        inbox_dir=_resolve_local_path(
            annotated_import_data.get("inbox_dir", "annotated_exports/inbox"),
            base_dir,
        ),
        downloaded_dir=_resolve_local_path(
            annotated_import_data.get("downloaded_dir", "annotated_exports/from_device"),
            base_dir,
        ),
        repaired_dir=_resolve_local_path(
            annotated_import_data.get("repaired_dir", "annotated_exports/repaired"),
            base_dir,
        ),
        state_path=_resolve_local_path(
            annotated_import_data.get(
                "state_path",
                ".cache/annotated_import_state.json",
            ),
            base_dir,
        ),
        replace_source_attachment=bool(
            annotated_import_data.get("replace_source_attachment", True)
        ),
        attachment_title=annotated_import_data.get(
            "attachment_title",
            "reMarkable Annotated PDF",
        ).strip()
        or "reMarkable Annotated PDF",
        require_zotero_closed=bool(
            annotated_import_data.get("require_zotero_closed", True)
        ),
        db_backup_dir=_resolve_local_path(
            annotated_import_data.get(
                "db_backup_dir",
                ".cache/zotero_db_backups",
            ),
            base_dir,
        ),
    )

    return AppConfig(
        raw_path=config_path,
        zotero=zotero,
        remarkable=remarkable,
        sync=sync,
        annotated_import=annotated_import,
    )
