"""Protected pause, resume, and rollback orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from market_radar.publishing import (
    LoadedPublicationControl,
    PromotionResult,
    PublicationControlRepository,
    Publisher,
)
from market_radar.timeutil import utc_now


@dataclass(frozen=True)
class OperationResult:
    operation: str
    control: LoadedPublicationControl
    promotion: PromotionResult | None = None


class OperationsService:
    """Apply production controls in a fail-closed order.

    Rollback pauses future publications before moving the public pointer. If
    snapshot verification or the conditional pointer update fails, publication
    remains paused for operator inspection.
    """

    def __init__(
        self,
        control_repository: PublicationControlRepository,
        *,
        publisher: Publisher | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.control_repository = control_repository
        self.publisher = publisher
        self.clock = clock

    def pause(self, *, reason: str, actor: str) -> OperationResult:
        updated = self.control_repository.pause(
            reason=reason,
            actor=actor,
            updated_at=self._now(),
            previous=self.control_repository.load(),
        )
        return OperationResult("pause", updated)

    def resume(self, *, reason: str, actor: str) -> OperationResult:
        updated = self.control_repository.resume(
            reason=reason,
            actor=actor,
            updated_at=self._now(),
            previous=self.control_repository.load(),
        )
        return OperationResult("resume", updated)

    def rollback(
        self,
        *,
        snapshot_key: str,
        reason: str,
        actor: str,
    ) -> OperationResult:
        if self.publisher is None:
            raise ValueError("rollback requires a public snapshot publisher")
        paused = self.control_repository.pause(
            reason=reason,
            actor=actor,
            updated_at=self._now(),
            previous=self.control_repository.load(),
        )
        promotion = self.publisher.promote_existing(snapshot_key)
        return OperationResult("rollback", paused, promotion)

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("operations clock must return a timezone-aware timestamp")
        return value.astimezone(timezone.utc).replace(microsecond=0)
