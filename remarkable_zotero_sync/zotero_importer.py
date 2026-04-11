from __future__ import annotations

import hashlib
import os
import random
import re
import shutil
import sqlite3
import string
import subprocess
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from remarkable_zotero_sync.config import AnnotatedImportConfig, ZoteroConfig
from remarkable_zotero_sync.models import ImportedAttachmentResult

ATTACHMENT_ITEM_TYPE_ID = 3
STORED_FILE_LINK_MODE = 1
ZOTERO_KEY_CHARS = "23456789ABCDEFGHIJKLMNPQRSTUVWXYZ"


class ZoteroImporter:
    def __init__(self, zotero_config: ZoteroConfig, import_config: AnnotatedImportConfig):
        self.zotero_config = zotero_config
        self.import_config = import_config
        self._backup_created = False

    def ensure_safe_to_write(self) -> None:
        if not self.import_config.require_zotero_closed:
            return
        if self.is_zotero_running():
            raise RuntimeError(
                "Zotero appears to be running. Close Zotero before importing "
                "annotated PDFs so the local database can be updated safely."
            )

    @staticmethod
    def is_zotero_running() -> bool:
        try:
            result = subprocess.run(
                ["pgrep", "-af", "Zotero"],
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            return False
        if result.returncode != 0 or not result.stdout.strip():
            return False

        for line in result.stdout.splitlines():
            command = line.strip()
            if not command:
                continue
            if ".appex/" in command or "ZoteroSafariExtension" in command:
                continue
            if re.search(r"Zotero\.app/Contents/MacOS/(?:zotero|Zotero)(?:\s|$)", command):
                return True

        return False

    def backup_database_bundle(self) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = self.import_config.db_backup_dir / timestamp
        backup_dir.mkdir(parents=True, exist_ok=True)

        source_db = self.zotero_config.db_path
        if not source_db.exists():
            raise FileNotFoundError(f"Zotero database not found: {source_db}")

        shutil.copy2(source_db, backup_dir / source_db.name)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{source_db}{suffix}")
            if sidecar.exists():
                shutil.copy2(sidecar, backup_dir / sidecar.name)

        self._backup_created = True
        return backup_dir

    def import_stored_pdf(
        self,
        *,
        parent_item_key: str,
        source_pdf_path: Path,
        attachment_title: str,
        source_attachment_key: str,
    ) -> ImportedAttachmentResult:
        self.ensure_safe_to_write()
        if not source_pdf_path.exists():
            raise FileNotFoundError(f"Annotated PDF not found: {source_pdf_path}")
        self._ensure_valid_pdf(source_pdf_path)

        parent_lookup = self._lookup_parent_item(parent_item_key)
        attachment_key = self._generate_unique_key(parent_lookup["libraryID"])
        storage_dir = self.zotero_config.storage_dir / attachment_key
        storage_dir.mkdir(parents=True, exist_ok=False)

        filename = self._safe_filename(source_pdf_path.name)
        destination_path = storage_dir / filename

        try:
            shutil.copy2(source_pdf_path, destination_path)
            storage_hash = self._md5_file(destination_path)
            storage_mod_time = int(destination_path.stat().st_mtime * 1000)

            with sqlite3.connect(self.zotero_config.db_path, timeout=30) as conn:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("BEGIN IMMEDIATE")
                item_id = self._insert_item(
                    conn=conn,
                    library_id=int(parent_lookup["libraryID"]),
                    item_key=attachment_key,
                )
                self._insert_attachment(
                    conn=conn,
                    item_id=item_id,
                    parent_item_id=int(parent_lookup["itemID"]),
                    path=f"storage:{filename}",
                    storage_mod_time=storage_mod_time,
                    storage_hash=storage_hash,
                )
                self._set_item_title(
                    conn=conn,
                    item_id=item_id,
                    title=attachment_title,
                )
                conn.commit()
        except Exception:
            shutil.rmtree(storage_dir, ignore_errors=True)
            raise

        return ImportedAttachmentResult(
            source_attachment_key=source_attachment_key,
            parent_item_key=parent_item_key,
            imported_attachment_key=attachment_key,
            attachment_title=attachment_title,
            source_path=source_pdf_path,
            zotero_storage_path=destination_path,
        )

    def replace_attachment_pdf(
        self,
        *,
        source_attachment_key: str,
        source_pdf_path: Path,
    ) -> ImportedAttachmentResult:
        return self.replace_attachment_pdfs(
            source_attachment_keys=[source_attachment_key],
            source_pdf_path=source_pdf_path,
        )[0]

    def replace_attachment_pdfs(
        self,
        *,
        source_attachment_keys: list[str],
        source_pdf_path: Path,
    ) -> list[ImportedAttachmentResult]:
        self.ensure_safe_to_write()
        if not source_pdf_path.exists():
            raise FileNotFoundError(f"Annotated PDF not found: {source_pdf_path}")
        self._ensure_valid_pdf(source_pdf_path)

        ordered_keys: list[str] = []
        seen: set[str] = set()
        for raw_key in source_attachment_keys:
            key = raw_key.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            ordered_keys.append(key)
        if not ordered_keys:
            raise ValueError("No Zotero attachment keys were provided for replacement.")

        results: list[ImportedAttachmentResult] = []
        for attachment_key in ordered_keys:
            attachment_lookup = self._lookup_attachment_item(attachment_key)
            results.append(
                self._replace_attachment_pdf_with_lookup(
                    attachment_lookup=attachment_lookup,
                    source_pdf_path=source_pdf_path,
                )
            )
        return results

    def _replace_attachment_pdf_with_lookup(
        self,
        *,
        attachment_lookup: sqlite3.Row,
        source_pdf_path: Path,
    ) -> ImportedAttachmentResult:
        source_attachment_key = str(attachment_lookup["key"])
        destination_path = self._resolve_attachment_file_path(
            attachment_key=source_attachment_key,
            attachment_path=str(attachment_lookup["path"] or ""),
        )
        destination_path.parent.mkdir(parents=True, exist_ok=True)

        temp_path = destination_path.with_name(f"{destination_path.name}.tmp-rm-sync")
        if temp_path.exists():
            temp_path.unlink()

        try:
            shutil.copy2(source_pdf_path, temp_path)
            os.replace(temp_path, destination_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

        storage_hash = self._md5_file(destination_path)
        storage_mod_time = int(destination_path.stat().st_mtime * 1000)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        with sqlite3.connect(self.zotero_config.db_path, timeout=30) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE itemAttachments
                SET syncState = 0,
                    storageModTime = ?,
                    storageHash = ?,
                    lastProcessedModificationTime = NULL
                WHERE itemID = ?
                """,
                (
                    storage_mod_time,
                    storage_hash,
                    int(attachment_lookup["itemID"]),
                ),
            )
            conn.execute(
                """
                UPDATE items
                SET dateModified = ?,
                    clientDateModified = ?,
                    synced = 0
                WHERE itemID = ?
                """,
                (
                    timestamp,
                    timestamp,
                    int(attachment_lookup["itemID"]),
                ),
            )
            conn.commit()

        return ImportedAttachmentResult(
            source_attachment_key=source_attachment_key,
            parent_item_key=str(attachment_lookup["parent_item_key"] or ""),
            imported_attachment_key=source_attachment_key,
            attachment_title=self._safe_filename(destination_path.name),
            source_path=source_pdf_path,
            zotero_storage_path=destination_path,
        )

    @staticmethod
    def normalize_title_match(text: str) -> str:
        normalized = unicodedata.normalize("NFKC", text)
        normalized = normalized.casefold()
        normalized = re.sub(r"[^0-9a-z]+", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized

    def matching_pdf_attachment_paths_for_parent_title(
        self,
        parent_title: str,
    ) -> dict[str, Path]:
        normalized_target = self.normalize_title_match(parent_title)
        if not normalized_target:
            return {}

        with sqlite3.connect(self.zotero_config.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT
                    attachment_item.key AS attachment_key,
                    attachment.path AS attachment_path,
                    title_value.value AS parent_title
                FROM itemAttachments AS attachment
                JOIN items AS attachment_item ON attachment_item.itemID = attachment.itemID
                JOIN items AS parent_item ON parent_item.itemID = attachment.parentItemID
                JOIN itemData AS parent_data ON parent_data.itemID = parent_item.itemID
                JOIN fields ON fields.fieldID = parent_data.fieldID
                JOIN itemDataValues AS title_value ON title_value.valueID = parent_data.valueID
                WHERE lower(COALESCE(attachment.contentType, '')) = 'application/pdf'
                  AND fields.fieldName = 'title'
                  AND NOT EXISTS (
                        SELECT 1
                        FROM deletedItems AS deleted_attachment
                        WHERE deleted_attachment.itemID = attachment.itemID
                  )
                ORDER BY attachment_item.key
                """
            ).fetchall()

        matches: dict[str, Path] = {}
        for row in rows:
            row_title = str(row["parent_title"] or "")
            if self.normalize_title_match(row_title) != normalized_target:
                continue

            attachment_key = str(row["attachment_key"] or "").strip()
            attachment_path = str(row["attachment_path"] or "")
            if not attachment_key:
                continue

            try:
                resolved_path = self._resolve_attachment_file_path(
                    attachment_key=attachment_key,
                    attachment_path=attachment_path,
                )
            except Exception:
                continue
            matches[attachment_key] = resolved_path

        return matches

    def missing_item_tags(
        self,
        *,
        item_key: str,
        desired_tags: list[str],
    ) -> list[str]:
        if not desired_tags:
            return []

        item_lookup = self._lookup_item_by_key(item_key)
        with sqlite3.connect(self.zotero_config.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT tags.name
                FROM itemTags
                JOIN tags ON tags.tagID = itemTags.tagID
                WHERE itemTags.itemID = ?
                ORDER BY lower(tags.name), tags.name
                """,
                (int(item_lookup["itemID"]),),
            ).fetchall()

        existing = {
            str(row["name"]).strip().casefold()
            for row in rows
            if row["name"]
        }
        missing: list[str] = []
        queued: set[str] = set()
        for raw_tag in desired_tags:
            tag = raw_tag.strip()
            if not tag:
                continue
            folded = tag.casefold()
            if folded in existing or folded in queued:
                continue
            queued.add(folded)
            missing.append(tag)
        return missing

    def add_item_tags(
        self,
        *,
        item_key: str,
        desired_tags: list[str],
    ) -> list[str]:
        missing = self.missing_item_tags(item_key=item_key, desired_tags=desired_tags)
        if not missing:
            return []

        self.ensure_safe_to_write()
        item_lookup = self._lookup_item_by_key(item_key)
        item_id = int(item_lookup["itemID"])
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        with sqlite3.connect(self.zotero_config.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE")
            for tag in missing:
                conn.execute(
                    """
                    INSERT INTO tags (name)
                    SELECT ?
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM tags
                        WHERE lower(name) = lower(?)
                    )
                    """,
                    (tag, tag),
                )
                tag_row = conn.execute(
                    """
                    SELECT tagID
                    FROM tags
                    WHERE lower(name) = lower(?)
                    ORDER BY tagID
                    LIMIT 1
                    """,
                    (tag,),
                ).fetchone()
                if tag_row is None:
                    raise RuntimeError(f'Could not resolve Zotero tag "{tag}".')
                conn.execute(
                    """
                    INSERT OR IGNORE INTO itemTags (itemID, tagID, type)
                    VALUES (?, ?, 0)
                    """,
                    (item_id, int(tag_row["tagID"])),
                )

            conn.execute(
                """
                UPDATE items
                SET dateModified = ?,
                    clientDateModified = ?,
                    synced = 0
                WHERE itemID = ?
                """,
                (timestamp, timestamp, item_id),
            )
            conn.commit()

        return missing

    def _lookup_parent_item(self, parent_item_key: str) -> sqlite3.Row:
        with sqlite3.connect(self.zotero_config.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT itemID, libraryID, key
                FROM items
                WHERE key = ?
                LIMIT 1
                """,
                (parent_item_key,),
            ).fetchone()

        if row is None:
            raise ValueError(f'Parent Zotero item key "{parent_item_key}" was not found.')
        return row

    def _lookup_attachment_item(self, attachment_key: str) -> sqlite3.Row:
        with sqlite3.connect(self.zotero_config.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT
                    attachment.itemID,
                    attachment.parentItemID,
                    attachment.linkMode,
                    attachment.path,
                    attachment.contentType,
                    attachment.storageModTime,
                    attachment.storageHash,
                    item.key,
                    parent.key AS parent_item_key
                FROM itemAttachments AS attachment
                JOIN items AS item ON item.itemID = attachment.itemID
                LEFT JOIN items AS parent ON parent.itemID = attachment.parentItemID
                WHERE item.key = ?
                LIMIT 1
                """,
                (attachment_key,),
            ).fetchone()

        if row is None:
            raise ValueError(f'Zotero attachment key "{attachment_key}" was not found.')
        if str(row["contentType"] or "").casefold() != "application/pdf":
            raise ValueError(
                f'Zotero attachment key "{attachment_key}" is not a PDF attachment.'
            )
        return row

    def _lookup_item_by_key(self, item_key: str) -> sqlite3.Row:
        with sqlite3.connect(self.zotero_config.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT itemID, key
                FROM items
                WHERE key = ?
                LIMIT 1
                """,
                (item_key,),
            ).fetchone()

        if row is None:
            raise ValueError(f'Zotero item key "{item_key}" was not found.')
        return row

    def _generate_unique_key(self, library_id: int) -> str:
        with sqlite3.connect(self.zotero_config.db_path, timeout=30) as conn:
            for _ in range(100):
                key = "".join(random.choice(ZOTERO_KEY_CHARS) for _ in range(8))
                existing = conn.execute(
                    "SELECT 1 FROM items WHERE libraryID = ? AND key = ?",
                    (library_id, key),
                ).fetchone()
                if existing is None:
                    return key
        raise RuntimeError("Failed to generate a unique Zotero item key.")

    @staticmethod
    def _insert_item(
        *,
        conn: sqlite3.Connection,
        library_id: int,
        item_key: str,
    ) -> int:
        cursor = conn.execute(
            """
            INSERT INTO items (itemTypeID, libraryID, key, version, synced)
            VALUES (?, ?, ?, 0, 0)
            """,
            (ATTACHMENT_ITEM_TYPE_ID, library_id, item_key),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _insert_attachment(
        *,
        conn: sqlite3.Connection,
        item_id: int,
        parent_item_id: int,
        path: str,
        storage_mod_time: int,
        storage_hash: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO itemAttachments (
                itemID,
                parentItemID,
                linkMode,
                contentType,
                path,
                syncState,
                storageModTime,
                storageHash
            )
            VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                item_id,
                parent_item_id,
                STORED_FILE_LINK_MODE,
                "application/pdf",
                path,
                storage_mod_time,
                storage_hash,
            ),
        )

    @staticmethod
    def _set_item_title(
        *,
        conn: sqlite3.Connection,
        item_id: int,
        title: str,
    ) -> None:
        title_field_row = conn.execute(
            "SELECT fieldID FROM fields WHERE fieldName = 'title' LIMIT 1"
        ).fetchone()
        if title_field_row is None:
            raise RuntimeError("Could not find Zotero title field ID.")

        title_field_id = int(title_field_row[0])
        conn.execute(
            "INSERT OR IGNORE INTO itemDataValues (value) VALUES (?)",
            (title,),
        )
        value_row = conn.execute(
            "SELECT valueID FROM itemDataValues WHERE value = ? LIMIT 1",
            (title,),
        ).fetchone()
        if value_row is None:
            raise RuntimeError("Could not create Zotero itemDataValues row for title.")

        conn.execute(
            """
            INSERT INTO itemData (itemID, fieldID, valueID)
            VALUES (?, ?, ?)
            """,
            (item_id, title_field_id, int(value_row[0])),
        )

    @staticmethod
    def _safe_filename(filename: str) -> str:
        cleaned = filename.replace("/", "-").replace("\0", "")
        if not cleaned.lower().endswith(".pdf"):
            cleaned = f"{cleaned}.pdf"
        return cleaned

    def _resolve_attachment_file_path(
        self,
        *,
        attachment_key: str,
        attachment_path: str,
    ) -> Path:
        if not attachment_path:
            raise ValueError(
                f"Attachment {attachment_key} does not have a local path in Zotero."
            )

        if attachment_path.startswith("storage:"):
            relative_name = attachment_path.split(":", 1)[1]
            return (self.zotero_config.storage_dir / attachment_key / relative_name).resolve()

        if attachment_path.startswith("attachments:"):
            if self.zotero_config.linked_attachment_base_dir is None:
                raise ValueError(
                    f"Attachment {attachment_key} uses an attachments: path but "
                    "linked_attachment_base_dir is not configured."
                )
            relative_path = attachment_path.split(":", 1)[1]
            return (
                self.zotero_config.linked_attachment_base_dir / relative_path
            ).resolve()

        if attachment_path.startswith("file://"):
            parsed = urlparse(attachment_path)
            return Path(unquote(parsed.path)).expanduser().resolve()

        direct_path = Path(attachment_path).expanduser()
        if direct_path.is_absolute():
            return direct_path.resolve()

        if self.zotero_config.linked_attachment_base_dir is not None:
            return (
                self.zotero_config.linked_attachment_base_dir / direct_path
            ).resolve()

        raise ValueError(
            f"Attachment {attachment_key} uses a relative linked path but "
            "linked_attachment_base_dir is not configured."
        )

    @staticmethod
    def _md5_file(path: Path) -> str:
        digest = hashlib.md5()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _ensure_valid_pdf(path: Path) -> None:
        try:
            if zipfile.is_zipfile(path):
                raise RuntimeError(
                    f"{path.name} is a ZIP archive, not a PDF. "
                    "This usually means the reMarkable returned an rmdoc package instead "
                    "of an exported PDF."
                )
            with path.open("rb") as handle:
                if handle.read(5) != b"%PDF-":
                    raise RuntimeError(
                        f"{path.name} does not appear to be a valid PDF file."
                    )
        except OSError as exc:
            raise RuntimeError(f"Could not validate imported PDF {path}: {exc}") from exc
