from __future__ import annotations

from collections.abc import Iterable

from app.schemas.agent import CandidateWindow, WindowVersion


class VersionManager:
    def sort_candidates(
        self,
        candidates: Iterable[CandidateWindow],
        sort_mode: str,
    ) -> list[CandidateWindow]:
        items = [item for item in candidates if not item.isDeleted]
        if sort_mode == "low_opportunity_cost":
            return sorted(items, key=lambda item: (item.metrics.opportunityCost, item.generatedAt))
        if sort_mode == "low_direct_cost":
            return sorted(items, key=lambda item: (item.metrics.directCost, item.generatedAt))
        if sort_mode == "high_risk_priority":
            risk_order = {"high": 0, "medium": 1, "low": 2}
            return sorted(items, key=lambda item: (risk_order.get(item.metrics.riskLevel, 9), item.generatedAt))
        if sort_mode == "latest_first":
            return sorted(items, key=lambda item: (item.generatedAt, item.displayOrder), reverse=True)
        return sorted(items, key=lambda item: (item.metrics.opportunityCost + item.metrics.directCost, item.generatedAt))

    def compute_top_recommendations(
        self,
        candidates: Iterable[CandidateWindow],
        versions: Iterable[WindowVersion],
        sort_mode: str,
    ) -> list[CandidateWindow]:
        sorted_candidates = self.sort_candidates(candidates, sort_mode)
        if sort_mode == "latest_first":
            version_rank = {version.id: version.generatedAt for version in versions if version.includedInCandidatePool}
            sorted_candidates = sorted(
                sorted_candidates,
                key=lambda item: (version_rank.get(item.versionId, ""), -item.displayOrder),
                reverse=True,
            )
        return sorted_candidates[:3]
