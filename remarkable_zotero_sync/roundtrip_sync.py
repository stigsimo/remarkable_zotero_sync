from __future__ import annotations

from collections.abc import Callable

from remarkable_zotero_sync.config import AppConfig
from remarkable_zotero_sync.models import ReturnSyncReport, RoundTripSyncReport
from remarkable_zotero_sync.remarkable_client import RemarkableClient
from remarkable_zotero_sync.remarkable_return_sync import RemarkableReturnSyncService
from remarkable_zotero_sync.sync_service import SyncService


class RoundTripSyncService:
    def __init__(
        self,
        config: AppConfig,
        *,
        progress: Callable[[str], None] | None = None,
    ):
        self.config = config
        self.progress = progress

    def build_report(self) -> RoundTripSyncReport:
        forward_service = SyncService(self.config, progress=self.progress)
        reverse_service = RemarkableReturnSyncService(self.config, progress=self.progress)
        return RoundTripSyncReport(
            dry_run=True,
            forward=forward_service.build_report(dry_run=True),
            reverse=reverse_service.build_report(dry_run=True),
        )

    def execute(self) -> RoundTripSyncReport:
        forward_service = SyncService(self.config, progress=self.progress)
        forward_report = forward_service.execute()

        if forward_report.xochitl_restarted:
            client = RemarkableClient(self.config.remarkable)
            if self.progress is not None:
                self.progress(
                    "Waiting for xochitl to come back before reMarkable -> Zotero sync..."
                )
            try:
                client.wait_for_xochitl_ready()
            except Exception as exc:
                error = (
                    "xochitl was restarted after forward sync, but it did not become "
                    f"ready again before return sync: {exc}"
                )
                forward_report.errors.append(error)
                return RoundTripSyncReport(
                    dry_run=False,
                    forward=forward_report,
                    reverse=ReturnSyncReport(
                        dry_run=False,
                        errors=[
                            "Skipped reMarkable -> Zotero sync because xochitl was "
                            "still restarting after the forward sync."
                        ],
                    ),
                )

        reverse_service = RemarkableReturnSyncService(self.config, progress=self.progress)
        reverse_report = reverse_service.execute()

        return RoundTripSyncReport(
            dry_run=False,
            forward=forward_report,
            reverse=reverse_report,
        )
