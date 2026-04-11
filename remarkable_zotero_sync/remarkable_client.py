from __future__ import annotations

import json
import getpass
import shlex
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

from remarkable_zotero_sync.config import RemarkableConfig
from remarkable_zotero_sync.models import RemarkableItem, UploadRequest, UploadResult


class RemarkableClient:
    def __init__(self, config: RemarkableConfig):
        self.config = config
        self._prompted_password: str | None = None

    def _run(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        last_result: subprocess.CompletedProcess[str] | None = None
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, text=True)
            except FileNotFoundError as exc:
                raise RuntimeError(
                    f"Required command was not found while talking to reMarkable: {cmd[0]}"
                ) from exc

            last_result = result
            if result.returncode == 0:
                return result

            detail = result.stderr.strip() or result.stdout.strip()
            if self._looks_like_timeout(detail):
                raise RuntimeError(
                    self._usb_reconnect_message(
                        "Timed out while talking to the reMarkable."
                    )
                )
            if attempt < 2 and self._looks_like_transient_connection_issue(detail):
                time.sleep(1.5)
                continue

            raise RuntimeError(
                f"Command failed ({' '.join(cmd)}):\n{detail}"
            )

        assert last_result is not None
        detail = last_result.stderr.strip() or last_result.stdout.strip()
        raise RuntimeError(f"Command failed ({' '.join(cmd)}):\n{detail}")

    @staticmethod
    def _looks_like_timeout(detail: str) -> bool:
        lowered = detail.casefold()
        return "timed out" in lowered or "operation timed out" in lowered

    @staticmethod
    def _looks_like_transient_connection_issue(detail: str) -> bool:
        lowered = detail.casefold()
        return any(
            marker in lowered
            for marker in (
                "permission denied",
                "connection closed",
                "connection reset",
                "broken pipe",
            )
        )

    def _usb_reconnect_message(self, prefix: str) -> str:
        return (
            f"{prefix} If your reMarkable is connected over USB, unplug the USB "
            "cable and plug it back in again, then retry the sync."
        )

    def _current_password(self) -> str:
        password = self.config.password or self._prompted_password
        if not password:
            password = getpass.getpass(
                prompt=(
                    f"reMarkable password for {self.config.user}@{self.config.host}: "
                )
            ).strip()
            if not password:
                raise ValueError(
                    "No reMarkable SSH password is available. Run the public sync "
                    "again and enter it when prompted, or store it in config.toml."
                )
            self._prompted_password = password
        return password

    def _sshpass_prefix(self) -> list[str]:
        return ["sshpass", "-p", self._current_password()]

    def ssh_cmd(self, command: str) -> subprocess.CompletedProcess[str]:
        return self._run(
            self._sshpass_prefix()
            + [
                "ssh",
                "-o",
                f"ConnectTimeout={self.config.ssh_connect_timeout_seconds}",
                "-o",
                "StrictHostKeyChecking=accept-new",
                f"{self.config.user}@{self.config.host}",
                command,
            ]
        )

    def scp_to(self, local_path: Path, remote_path: str) -> subprocess.CompletedProcess[str]:
        return self._run(
            self._sshpass_prefix()
            + [
                "scp",
                "-o",
                f"ConnectTimeout={self.config.ssh_connect_timeout_seconds}",
                "-o",
                "StrictHostKeyChecking=accept-new",
                str(local_path),
                f"{self.config.user}@{self.config.host}:{remote_path}",
            ]
        )

    def pull_metadata(self) -> None:
        password = self._current_password()
        cache_dir = self.config.metadata_cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        for stale_metadata in cache_dir.glob("*.metadata"):
            stale_metadata.unlink()

        ssh_command = (
            f"sshpass -p {shlex.quote(password)} ssh "
            f"-o ConnectTimeout={self.config.ssh_connect_timeout_seconds} "
            "-o StrictHostKeyChecking=accept-new"
        )

        self._run(
            [
                "rsync",
                "-av",
                "--include=*.metadata",
                "--exclude=*",
                "-e",
                ssh_command,
                f"{self.config.user}@{self.config.host}:{self.config.xochitl_dir}/",
                f"{cache_dir}/",
            ]
        )

    def fetch_metadata_items_local(self) -> dict[str, RemarkableItem]:
        items: dict[str, RemarkableItem] = {}
        for path in self.config.metadata_cache_dir.glob("*.metadata"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            item_uuid = path.stem
            items[item_uuid] = RemarkableItem(
                uuid=item_uuid,
                visible_name=data.get("visibleName", item_uuid),
                item_type=data.get("type", ""),
                parent=data.get("parent", ""),
                deleted=bool(data.get("deleted", False)),
                raw=data,
            )
        return items

    def load_metadata_items(self) -> dict[str, RemarkableItem]:
        self.pull_metadata()
        return self.fetch_metadata_items_local()

    def list_existing_document_file_ids(self) -> set[str]:
        quoted_root = shlex.quote(self.config.xochitl_dir)
        command = (
            f"find {quoted_root} -maxdepth 1 -type f -name '*.pdf' -print"
        )
        result = self.ssh_cmd(command)

        document_ids: set[str] = set()
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            path = Path(line)
            if path.suffix != ".pdf":
                continue
            document_ids.add(path.stem)
        return document_ids

    def folder_path(self, folder_uuid: str, items: dict[str, RemarkableItem]) -> str:
        parts: list[str] = []
        current = items.get(folder_uuid)
        while current is not None:
            parts.append(current.visible_name)
            if not current.parent:
                break
            current = items.get(current.parent)
        return "/" + "/".join(reversed(parts))

    @staticmethod
    def normalize_folder_path(folder_path: str) -> str:
        if not folder_path or folder_path == "/":
            return "/"
        normalized = "/" + folder_path.strip("/")
        return normalized

    @staticmethod
    def item_is_accessible(
        item_uuid: str,
        items: dict[str, RemarkableItem],
        *,
        expected_type: str | None = None,
    ) -> bool:
        item = items.get(item_uuid)
        if item is None:
            return False
        if expected_type is not None and item.item_type != expected_type:
            return False
        if item.deleted:
            return False

        seen: set[str] = set()
        current = item
        while current.parent:
            parent_uuid = current.parent
            if parent_uuid == "trash":
                return False
            if parent_uuid in seen:
                return False
            seen.add(parent_uuid)
            parent = items.get(parent_uuid)
            if parent is None:
                return False
            if parent.deleted:
                return False
            current = parent

        return True

    def resolve_parent_uuid(self) -> str:
        if self.config.folder_uuid:
            return self.config.folder_uuid

        desired_path = self.normalize_folder_path(self.config.folder_path)
        if desired_path == "/":
            return ""

        self.pull_metadata()
        items = self.fetch_metadata_items_local()
        folders = [
            item
            for item in items.values()
            if self.item_is_accessible(
                item.uuid,
                items,
                expected_type="CollectionType",
            )
        ]
        for folder in folders:
            if self.folder_path(folder.uuid, items) == desired_path:
                return folder.uuid

        raise ValueError(
            f'reMarkable folder path "{desired_path}" was not found in pulled metadata.'
        )

    def ensure_existing_folder_uuid(
        self,
        folder_uuid: str,
        *,
        items: dict[str, RemarkableItem] | None = None,
    ) -> str:
        if not folder_uuid:
            return ""

        metadata_items = items if items is not None else self.load_metadata_items()
        if not self.item_is_accessible(
            folder_uuid,
            metadata_items,
            expected_type="CollectionType",
        ):
            raise ValueError(
                f'reMarkable folder UUID "{folder_uuid}" was not found in pulled metadata.'
            )
        return folder_uuid

    def ensure_folder_path(
        self,
        folder_path: str,
        *,
        items: dict[str, RemarkableItem] | None = None,
    ) -> str:
        desired_path = self.normalize_folder_path(folder_path)
        if desired_path == "/":
            return ""

        metadata_items = items if items is not None else self.load_metadata_items()
        current_parent = ""
        for part in self._path_parts(desired_path):
            existing = self.find_child_folder(
                parent_uuid=current_parent,
                visible_name=part,
                items=metadata_items,
            )
            if existing is None:
                existing = self.create_folder(
                    visible_name=part,
                    parent_uuid=current_parent,
                )
                metadata_items[existing.uuid] = existing
            current_parent = existing.uuid

        return current_parent

    def ensure_child_folders(
        self,
        base_parent_uuid: str,
        path_parts: tuple[str, ...],
        *,
        items: dict[str, RemarkableItem] | None = None,
    ) -> str:
        metadata_items = items if items is not None else self.load_metadata_items()
        current_parent = self.ensure_existing_folder_uuid(
            base_parent_uuid,
            items=metadata_items,
        )

        for part in path_parts:
            cleaned = part.strip()
            if not cleaned:
                continue
            existing = self.find_child_folder(
                parent_uuid=current_parent,
                visible_name=cleaned,
                items=metadata_items,
            )
            if existing is None:
                existing = self.create_folder(
                    visible_name=cleaned,
                    parent_uuid=current_parent,
                )
                metadata_items[existing.uuid] = existing
            current_parent = existing.uuid

        return current_parent

    def find_child_folder(
        self,
        *,
        parent_uuid: str,
        visible_name: str,
        items: dict[str, RemarkableItem],
    ) -> RemarkableItem | None:
        for item in items.values():
            if not self.item_is_accessible(
                item.uuid,
                items,
                expected_type="CollectionType",
            ):
                continue
            if item.parent != parent_uuid:
                continue
            if item.visible_name == visible_name:
                return item
        return None

    def create_folder(
        self,
        *,
        visible_name: str,
        parent_uuid: str,
    ) -> RemarkableItem:
        folder_uuid = str(uuid.uuid4())
        timestamp_ms = str(int(time.time() * 1000))
        metadata = {
            "createdTime": timestamp_ms,
            "lastModified": timestamp_ms,
            "new": False,
            "parent": parent_uuid,
            "pinned": False,
            "source": "",
            "type": "CollectionType",
            "visibleName": visible_name,
        }

        self.write_remote_json(
            filename=f"{folder_uuid}.metadata",
            payload=metadata,
        )

        return RemarkableItem(
            uuid=folder_uuid,
            visible_name=visible_name,
            item_type="CollectionType",
            parent=parent_uuid,
            deleted=False,
            raw=metadata,
        )

    @staticmethod
    def _path_parts(folder_path: str) -> list[str]:
        normalized = RemarkableClient.normalize_folder_path(folder_path)
        if normalized == "/":
            return []
        return [part for part in normalized.strip("/").split("/") if part]

    def upload_pdf(self, request: UploadRequest) -> UploadResult:
        document_uuid = str(uuid.uuid4())
        timestamp_ms = str(int(time.time() * 1000))

        metadata = {
            "deleted": False,
            "lastModified": timestamp_ms,
            "lastOpened": "0",
            "lastOpenedPage": 0,
            "metadatamodified": True,
            "modified": False,
            "parent": request.parent_uuid,
            "pinned": False,
            "synced": False,
            "type": "DocumentType",
            "version": 0,
            "visibleName": request.visible_name,
        }
        if request.tags and self.config.tag_metadata_field:
            # Assumption: a top-level string-list field like "tags" is tolerated or
            # consumed by the device metadata format. This is intentionally best-effort.
            metadata[self.config.tag_metadata_field] = request.tags

        content = {
            "fileType": "pdf",
            "lastOpenedPage": 0,
            "pageCount": 0,
        }
        if request.tags:
            content["pageTags"] = []
            content["tags"] = self.build_content_tag_entries(
                request.tags,
                timestamp_ms=timestamp_ms,
            )

        remote_prefix = f"{self.config.xochitl_dir}/{document_uuid}"
        self.scp_to(request.source_path, f"{remote_prefix}.pdf")
        self.write_remote_json(
            filename=f"{document_uuid}.metadata",
            payload=metadata,
        )
        self.write_remote_json(
            filename=f"{document_uuid}.content",
            payload=content,
        )

        return UploadResult(
            remote_uuid=document_uuid,
            visible_name=request.visible_name,
            source_path=request.source_path,
            tags=request.tags,
        )

    @staticmethod
    def normalize_tag_list(raw_value: object) -> list[str]:
        if not isinstance(raw_value, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for raw_tag in raw_value:
            if not isinstance(raw_tag, str):
                continue
            tag = raw_tag.strip()
            if not tag:
                continue
            folded = tag.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            out.append(tag)
        return out

    @classmethod
    def build_content_tag_entries(
        cls,
        tags: list[str],
        *,
        timestamp_ms: str | int | None = None,
    ) -> list[dict[str, object]]:
        normalized_tags = cls.normalize_tag_list(tags)
        if not normalized_tags:
            return []
        if timestamp_ms is None:
            timestamp_value = int(time.time() * 1000)
        else:
            timestamp_value = int(timestamp_ms)
        return [
            {
                "name": tag,
                "timestamp": timestamp_value,
            }
            for tag in normalized_tags
        ]

    @classmethod
    def normalize_content_tag_list(cls, raw_value: object) -> list[str]:
        if not isinstance(raw_value, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for raw_tag in raw_value:
            if not isinstance(raw_tag, dict):
                continue
            name = raw_tag.get("name")
            if not isinstance(name, str):
                continue
            normalized = cls.normalize_tag_list([name])
            if not normalized:
                continue
            tag = normalized[0]
            folded = tag.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            out.append(tag)
        return out

    def document_tags(self, item: RemarkableItem) -> list[str]:
        if not self.config.tag_metadata_field:
            return []
        return self.normalize_tag_list(item.raw.get(self.config.tag_metadata_field))

    def read_remote_json(self, *, filename: str) -> dict[str, object]:
        remote_path = shlex.quote(f"{self.config.xochitl_dir}/{filename}")
        result = self.ssh_cmd(f"cat {remote_path}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Remote JSON file {filename} did not contain valid JSON."
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Remote JSON file {filename} did not contain a JSON object."
            )
        return payload

    def update_document_tags(
        self,
        document_uuid: str,
        *,
        tags: list[str],
        visible_name: str | None = None,
        items: dict[str, RemarkableItem] | None = None,
    ) -> bool:
        metadata_items = items if items is not None else self.load_metadata_items()
        item = metadata_items.get(document_uuid)
        if item is None or item.deleted or item.item_type != "DocumentType":
            return False

        normalized_tags = self.normalize_tag_list(tags)
        current_metadata_tags = self.document_tags(item)
        content = self.read_remote_json(filename=f"{document_uuid}.content")
        current_content_tags = self.normalize_content_tag_list(content.get("tags"))
        current_visible_name = str(item.raw.get("visibleName") or item.visible_name or "")
        desired_visible_name = (
            visible_name.strip()
            if isinstance(visible_name, str) and visible_name.strip()
            else current_visible_name
        )
        desired_tags = self.normalize_tag_list(
            [
                *current_content_tags,
                *current_metadata_tags,
                *normalized_tags,
            ]
        )
        if (
            current_metadata_tags == desired_tags
            and current_content_tags == desired_tags
            and current_visible_name == desired_visible_name
        ):
            return False

        timestamp_ms = str(int(time.time() * 1000))
        metadata = dict(item.raw)
        if desired_tags and self.config.tag_metadata_field:
            metadata[self.config.tag_metadata_field] = desired_tags
        elif self.config.tag_metadata_field:
            metadata.pop(self.config.tag_metadata_field, None)
        metadata["visibleName"] = desired_visible_name
        metadata["lastModified"] = timestamp_ms
        metadata["metadatamodified"] = True

        if desired_tags:
            content["tags"] = self.build_content_tag_entries(
                desired_tags,
                timestamp_ms=timestamp_ms,
            )
            content.setdefault("pageTags", [])
        else:
            content.pop("tags", None)
            content.setdefault("pageTags", [])
        document_metadata = content.get("documentMetadata")
        if isinstance(document_metadata, dict):
            document_metadata = dict(document_metadata)
            document_metadata["title"] = desired_visible_name
            content["documentMetadata"] = document_metadata

        self.write_remote_json(
            filename=f"{document_uuid}.metadata",
            payload=metadata,
        )
        self.write_remote_json(
            filename=f"{document_uuid}.content",
            payload=content,
        )
        metadata_items[document_uuid] = RemarkableItem(
            uuid=item.uuid,
            visible_name=desired_visible_name or item.visible_name,
            item_type=item.item_type,
            parent=item.parent,
            deleted=item.deleted,
            raw=metadata,
        )
        return True

    def soft_delete_document(
        self,
        document_uuid: str,
        *,
        items: dict[str, RemarkableItem] | None = None,
    ) -> bool:
        metadata_items = items if items is not None else self.load_metadata_items()
        item = metadata_items.get(document_uuid)
        if item is None or item.deleted or item.item_type != "DocumentType":
            return False

        metadata = dict(item.raw)
        metadata["deleted"] = True
        metadata["lastModified"] = str(int(time.time() * 1000))
        metadata["metadatamodified"] = True
        self.write_remote_json(
            filename=f"{document_uuid}.metadata",
            payload=metadata,
        )
        metadata_items[document_uuid] = RemarkableItem(
            uuid=item.uuid,
            visible_name=item.visible_name,
            item_type=item.item_type,
            parent=item.parent,
            deleted=True,
            raw=metadata,
        )
        return True

    def write_remote_json(self, *, filename: str, payload: dict[str, object]) -> None:
        remote_path = f"{self.config.xochitl_dir}/{filename}"
        with tempfile.TemporaryDirectory(prefix="remarkable-json-") as temp_dir:
            local_path = Path(temp_dir) / filename
            local_path.write_text(json.dumps(payload), encoding="utf-8")
            self.scp_to(local_path, remote_path)

    def restart_xochitl(self) -> None:
        self.ssh_cmd("systemctl restart xochitl")

    def wait_for_xochitl_ready(self) -> None:
        deadline = time.monotonic() + max(1, self.config.xochitl_ready_timeout_seconds)
        last_error: Exception | None = None

        while time.monotonic() < deadline:
            try:
                result = self.ssh_cmd("systemctl is-active xochitl || true")
                state = result.stdout.strip().casefold()
                if state == "active":
                    if self.config.xochitl_settle_seconds > 0:
                        time.sleep(self.config.xochitl_settle_seconds)
                    return
            except Exception as exc:
                last_error = exc

            time.sleep(2)

        message = (
            "xochitl did not become ready again before the timeout expired."
        )
        if last_error is not None:
            message = f"{message} Last error: {last_error}"
        raise RuntimeError(message)

    def list_document_ids_with_highlights(self) -> set[str]:
        quoted_root = shlex.quote(self.config.xochitl_dir)
        command = (
            f"find {quoted_root} -type f "
            "\\( -name '*.rm' -o -name '*.json' \\)"
        )
        result = self.ssh_cmd(command)

        document_ids: set[str] = set()
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parent_name = Path(line).parent.name
            if parent_name.endswith(".highlights"):
                document_ids.add(parent_name[: -len(".highlights")])
                continue
            if line.endswith(".rm"):
                document_ids.add(parent_name)
        return document_ids

    def download_annotated_pdf(self, document_uuid: str, destination_path: Path) -> Path:
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if destination_path.exists():
            destination_path.unlink()

        url = f"http://{self.config.host}/download/{document_uuid}/pdf"
        for attempt in range(2):
            try:
                with urllib.request.urlopen(
                    url,
                    timeout=self.config.web_download_timeout_seconds,
                ) as response:
                    if getattr(response, "status", 200) != 200:
                        raise RuntimeError(
                            f"Unexpected response when downloading {document_uuid}: "
                            f"HTTP {response.status}"
                        )
                    destination_path.write_bytes(response.read())
                    break
            except urllib.error.HTTPError as exc:
                if exc.code == 408 and attempt == 0:
                    self.restart_xochitl()
                    self.wait_for_xochitl_ready()
                    continue
                raise RuntimeError(
                    "Failed to download the annotated PDF from the reMarkable web interface. "
                    f"HTTP {exc.code}."
                ) from exc
            except urllib.error.URLError as exc:
                message = str(exc.reason or exc)
                if self._looks_like_timeout(message) and attempt == 0:
                    self.restart_xochitl()
                    self.wait_for_xochitl_ready()
                    continue
                if self._looks_like_timeout(message):
                    raise RuntimeError(
                        self._usb_reconnect_message(
                            "Timed out while downloading the annotated PDF from the reMarkable web interface."
                        )
                    ) from exc
                raise RuntimeError(
                    "Failed to download the annotated PDF from the reMarkable web interface. "
                    "Make sure the device is reachable and its web interface is enabled."
                ) from exc
        else:
            raise RuntimeError(
                "Failed to download the annotated PDF from the reMarkable web interface."
            )

        if self._is_zip_archive(destination_path):
            raise RuntimeError(
                "The reMarkable web interface returned an rmdoc archive instead of an annotated PDF. "
                "The sync expects /download/<uuid>/pdf to return a real PDF."
            )
        if not self._looks_like_pdf(destination_path):
            raise RuntimeError(
                "The reMarkable web interface did not return a valid PDF file."
            )

        return destination_path

    @staticmethod
    def _is_zip_archive(path: Path) -> bool:
        try:
            return zipfile.is_zipfile(path)
        except OSError:
            return False

    @staticmethod
    def _looks_like_pdf(path: Path) -> bool:
        try:
            with path.open("rb") as handle:
                return handle.read(5) == b"%PDF-"
        except OSError:
            return False
