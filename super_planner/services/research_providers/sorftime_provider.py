# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from super_planner.core.config import PROJECT_ROOT
from super_planner.core.exceptions import WorkflowSystemError
from super_planner.schemas.project import PlanningProject, ResearchResult
from super_planner.services.research_providers.base import ProgressCallback, ProviderHealth


class SorftimeResearchProvider:
    name = "sorftime"

    TOOL_REFERENCES = {
        "product-research": [
            "category_name_search",
            "category_report",
            "category_trend",
            "keyword_detail",
            "product_search",
            "product_detail",
            "product_reviews",
        ],
        "keyword-research": [
            "keyword_detail",
            "keyword_related_words",
            "category_keywords",
            "product_traffic_terms",
            "competitor_product_keywords",
        ],
        "amazon-analyse": [
            "product_detail",
            "product_traffic_terms",
            "competitor_product_keywords",
            "product_reviews",
            "product_trend",
        ],
        "review-analysis": ["product_detail", "product_reviews"],
    }

    def __init__(self) -> None:
        self.config_path = Path(os.getenv("SUPER_PLANNER_SORFTIME_MCP_CONFIG", PROJECT_ROOT / ".mcp.json"))

    def health_check(self) -> ProviderHealth:
        key, source = self._load_api_key()
        if not key:
            return ProviderHealth(
                name=self.name,
                configured=False,
                available=False,
                reason="SORFTIME_API_KEY is not configured.",
                metadata={
                    "config_locations": [
                        "SUPER_PLANNER_SORFTIME_API_KEY",
                        "SORFTIME_API_KEY",
                        str(PROJECT_ROOT / ".mcp.json"),
                        "SUPER_PLANNER_SORFTIME_MCP_CONFIG",
                    ],
                    "tool_references": self.TOOL_REFERENCES,
                },
            )

        url = f"https://mcp.sorftime.com?key={key}"
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                body = response.read(4096)
                available = 200 <= response.status < 300 and bool(body)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            return ProviderHealth(
                name=self.name,
                configured=True,
                available=False,
                reason=f"Sorftime MCP health check failed: {type(exc).__name__}",
                metadata={"api_key_source": source, "tool_references": self.TOOL_REFERENCES},
            )

        return ProviderHealth(
            name=self.name,
            configured=True,
            available=available,
            reason="" if available else "Sorftime MCP returned an empty or non-success response.",
            metadata={"api_key_source": source, "tool_references": self.TOOL_REFERENCES},
        )

    def run(self, project: PlanningProject, progress: ProgressCallback | None = None) -> ResearchResult:
        raise WorkflowSystemError(
            "Sorftime Research Provider is reserved but not available. Configure a valid SORFTIME_API_KEY "
            "and enable router auto/sorftime mode before using it."
        )

    def _load_api_key(self) -> tuple[str, str]:
        for name in ("SUPER_PLANNER_SORFTIME_API_KEY", "SORFTIME_API_KEY"):
            value = os.getenv(name, "").strip()
            if value:
                return value, name

        if not self.config_path.exists():
            return "", ""
        text = self.config_path.read_text(encoding="utf-8", errors="ignore")
        try:
            data: dict[str, Any] = json.loads(text)
            url = data.get("mcpServers", {}).get("sorftime", {}).get("url", "")
            key = self._extract_key_from_url(url)
            if key:
                return key, str(self.config_path)
        except json.JSONDecodeError:
            pass

        match = re.search(r'"sorftime"[\s\S]*?"url"\s*:\s*"([^"]+)"', text)
        if match:
            key = self._extract_key_from_url(match.group(1))
            if key:
                return key, str(self.config_path)
        return "", ""

    @staticmethod
    def _extract_key_from_url(url: str) -> str:
        match = re.search(r"[?&]key=([^&\s]+)", url or "")
        return match.group(1).strip() if match else ""
