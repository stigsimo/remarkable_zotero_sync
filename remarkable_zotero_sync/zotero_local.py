from __future__ import annotations

import re
import shutil
import sqlite3
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from remarkable_zotero_sync.config import ZoteroConfig
from remarkable_zotero_sync.models import ZoteroAttachment


@dataclass(frozen=True)
class PreparedAttachmentCandidate:
    source_item_id: int
    attachment_item_id: int
    logical_attachment_key: str
    selected_attachment_key: str
    parent_item_id: int | None
    parent_item_key: str | None
    parent_title: str
    attachment_title: str | None
    resolved_path: Path | None
    original_filename: str
    tags: list[str]
    source_kind: str
    collection_path_parts: tuple[str, ...]
    routing_error: str | None
    date_added: str
    date_modified: str
    pdf_is_valid: bool


class ZoteroLibrary:
    def __init__(self, config: ZoteroConfig):
        self.config = config
        self._temp_dir: tempfile.TemporaryDirectory[str] | None = None
        self._db_path: Path | None = None
        self._conn: sqlite3.Connection | None = None

    def __enter__(self) -> "ZoteroLibrary":
        self._temp_dir = tempfile.TemporaryDirectory(prefix="zotero-sync-")
        temp_dir_path = Path(self._temp_dir.name)
        self._db_path = temp_dir_path / self.config.db_path.name
        self._copy_database_bundle(self.config.db_path, self._db_path)

        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._temp_dir is not None:
            self._temp_dir.cleanup()
            self._temp_dir = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("ZoteroLibrary connection has not been opened yet.")
        return self._conn

    @staticmethod
    def _copy_database_bundle(source_db: Path, destination_db: Path) -> None:
        if not source_db.exists():
            raise FileNotFoundError(f"Zotero database not found: {source_db}")

        shutil.copy2(source_db, destination_db)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{source_db}{suffix}")
            if sidecar.exists():
                shutil.copy2(sidecar, Path(f"{destination_db}{suffix}"))

    def get_collection_id(self, collection_name: str) -> int:
        rows = self.conn.execute(
            """
            SELECT collectionID, libraryID, key, parentCollectionID
            FROM collections
            WHERE collectionName = ?
            ORDER BY collectionID
            """,
            (collection_name,),
        ).fetchall()

        if not rows:
            raise ValueError(f'No Zotero collection named "{collection_name}" was found.')

        if len(rows) > 1:
            raise ValueError(
                f'More than one Zotero collection named "{collection_name}" was found. '
                "This first version expects the collection name to be unique."
            )

        return int(rows[0]["collectionID"])

    def source_scope_label(self) -> str:
        if self.config.library_scope == "library":
            return "Entire Zotero library"
        return self.config.collection_name

    def list_sync_target_pdfs(
        self,
        *,
        excluded_attachment_titles: set[str] | None = None,
        imported_attachment_source_keys: dict[str, str] | None = None,
    ) -> list[ZoteroAttachment]:
        if self.config.library_scope == "library":
            return self.list_library_pdfs(
                excluded_attachment_titles=excluded_attachment_titles,
                imported_attachment_source_keys=imported_attachment_source_keys,
            )
        return self.list_collection_pdfs(
            self.config.collection_name,
            excluded_attachment_titles=excluded_attachment_titles,
            imported_attachment_source_keys=imported_attachment_source_keys,
        )

    def list_collection_pdfs(
        self,
        collection_name: str,
        *,
        excluded_attachment_titles: set[str] | None = None,
        imported_attachment_source_keys: dict[str, str] | None = None,
    ) -> list[ZoteroAttachment]:
        _, collection_paths = self.get_collection_subtree(collection_name)
        descendant_collection_ids = sorted(collection_paths)
        rows = self.fetch_pdf_attachment_rows(collection_ids=descendant_collection_ids)

        return self.prepare_pdf_attachments(
            rows=rows,
            excluded_attachment_titles=excluded_attachment_titles,
            imported_attachment_source_keys=imported_attachment_source_keys,
            collection_ids=descendant_collection_ids,
            collection_paths=collection_paths,
            root_collection_name=collection_name,
            require_membership_path=True,
        )

    def list_library_pdfs(
        self,
        *,
        excluded_attachment_titles: set[str] | None = None,
        imported_attachment_source_keys: dict[str, str] | None = None,
    ) -> list[ZoteroAttachment]:
        collection_paths = self.get_all_collection_paths()
        rows = self.fetch_pdf_attachment_rows(collection_ids=None)

        return self.prepare_pdf_attachments(
            rows=rows,
            excluded_attachment_titles=excluded_attachment_titles,
            imported_attachment_source_keys=imported_attachment_source_keys,
            collection_ids=sorted(collection_paths),
            collection_paths=collection_paths,
            root_collection_name=None,
            require_membership_path=False,
        )

    def fetch_pdf_attachment_rows(
        self,
        *,
        collection_ids: list[int] | None,
    ) -> list[sqlite3.Row]:
        if collection_ids is None:
            return self.conn.execute(
                """
                SELECT DISTINCT
                    attachment.itemID AS attachment_item_id,
                    attachment.parentItemID AS parent_item_id,
                    attachment.linkMode AS link_mode,
                    attachment.path AS attachment_path,
                    attachment.contentType AS content_type,
                    attachment_item.key AS attachment_key,
                    attachment_item.dateAdded AS attachment_date_added,
                    attachment_item.dateModified AS attachment_date_modified,
                    parent_item.key AS parent_item_key
                FROM itemAttachments AS attachment
                JOIN items AS attachment_item ON attachment_item.itemID = attachment.itemID
                LEFT JOIN items AS parent_item ON parent_item.itemID = attachment.parentItemID
                WHERE lower(COALESCE(attachment.contentType, '')) = 'application/pdf'
                  AND NOT EXISTS (
                        SELECT 1
                        FROM deletedItems AS deleted_attachment
                        WHERE deleted_attachment.itemID = attachment.itemID
                  )
                ORDER BY COALESCE(attachment.parentItemID, attachment.itemID), attachment.itemID
                """
            ).fetchall()

        collection_placeholders = ", ".join("?" for _ in collection_ids)
        return self.conn.execute(
            f"""
            SELECT DISTINCT
                attachment.itemID AS attachment_item_id,
                attachment.parentItemID AS parent_item_id,
                attachment.linkMode AS link_mode,
                attachment.path AS attachment_path,
                attachment.contentType AS content_type,
                attachment_item.key AS attachment_key,
                attachment_item.dateAdded AS attachment_date_added,
                attachment_item.dateModified AS attachment_date_modified,
                parent_item.key AS parent_item_key
            FROM itemAttachments AS attachment
            JOIN items AS attachment_item ON attachment_item.itemID = attachment.itemID
            LEFT JOIN items AS parent_item ON parent_item.itemID = attachment.parentItemID
            WHERE lower(COALESCE(attachment.contentType, '')) = 'application/pdf'
              AND NOT EXISTS (
                    SELECT 1
                    FROM deletedItems AS deleted_attachment
                    WHERE deleted_attachment.itemID = attachment.itemID
              )
              AND (
                    attachment.itemID IN (
                        SELECT itemID
                        FROM collectionItems
                        WHERE collectionID IN ({collection_placeholders})
                    )
                    OR attachment.parentItemID IN (
                        SELECT itemID
                        FROM collectionItems
                        WHERE collectionID IN ({collection_placeholders})
                    )
              )
            ORDER BY COALESCE(attachment.parentItemID, attachment.itemID), attachment.itemID
            """,
            tuple(collection_ids) + tuple(collection_ids),
        ).fetchall()

    def prepare_pdf_attachments(
        self,
        *,
        rows: list[sqlite3.Row],
        excluded_attachment_titles: set[str] | None,
        imported_attachment_source_keys: dict[str, str] | None,
        collection_ids: list[int],
        collection_paths: dict[int, tuple[str, ...]],
        root_collection_name: str | None,
        require_membership_path: bool,
    ) -> list[ZoteroAttachment]:

        if not rows:
            return []

        excluded_titles = {
            title.strip()
            for title in (excluded_attachment_titles or set())
            if title and title.strip()
        }

        enriched_rows: list[tuple[sqlite3.Row, str | None]] = []
        for row in rows:
            attachment_item_id = int(row["attachment_item_id"])
            attachment_title = self.get_item_title(attachment_item_id).strip() or None
            if attachment_title in excluded_titles:
                continue
            enriched_rows.append((row, attachment_title))

        if not enriched_rows:
            return []

        imported_source_keys = dict(imported_attachment_source_keys or {})
        relevant_item_ids = {
            int(row["attachment_item_id"])
            for row, _ in enriched_rows
        }
        relevant_item_ids.update(
            int(row["parent_item_id"])
            for row, _ in enriched_rows
            if row["parent_item_id"] is not None
        )
        membership_map = self.get_item_collection_memberships(
            collection_ids=collection_ids,
            item_ids=relevant_item_ids,
        )

        grouped_candidates: dict[str, list[PreparedAttachmentCandidate]] = {}
        for row, attachment_title in enriched_rows:
            attachment_item_id = int(row["attachment_item_id"])
            parent_item_id = row["parent_item_id"]
            source_item_id = int(parent_item_id or attachment_item_id)
            selected_attachment_key = str(row["attachment_key"])
            logical_attachment_key = imported_source_keys.get(
                selected_attachment_key,
                selected_attachment_key,
            )
            parent_item_key = row["parent_item_key"]
            parent_title = self.get_item_title(source_item_id).strip()
            tags = self.get_item_tags(source_item_id)
            collection_path_parts, routing_error = self.resolve_attachment_collection_path(
                attachment_item_id=attachment_item_id,
                parent_item_id=int(parent_item_id) if parent_item_id is not None else None,
                membership_map=membership_map,
                collection_paths=collection_paths,
                root_collection_name=root_collection_name,
                require_membership_path=require_membership_path,
            )

            resolved_path: Path | None = None
            original_filename = ""
            pdf_is_valid = False
            try:
                resolved_path = self.resolve_attachment_path(
                    attachment_key=selected_attachment_key,
                    attachment_path=row["attachment_path"],
                )
                original_filename = resolved_path.name
                pdf_is_valid, pdf_error = self.validate_pdf_file(resolved_path)
                if pdf_error:
                    routing_error = self.combine_error_messages(routing_error, pdf_error)
            except Exception as exc:
                original_filename = self.attachment_display_name(
                    selected_attachment_key=selected_attachment_key,
                    attachment_path=row["attachment_path"],
                )
                routing_error = self.combine_error_messages(routing_error, str(exc))

            grouped_candidates.setdefault(logical_attachment_key, []).append(
                PreparedAttachmentCandidate(
                    source_item_id=source_item_id,
                    attachment_item_id=attachment_item_id,
                    logical_attachment_key=logical_attachment_key,
                    selected_attachment_key=selected_attachment_key,
                    parent_item_id=int(parent_item_id) if parent_item_id is not None else None,
                    parent_item_key=str(parent_item_key) if parent_item_key is not None else None,
                    parent_title=parent_title,
                    attachment_title=attachment_title,
                    resolved_path=resolved_path,
                    original_filename=original_filename,
                    tags=tags,
                    source_kind=self.attachment_source_kind(row["attachment_path"]),
                    collection_path_parts=collection_path_parts,
                    routing_error=routing_error,
                    date_added=str(row["attachment_date_added"] or ""),
                    date_modified=str(row["attachment_date_modified"] or ""),
                    pdf_is_valid=pdf_is_valid,
                )
            )

        selected_candidates = [
            self.pick_preferred_candidate(candidates)
            for _, candidates in sorted(grouped_candidates.items())
        ]

        source_counts = Counter(
            candidate.source_item_id
            for candidate in selected_candidates
        )

        attachments: list[ZoteroAttachment] = []
        for candidate in selected_candidates:
            resolved_path = candidate.resolved_path or Path(
                candidate.original_filename or candidate.selected_attachment_key
            )
            visible_name = self.build_visible_name(
                parent_title=(
                    candidate.parent_title
                    or resolved_path.stem
                    or candidate.logical_attachment_key
                ),
                original_filename=candidate.original_filename or resolved_path.name,
                attachment_count=source_counts[candidate.source_item_id],
            )

            attachments.append(
                ZoteroAttachment(
                    attachment_item_id=candidate.attachment_item_id,
                    attachment_key=candidate.logical_attachment_key,
                    selected_attachment_key=candidate.selected_attachment_key,
                    parent_item_id=candidate.parent_item_id,
                    parent_item_key=candidate.parent_item_key,
                    parent_title=candidate.parent_title or visible_name,
                    attachment_title=candidate.attachment_title,
                    visible_name=visible_name,
                    pdf_path=resolved_path,
                    original_filename=candidate.original_filename or resolved_path.name,
                    tags=candidate.tags,
                    source_kind=candidate.source_kind,
                    collection_path_parts=candidate.collection_path_parts,
                    date_added=candidate.date_added,
                    date_modified=candidate.date_modified,
                    routing_error=candidate.routing_error,
                )
            )

        attachments.sort(key=lambda item: (item.visible_name.lower(), item.original_filename.lower()))
        return attachments

    def get_all_collection_paths(self) -> dict[int, tuple[str, ...]]:
        rows = self.conn.execute(
            """
            SELECT collectionID, collectionName, parentCollectionID
            FROM collections
            ORDER BY collectionID
            """
        ).fetchall()

        children_by_parent: dict[int, list[sqlite3.Row]] = {}
        top_level_rows: list[sqlite3.Row] = []
        for row in rows:
            parent_id = row["parentCollectionID"]
            if parent_id is None:
                top_level_rows.append(row)
                continue
            children_by_parent.setdefault(int(parent_id), []).append(row)

        paths: dict[int, tuple[str, ...]] = {}

        def walk(row: sqlite3.Row, prefix: tuple[str, ...]) -> None:
            collection_id = int(row["collectionID"])
            collection_name = str(row["collectionName"])
            path = prefix + (collection_name,)
            paths[collection_id] = path
            for child_row in children_by_parent.get(collection_id, []):
                walk(child_row, path)

        for row in top_level_rows:
            walk(row, ())

        return paths

    @staticmethod
    def combine_error_messages(primary: str | None, secondary: str | None) -> str | None:
        if primary and secondary:
            if secondary in primary:
                return primary
            return f"{primary} {secondary}"
        return primary or secondary

    @staticmethod
    def attachment_display_name(
        *,
        selected_attachment_key: str,
        attachment_path: str | None,
    ) -> str:
        if attachment_path:
            if ":" in attachment_path:
                return attachment_path.split(":", 1)[1].strip() or selected_attachment_key
            return Path(attachment_path).name or selected_attachment_key
        return selected_attachment_key

    @staticmethod
    def validate_pdf_file(path: Path) -> tuple[bool, str | None]:
        try:
            if not path.exists():
                return False, f"Source PDF does not exist: {path}"
            if zipfile.is_zipfile(path):
                return False, f"Source PDF is actually a ZIP archive: {path.name}"
            with path.open("rb") as handle:
                if handle.read(5) != b"%PDF-":
                    return False, f"Source file does not appear to be a valid PDF: {path.name}"
        except OSError as exc:
            return False, f"Could not inspect source PDF {path}: {exc}"
        return True, None

    @staticmethod
    def pick_preferred_candidate(
        candidates: list[PreparedAttachmentCandidate],
    ) -> PreparedAttachmentCandidate:
        return max(
            candidates,
            key=lambda candidate: (
                candidate.pdf_is_valid,
                candidate.date_modified,
                candidate.date_added,
                candidate.attachment_item_id,
            ),
        )

    def get_collection_subtree(
        self,
        collection_name: str,
    ) -> tuple[int, dict[int, tuple[str, ...]]]:
        root_collection_id = self.get_collection_id(collection_name)
        rows = self.conn.execute(
            """
            SELECT collectionID, collectionName, parentCollectionID
            FROM collections
            ORDER BY collectionID
            """
        ).fetchall()

        children_by_parent: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            parent_id = row["parentCollectionID"]
            if parent_id is None:
                continue
            children_by_parent.setdefault(int(parent_id), []).append(row)

        subtree_paths: dict[int, tuple[str, ...]] = {
            root_collection_id: (),
        }

        def walk(parent_collection_id: int, prefix: tuple[str, ...]) -> None:
            for child_row in children_by_parent.get(parent_collection_id, []):
                child_collection_id = int(child_row["collectionID"])
                child_name = str(child_row["collectionName"])
                child_path = prefix + (child_name,)
                subtree_paths[child_collection_id] = child_path
                walk(child_collection_id, child_path)

        walk(root_collection_id, ())
        return root_collection_id, subtree_paths

    def get_item_collection_memberships(
        self,
        *,
        collection_ids: list[int],
        item_ids: set[int],
    ) -> dict[int, set[int]]:
        if not collection_ids or not item_ids:
            return {}

        collection_placeholders = ", ".join("?" for _ in collection_ids)
        item_placeholders = ", ".join("?" for _ in item_ids)
        rows = self.conn.execute(
            f"""
            SELECT itemID, collectionID
            FROM collectionItems
            WHERE collectionID IN ({collection_placeholders})
              AND itemID IN ({item_placeholders})
            """,
            tuple(collection_ids) + tuple(sorted(item_ids)),
        ).fetchall()

        memberships: dict[int, set[int]] = {}
        for row in rows:
            memberships.setdefault(int(row["itemID"]), set()).add(int(row["collectionID"]))
        return memberships

    def resolve_attachment_collection_path(
        self,
        *,
        attachment_item_id: int,
        parent_item_id: int | None,
        membership_map: dict[int, set[int]],
        collection_paths: dict[int, tuple[str, ...]],
        root_collection_name: str | None,
        require_membership_path: bool,
    ) -> tuple[tuple[str, ...], str | None]:
        candidate_paths = {
            collection_paths[collection_id]
            for item_id in (attachment_item_id, parent_item_id)
            if item_id is not None
            for collection_id in membership_map.get(item_id, set())
            if collection_id in collection_paths
        }

        if not candidate_paths:
            if require_membership_path:
                label = root_collection_name or "the selected Zotero scope"
                return (), (
                    f'Attachment was matched under "{label}" but no direct '
                    "collection membership path could be resolved."
                )
            return (), None

        if len(candidate_paths) == 1:
            return next(iter(candidate_paths)), None

        ordered_paths = sorted(candidate_paths, key=lambda parts: (len(parts), parts))
        deepest_path = ordered_paths[-1]
        if all(self.is_collection_path_prefix(path, deepest_path) for path in ordered_paths):
            return deepest_path, None

        rendered_paths = ", ".join(
            self.describe_collection_path(path, root_collection_name=root_collection_name)
            for path in ordered_paths
        )
        if root_collection_name:
            return (), (
                f'Attachment belongs to multiple "{root_collection_name}" collection branches '
                f"({rendered_paths}). Keep it in one branch so the target reMarkable "
                "folder is unambiguous."
            )
        return (), (
            "Attachment belongs to multiple Zotero collection branches "
            f"({rendered_paths}). Keep it in one branch so the target reMarkable "
            "folder is unambiguous."
        )

    @staticmethod
    def is_collection_path_prefix(
        candidate: tuple[str, ...],
        target: tuple[str, ...],
    ) -> bool:
        return candidate == target[: len(candidate)]

    @staticmethod
    def describe_collection_path(
        path_parts: tuple[str, ...],
        *,
        root_collection_name: str | None,
    ) -> str:
        if root_collection_name is None:
            return " / ".join(path_parts) if path_parts else "(library root)"
        if not path_parts:
            return root_collection_name
        return " / ".join((root_collection_name, *path_parts))

    def get_item_title(self, item_id: int) -> str:
        row = self.conn.execute(
            """
            SELECT item_value.value AS title
            FROM itemData
            JOIN fields ON fields.fieldID = itemData.fieldID
            JOIN itemDataValues AS item_value ON item_value.valueID = itemData.valueID
            WHERE itemData.itemID = ?
              AND fields.fieldName = 'title'
            LIMIT 1
            """,
            (item_id,),
        ).fetchone()
        return row["title"] if row and row["title"] else ""

    def get_item_tags(self, item_id: int) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT DISTINCT tags.name
            FROM itemTags
            JOIN tags ON tags.tagID = itemTags.tagID
            WHERE itemTags.itemID = ?
            ORDER BY lower(tags.name), tags.name
            """,
            (item_id,),
        ).fetchall()
        item_tags = [str(row["name"]) for row in rows if row["name"]]
        author_tags = self.get_item_author_surname_tags(item_id)
        publication_year_tags = self.get_item_publication_year_tags(item_id)
        return self.merge_tag_lists(item_tags, author_tags, publication_year_tags)

    def get_item_author_surname_tags(self, item_id: int) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT creators.lastName AS last_name
            FROM itemCreators
            JOIN creatorTypes ON creatorTypes.creatorTypeID = itemCreators.creatorTypeID
            JOIN creators ON creators.creatorID = itemCreators.creatorID
            WHERE itemCreators.itemID = ?
              AND lower(creatorTypes.creatorType) = 'author'
            ORDER BY itemCreators.orderIndex, itemCreators.creatorID
            """,
            (item_id,),
        ).fetchall()

        surnames = [str(row["last_name"]).strip() for row in rows if row["last_name"]]
        if not surnames:
            return []
        if len(surnames) == 1:
            return [surnames[0]]
        return [surnames[0], surnames[-1]]

    def get_item_publication_year_tags(self, item_id: int) -> list[str]:
        row = self.conn.execute(
            """
            SELECT item_value.value AS date_value
            FROM itemData
            JOIN fields ON fields.fieldID = itemData.fieldID
            JOIN itemDataValues AS item_value ON item_value.valueID = itemData.valueID
            WHERE itemData.itemID = ?
              AND fields.fieldName = 'date'
            LIMIT 1
            """,
            (item_id,),
        ).fetchone()
        if not row or not row["date_value"]:
            return []

        date_value = str(row["date_value"]).strip()
        # Zotero date fields can contain partial dates or free text; take the first
        # plausible publication year when one is present.
        match = re.search(r"(?<!\d)(1[5-9]\d{2}|20\d{2}|21\d{2})(?!\d)", date_value)
        if not match:
            return []
        return [match.group(1)]

    @staticmethod
    def merge_tag_lists(*tag_lists: list[str]) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for tag_list in tag_lists:
            for raw_tag in tag_list:
                tag = raw_tag.strip()
                if not tag:
                    continue
                key = tag.casefold()
                if key in seen:
                    continue
                seen.add(key)
                merged.append(tag)
        return merged

    @staticmethod
    def build_visible_name(
        *,
        parent_title: str,
        original_filename: str,
        attachment_count: int,
    ) -> str:
        filename_stem = Path(original_filename).stem.strip()
        title = parent_title.strip() or filename_stem
        return title

    @staticmethod
    def attachment_source_kind(attachment_path: str | None) -> str:
        if attachment_path and attachment_path.startswith("storage:"):
            return "stored"
        return "linked"

    def resolve_attachment_path(
        self,
        *,
        attachment_key: str,
        attachment_path: str | None,
    ) -> Path:
        if not attachment_path:
            raise ValueError(
                f"Attachment {attachment_key} does not have a local path in Zotero."
            )

        if attachment_path.startswith("storage:"):
            relative_name = attachment_path.split(":", 1)[1]
            return (self.config.storage_dir / attachment_key / relative_name).resolve()

        if attachment_path.startswith("attachments:"):
            if self.config.linked_attachment_base_dir is None:
                raise ValueError(
                    f"Attachment {attachment_key} uses an attachments: path but "
                    "linked_attachment_base_dir is not configured."
                )
            relative_path = attachment_path.split(":", 1)[1]
            return (self.config.linked_attachment_base_dir / relative_path).resolve()

        if attachment_path.startswith("file://"):
            parsed = urlparse(attachment_path)
            return Path(unquote(parsed.path)).expanduser().resolve()

        direct_path = Path(attachment_path).expanduser()
        if direct_path.is_absolute():
            return direct_path.resolve()

        if self.config.linked_attachment_base_dir is not None:
            return (self.config.linked_attachment_base_dir / direct_path).resolve()

        raise ValueError(
            f"Attachment {attachment_key} uses a relative linked path but "
            "linked_attachment_base_dir is not configured."
        )
