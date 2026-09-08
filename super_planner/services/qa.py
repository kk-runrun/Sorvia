# -*- coding: utf-8 -*-
from __future__ import annotations

import ast
import csv
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from super_planner.core.config import WORKBENCH_ROOT
from super_planner.core.exceptions import WorkflowSystemError
from super_planner.schemas.project import PlanningProject, QAResult
from super_planner.services.content import (
    apply_project_fact_guardrails,
    build_allowed_product_facts,
    build_legacy_drafts,
    group_modules,
    merge_repair_payload,
    run_minimal_checks,
    unique,
)
from super_planner.services.content_openai import OpenAICopyGenerator, parse_json_object
from super_planner.services.content_rules import (
    FORMAT_RULES_VERSION,
    attach_length_metadata,
    failed_length_checks,
    item_length_ok,
)
from super_planner.services.research_providers.base import ProgressCallback, emit_progress


QA_VERSION = "qa-engine-v0.1"
MAX_QA_REWRITE_ROUNDS = 3
DEFAULT_BRAND_NAME = os.getenv("SUPER_PLANNER_DEFAULT_BRAND", "VEVOR").strip() or "VEVOR"


@dataclass(frozen=True)
class ForbiddenEntry:
    word: str
    meaning: str = ""
    solution: str = ""
    source: str = ""
    note: str = ""

    @property
    def risk_level(self) -> str:
        text = f"{self.word} {self.meaning} {self.solution} {self.note}".lower()
        high_markers = {
            "trademark",
            "certified",
            "approved",
            "guarantee",
            "guaranteed",
            "warranty",
            "100%",
            "\u7edd\u5bf9\u7981\u6b62",
            "\u5546\u6807",
            "\u533b\u7597",
            "\u5b89\u5168",
            "\u9632\u6b62",
        }
        if any(marker in text for marker in high_markers):
            return "high"
        return "medium"

    def as_dict(self) -> dict[str, Any]:
        return {
            "word": self.word,
            "meaning": self.meaning,
            "solution": self.solution,
            "source": self.source,
            "note": self.note,
            "risk_level": self.risk_level,
        }


class QAState:
    def __init__(self) -> None:
        self.parameter_issues: list[dict[str, Any]] = []
        self.forbidden_word_issues: list[dict[str, Any]] = []
        self.translation_issues: list[dict[str, Any]] = []
        self.format_issues: list[dict[str, Any]] = []
        self.strategy_issues: list[dict[str, Any]] = []
        self.correction_history: list[dict[str, Any]] = []
        self.unresolved_issues: list[dict[str, Any]] = []
        self.corrected_item_ids: set[str] = set()

    def add_category_issue(self, category: str, issue: dict[str, Any]) -> None:
        getattr(self, category).append(issue)
        if issue.get("status") == "unresolved":
            self.unresolved_issues.append(issue)

    def add_correction(self, item: dict[str, Any], correction: dict[str, Any]) -> None:
        item_id = str(item.get("id") or correction.get("item_id") or "")
        if item_id:
            self.corrected_item_ids.add(item_id)
        self.correction_history.append({"item_id": item_id, **correction})


class QAEngine:
    def __init__(self, project: PlanningProject) -> None:
        self.project = project
        self.planning_context = project.planning_context
        self.research_result = project.research_result
        self.strategy_result = project.strategy_result
        self.content_result = project.content_result
        self.state = QAState()
        self.forbidden_words = load_forbidden_entries()
        self.forbidden_source = forbidden_word_source_summary()
        self.fact_index = FactIndex(self.planning_context)
        self.brand = find_brand_name(self.planning_context)
        self.ai_agent: OpenAIQAAgent | None = None
        self.ai_translation_meta: dict[str, Any] = {"mode": "not_run"}

    def run(self, progress: ProgressCallback | None = None) -> QAResult:
        if (self.content_result.status or "").strip() not in {"completed", "completed_with_issues"}:
            raise WorkflowSystemError("QA requires a completed ContentResult. Please contact IT.")

        items = [json_clone(item) for item in (self.content_result.content_items or [])]
        if not items:
            raise WorkflowSystemError("QA found no ContentResult items to check. Please contact IT.")

        emit_progress(progress, "\u6b63\u5728\u6838\u5bf9\u4ea7\u54c1\u53c2\u6570")
        items = [self.normalize_item(item) for item in items]
        self.check_parameters(items)

        emit_progress(progress, "\u6b63\u5728\u68c0\u67e5\u8fdd\u7981\u8bcd")
        self.check_forbidden_words(items)

        emit_progress(progress, "\u6b63\u5728\u68c0\u67e5\u4e2d\u82f1\u6587\u4e00\u81f4\u6027")
        self.check_translation_consistency(items)

        emit_progress(progress, "\u6b63\u5728\u68c0\u67e5\u6587\u6848\u683c\u5f0f")
        self.check_format(items)

        emit_progress(progress, "\u6b63\u5728\u68c0\u67e5\u7b56\u7565\u4e00\u81f4\u6027")
        self.check_strategy_consistency(items)

        emit_progress(progress, "\u6b63\u5728\u8fdb\u884c\u81ea\u52a8\u4fee\u6b63")
        items = [self.normalize_item(item) for item in items]
        final_forbidden = self.find_forbidden_hits(items)
        final_format_failures = [
            {
                "id": item.get("id"),
                "module": item.get("module"),
                "submodule": item.get("submodule"),
                "failed_length_checks": failed_length_checks(item),
            }
            for item in items
            if failed_length_checks(item)
        ]
        self.add_final_unresolved_forbidden(items, final_forbidden)
        self.add_final_unresolved_format(items, final_format_failures)

        modules = group_modules(items)
        self.content_result.content_items = items
        self.content_result.modules = modules
        self.content_result.drafts = build_legacy_drafts(modules)
        self.content_result.minimal_checks = {
            **(run_minimal_checks(items) or {}),
            "qa_engine_applied": True,
            "qa_version": QA_VERSION,
            "format_rules_version": FORMAT_RULES_VERSION,
        }

        unresolved = dedupe_issues(self.state.unresolved_issues)
        self.state.unresolved_issues = unresolved
        unresolved_item_ids = {str(issue.get("content_item_id") or "") for issue in unresolved if issue.get("content_item_id")}
        checked_count = len(items)
        passed_count = max(0, checked_count - len(unresolved_item_ids))
        overall_risk = derive_overall_risk(unresolved, self.state)
        status = "completed_with_issues" if unresolved else "completed"
        summary = (
            f"QA completed: checked {checked_count} items, "
            f"corrected {len(self.state.corrected_item_ids)} items, "
            f"{len(unresolved)} unresolved issue(s), overall risk {overall_risk}."
        )

        emit_progress(progress, "QA\u5b8c\u6210")
        return QAResult(
            status=status,
            summary=summary,
            qa_version=QA_VERSION,
            generated_at=datetime.utcnow(),
            checked_items=checked_count,
            passed_items=passed_count,
            corrected_items=len(self.state.corrected_item_ids),
            unresolved_issues=unresolved,
            correction_history=self.state.correction_history,
            parameter_issues=self.state.parameter_issues,
            forbidden_word_issues=self.state.forbidden_word_issues,
            translation_issues=self.state.translation_issues,
            format_issues=self.state.format_issues,
            strategy_issues=self.state.strategy_issues,
            overall_risk=overall_risk,
            data_quality=self.build_data_quality(items),
            raw_payload={
                "qa_version": QA_VERSION,
                "format_rules_version": FORMAT_RULES_VERSION,
                "forbidden_word_source": self.forbidden_source,
                "forbidden_word_count": len(self.forbidden_words),
                "translation_semantic_check": self.ai_translation_meta,
                "fact_index_summary": self.fact_index.summary(),
                "final_forbidden_hit_count": sum(len(value) for value in final_forbidden.values()),
                "final_format_failure_count": len(final_format_failures),
                "qa_boundaries": [
                    "QA reads Planning Context, ResearchResult, StrategyResult, and ContentResult only.",
                    "Specific product parameters are checked against Task 2 structured facts.",
                    "Resolved auto-corrections are kept in correction_history and not repeated in unresolved_issues.",
                    "Unresolved QA issues do not fail the workflow; they are carried forward for Task 7 export marking.",
                ],
            },
            issues=unresolved,
        )

    def normalize_item(self, item: dict[str, Any]) -> dict[str, Any]:
        guarded = apply_project_fact_guardrails(item, self.planning_context)
        return attach_length_metadata(guarded)

    def check_parameters(self, items: list[dict[str, Any]]) -> None:
        for item in items:
            self.check_brand_placeholder(item)
            self.check_measurements(item)
            self.check_sku_variants(item)
            self.check_unsupported_product_facts(item)

    def check_brand_placeholder(self, item: dict[str, Any]) -> None:
        english = collect_english_text(item)
        if not re.search(r"(?<![A-Za-z])Brand(?![A-Za-z])", english):
            return
        if item.get("module") != "Title":
            return
        if self.brand:
            before = collect_english_text(item)
            replace_text_everywhere(item, {r"(?<![A-Za-z])Brand(?![A-Za-z])": self.brand})
            updated = self.normalize_item(item)
            item.clear()
            item.update(updated)
            issue = build_issue(
                item,
                issue_type="brand_placeholder_corrected",
                risk_level="low",
                problematic_text="Brand",
                issue_description=f"Brand placeholder was replaced with configured brand '{self.brand}'.",
                suggested_action="No action required unless this project needs a different brand.",
                category="parameter_issues",
                status="corrected",
                rewrite_attempts=[{"round": 1, "method": "default_brand_replacement"}],
            )
            self.state.add_category_issue("parameter_issues", issue)
            self.state.add_correction(
                item,
                {
                    "issue_type": "brand_placeholder",
                    "round": 1,
                    "method": "default_brand_replacement",
                    "english_before": before,
                    "english_after": collect_english_text(item),
                    "brand": self.brand,
                },
            )
            return
        issue = build_issue(
            item,
            issue_type="brand_missing",
            risk_level="medium",
            problematic_text="Brand",
            issue_description="Item Name uses the required literal Brand placeholder because no verified brand was found in Task 2 product facts.",
            suggested_action="Replace Brand with the verified brand before final export or publishing.",
            category="parameter_issues",
            status="unresolved",
            rewrite_attempts=[],
        )
        self.state.add_category_issue("parameter_issues", issue)

    def check_measurements(self, item: dict[str, Any]) -> None:
        measurements = unique(extract_measurements(collect_english_text(item)) + extract_measurements(collect_chinese_text(item)))
        if not measurements:
            return
        if not item.get("parameter_reference") and item.get("sku_scope") != "SKU_SPECIFIC":
            issue = build_issue(
                item,
                issue_type="missing_parameter_reference",
                risk_level="high",
                problematic_text=", ".join(measurements[:4]),
                issue_description="Specific numeric parameter appears in copy but the content item has no parameter_reference.",
                suggested_action="Attach the matching Task 2 parameter reference or remove the unverified parameter.",
                category="parameter_issues",
                status="unresolved",
                rewrite_attempts=[],
            )
            self.state.add_category_issue("parameter_issues", issue)

        sku_scope = str(item.get("sku_scope") or "ALL")
        if sku_scope == "ALL":
            for measurement in measurements:
                if measurement in self.fact_index.common_measurements:
                    continue
                if measurement in self.fact_index.all_measurements and measurement not in self.fact_index.common_measurements:
                    fixed = self.remove_all_scope_sku_measurement(item, measurement)
                    if fixed:
                        self.state.add_category_issue(
                            "parameter_issues",
                            {
                                **build_issue(
                                    item,
                                    issue_type="sku_parameter_misuse_corrected",
                                    risk_level="high",
                                    problematic_text=measurement,
                                    issue_description="SKU-specific measurement appeared in ALL-scope copy and was replaced with a selected-SKU reminder.",
                                    suggested_action="Review the corrected wording during QA.",
                                    category="parameter_issues",
                                    status="corrected",
                                    rewrite_attempts=[{"round": 1, "method": "local_safe_parameter_rewrite"}],
                                ),
                                "status": "corrected",
                            },
                        )
                    else:
                        issue = build_issue(
                            item,
                            issue_type="sku_parameter_misuse",
                            risk_level="high",
                            problematic_text=measurement,
                            issue_description="SKU-specific measurement appears in ALL-scope copy.",
                            suggested_action="Move the parameter into SKU-specific copy or replace it with a selected-SKU reminder.",
                            category="parameter_issues",
                            status="unresolved",
                            rewrite_attempts=[],
                        )
                        self.state.add_category_issue("parameter_issues", issue)
                elif measurement not in self.fact_index.all_measurements:
                    issue = build_issue(
                        item,
                        issue_type="unverified_parameter_value",
                        risk_level="high",
                        problematic_text=measurement,
                        issue_description="Numeric parameter was not found in Task 2 structured product facts.",
                        suggested_action="Verify against the source spec sheet and add a parameter_reference, or remove the value.",
                        category="parameter_issues",
                        status="unresolved",
                        rewrite_attempts=[],
                    )
                    self.state.add_category_issue("parameter_issues", issue)

    def check_sku_variants(self, item: dict[str, Any]) -> None:
        variants = item.get("sku_copy_variants") or []
        if not isinstance(variants, list):
            return
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            sku_id = str(variant.get("sku_id") or "")
            text = f"{variant.get('english_copy') or ''}\n{variant.get('chinese_copy') or ''}"
            measurements = unique(extract_measurements(text))
            if measurements and not variant.get("parameter_reference"):
                issue = build_variant_issue(
                    item,
                    variant,
                    issue_type="sku_variant_missing_parameter_reference",
                    risk_level="high",
                    problematic_text=", ".join(measurements[:4]),
                    issue_description="SKU-specific copy contains numeric parameters but no variant parameter_reference.",
                    suggested_action="Attach the matching SKU parameter reference before export.",
                    status="unresolved",
                )
                self.state.add_category_issue("parameter_issues", issue)
                continue
            allowed = self.fact_index.sku_measurements.get(sku_id, set())
            for measurement in measurements:
                if measurement in allowed or measurement in self.fact_index.common_measurements:
                    continue
                source_sku = self.fact_index.measurement_to_skus.get(measurement, set())
                issue_type = "sku_parameter_cross_use" if source_sku else "unverified_sku_parameter"
                desc = (
                    f"Measurement belongs to another SKU: {', '.join(sorted(source_sku))}."
                    if source_sku
                    else "Measurement was not found in Task 2 SKU facts."
                )
                issue = build_variant_issue(
                    item,
                    variant,
                    issue_type=issue_type,
                    risk_level="high",
                    problematic_text=measurement,
                    issue_description=desc,
                    suggested_action="Replace with the selected SKU's verified parameter or request manual confirmation.",
                    status="unresolved",
                )
                self.state.add_category_issue("parameter_issues", issue)

    def check_unsupported_product_facts(self, item: dict[str, Any]) -> None:
        text = collect_english_text(item)
        fact_text = self.fact_index.raw_fact_text.lower()
        local_replacements = {
            r"\bcedar wood\b": "fir wood",
            r"\bcedar frame\b": "fir wood frame",
            r"\bcedar\b": "fir wood",
        }
        if "cedar" in text.lower() and "cedar" not in fact_text:
            before = collect_english_text(item)
            replace_text_everywhere(item, local_replacements)
            sync_ok = self.sync_chinese_if_needed(item, before, "cedar_to_fir_wood")
            corrected = self.normalize_item(item)
            item.clear()
            item.update(corrected)
            self.state.add_correction(
                item,
                {
                    "issue_type": "unsupported_material_corrected",
                    "round": 1,
                    "method": "local_fact_guardrail",
                    "english_before": before,
                    "english_after": collect_english_text(item),
                    "chinese_synced": sync_ok,
                },
            )

        unsupported_patterns = {
            "tempered glass": "No verified tempered glass fact exists.",
            "glass panel": "No verified glass panel fact exists.",
            "aluminum frame": "No verified aluminum frame fact exists.",
            "steel frame": "No verified steel frame fact exists.",
            "heavy-duty": "Heavy-duty is a performance positioning claim that needs proof.",
            "commercial-grade": "Commercial-grade is a product grade claim that needs proof.",
            "certified": "Certification claim requires verified certification evidence.",
            "certification": "Certification claim requires verified certification evidence.",
            "warranty": "Warranty claim requires verified warranty evidence.",
        }
        for phrase, desc in unsupported_patterns.items():
            if not re.search(rf"(?i)(?<![A-Za-z]){re.escape(phrase)}(?![A-Za-z])", text):
                continue
            if phrase.lower() in fact_text:
                continue
            issue = build_issue(
                item,
                issue_type="unsupported_product_claim",
                risk_level="high",
                problematic_text=phrase,
                issue_description=desc,
                suggested_action="Remove or verify the claim before final export.",
                category="parameter_issues",
                status="unresolved",
                rewrite_attempts=[],
            )
            self.state.add_category_issue("parameter_issues", issue)

    def remove_all_scope_sku_measurement(self, item: dict[str, Any], measurement: str) -> bool:
        changed = False
        before = collect_english_text(item)
        replacement = "selected SKU size"
        for segment in iter_english_segments(item):
            text = segment["text"]
            if measurement not in extract_measurements(text):
                continue
            new_text = replace_measurement_text(text, measurement, replacement)
            if new_text != text:
                set_segment_text(item, segment, english_copy=new_text)
                changed = True
        if not changed:
            return False
        self.sync_chinese_if_needed(item, before, "sku_parameter_misuse")
        self.state.add_correction(
            item,
            {
                "issue_type": "sku_parameter_misuse",
                "round": 1,
                "method": "local_selected_sku_rewrite",
                "problematic_text": measurement,
            },
        )
        return True

    def check_forbidden_words(self, items: list[dict[str, Any]]) -> None:
        initial_hits = self.find_forbidden_hits(items)
        if not initial_hits:
            return
        emit_progress(None, "\u6b63\u5728\u8fdb\u884c\u81ea\u52a8\u4fee\u6b63")
        for item in items:
            item_hits = initial_hits.get(str(item.get("id") or ""), [])
            if not item_hits:
                continue
            corrected_item, attempts = self.rewrite_forbidden_item(item, item_hits)
            item.clear()
            item.update(corrected_item)
            remaining = self.find_forbidden_hits([item]).get(str(item.get("id") or ""), [])
            if remaining:
                issue = build_issue(
                    item,
                    issue_type="forbidden_word_unresolved",
                    risk_level=max_risk([hit["risk_level"] for hit in remaining]),
                    problematic_text=", ".join(unique([hit["word"] for hit in remaining])[:6]),
                    issue_description="Forbidden or sensitive word remains after the maximum QA rewrite rounds.",
                    suggested_action="Manually rewrite the marked word or phrase before final publishing.",
                    category="forbidden_word_issues",
                    status="unresolved",
                    rewrite_attempts=attempts,
                )
                issue["forbidden_hits"] = remaining
                self.state.add_category_issue("forbidden_word_issues", issue)
            else:
                issue = build_issue(
                    item,
                    issue_type="forbidden_word_corrected",
                    risk_level=max_risk([hit["risk_level"] for hit in item_hits]),
                    problematic_text=", ".join(unique([hit["word"] for hit in item_hits])[:6]),
                    issue_description="Forbidden or sensitive word was corrected automatically.",
                    suggested_action="No action required unless manual review wants alternate wording.",
                    category="forbidden_word_issues",
                    status="corrected",
                    rewrite_attempts=attempts,
                )
                self.state.add_category_issue("forbidden_word_issues", issue)
                self.state.add_correction(
                    item,
                    {
                        "issue_type": "forbidden_word",
                        "rounds": len(attempts),
                        "method": attempts[-1]["method"] if attempts else "",
                        "forbidden_words": unique([hit["word"] for hit in item_hits]),
                        "rewrite_attempts": attempts,
                    },
                )

    def rewrite_forbidden_item(self, item: dict[str, Any], hits: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        current = json_clone(item)
        attempts: list[dict[str, Any]] = []
        for round_index in range(1, MAX_QA_REWRITE_ROUNDS + 1):
            before = collect_english_text(current)
            method = {1: "local_safe_rewrite", 2: "openai_full_sentence_rewrite", 3: "openai_angle_shift_rewrite"}[round_index]
            if round_index == 1:
                revised = self.local_forbidden_rewrite(current, hits)
            else:
                revised = self.openai_rewrite_issue(
                    current,
                    issue_type="forbidden_word",
                    issue_payload={
                        "forbidden_hits": hits,
                        "round_strategy": (
                            "Rewrite the entire sentence while preserving facts."
                            if round_index == 2
                            else "Change the expression angle while preserving the StrategyResult and verified product facts."
                        ),
                    },
                    round_index=round_index,
                )
            if revised is None:
                attempts.append({"round": round_index, "method": method, "status": "failed_to_generate"})
                continue
            revised = self.normalize_item(revised)
            remaining = self.find_forbidden_hits([revised]).get(str(revised.get("id") or ""), [])
            attempts.append(
                {
                    "round": round_index,
                    "method": method,
                    "status": "passed" if not remaining else "still_has_forbidden_words",
                    "remaining_words": unique([hit["word"] for hit in remaining]),
                    "english_before": before,
                    "english_after": collect_english_text(revised),
                }
            )
            current = revised
            if not remaining:
                break
        return current, attempts

    def local_forbidden_rewrite(self, item: dict[str, Any], hits: list[dict[str, Any]]) -> dict[str, Any]:
        revised = json_clone(item)
        replacements = {}
        for hit in hits:
            word = str(hit.get("word") or "")
            replacements[rf"(?i)(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])"] = safe_forbidden_replacement(word)
        before = collect_english_text(revised)
        replace_text_everywhere(revised, replacements)
        sync_ok = self.sync_chinese_if_needed(revised, before, "forbidden_word_local_rewrite")
        if sync_ok:
            revised["qa_chinese_synced"] = True
        return revised

    def openai_rewrite_issue(self, item: dict[str, Any], *, issue_type: str, issue_payload: dict[str, Any], round_index: int) -> dict[str, Any] | None:
        agent = self.get_ai_agent()
        if agent is None:
            return None
        payload = {
            "task": "Rewrite one ContentResult item for QA.",
            "issue_type": issue_type,
            "round": round_index,
            "content_item": compact_item_for_qa(item),
            "issue_payload": issue_payload,
            "allowed_product_facts": self.fact_index.allowed_facts[:120],
            "strategy_context": compact_strategy_context(self.strategy_result),
            "instructions": [
                "Return only JSON.",
                "Revise English first, then generate Chinese from the final English.",
                "Do not change SKU scope, strategy references, selling point rank, product facts, or parameter values.",
                "Do not add unsupported certifications, effects, comparisons, warranties, market numbers, ASINs, BSR, or review percentages.",
                "Keep all existing character and format rules.",
                "For Bullet Point keep Title: Description and no terminal punctuation.",
            ],
        }
        return agent.rewrite(payload)

    def check_translation_consistency(self, items: list[dict[str, Any]]) -> None:
        self.check_deterministic_translation(items)
        if os.getenv("SUPER_PLANNER_QA_AI_SEMANTIC", "1").strip() not in {"1", "true", "TRUE", "yes", "on"}:
            self.ai_translation_meta = {"mode": "disabled_by_env"}
            return
        agent = self.get_ai_agent()
        if agent is None:
            self.ai_translation_meta = {"mode": "openai_batch", "status": "unavailable"}
            return
        try:
            payload = {
                "task": "Check English and Chinese ContentResult copy for semantic equivalence.",
                "items": [compact_item_for_translation_check(item) for item in items],
                "rules": [
                    "Return issues only; empty issues means pass.",
                    "Do not flag natural translation differences.",
                    "Flag mismatched numbers, units, model names, materials, certifications, unsupported promises, or reversed meanings.",
                    "If English is safe and Chinese needs correction, provide suggested_chinese_copy or suggested_copy_parts.",
                ],
            }
            result = agent.translation_check(payload)
            issues = result.get("issues", []) if isinstance(result, dict) else []
            self.ai_translation_meta = {
                "mode": "openai_batch",
                "status": "completed",
                "model": agent.model,
                "issue_count": len(issues),
                "api_calls": agent.calls,
            }
            for raw_issue in issues:
                if not isinstance(raw_issue, dict):
                    continue
                item = find_item(items, str(raw_issue.get("id") or raw_issue.get("item_id") or ""))
                if not item:
                    continue
                issue = build_issue(
                    item,
                    issue_type="translation_semantic_mismatch",
                    risk_level=normalize_risk(raw_issue.get("risk_level"), "medium"),
                    problematic_text=str(raw_issue.get("problematic_text") or raw_issue.get("problematic_span") or collect_chinese_text(item)[:80]),
                    issue_description=str(raw_issue.get("issue_description") or raw_issue.get("reason") or "OpenAI semantic check found an English/Chinese mismatch."),
                    suggested_action=str(raw_issue.get("suggested_action") or "Review and align the Chinese copy to final English."),
                    category="translation_issues",
                    status="unresolved",
                    rewrite_attempts=[],
                )
                if self.apply_translation_suggestion(item, raw_issue):
                    issue["status"] = "corrected"
                    self.state.add_correction(
                        item,
                        {
                            "issue_type": "translation_semantic_mismatch",
                            "round": 1,
                            "method": "openai_chinese_resync",
                            "issue_description": issue["issue_description"],
                        },
                    )
                self.state.add_category_issue("translation_issues", issue)
        except Exception as exc:  # noqa: BLE001
            self.ai_translation_meta = {
                "mode": "openai_batch",
                "status": "failed_fallback_to_deterministic",
                "error": sanitize_error(exc),
            }

    def check_deterministic_translation(self, items: list[dict[str, Any]]) -> None:
        for item in items:
            english = collect_english_text(item)
            chinese = collect_chinese_text(item)
            en_measurements = sorted(set(extract_measurements(english)))
            cn_measurements = sorted(set(extract_measurements(chinese)))
            if en_measurements != cn_measurements:
                issue = build_issue(
                    item,
                    issue_type="translation_numeric_or_unit_mismatch",
                    risk_level="high",
                    problematic_text=", ".join(sorted(set(en_measurements).symmetric_difference(cn_measurements))[:6]),
                    issue_description="English and Chinese copy do not contain the same numeric/unit facts.",
                    suggested_action="Synchronize Chinese from the final English and recheck numbers and units.",
                    category="translation_issues",
                    status="unresolved",
                    rewrite_attempts=[],
                )
                self.state.add_category_issue("translation_issues", issue)

            en_models = sorted(set(extract_model_codes(english)))
            cn_models = sorted(set(extract_model_codes(chinese)))
            if en_models != cn_models:
                issue = build_issue(
                    item,
                    issue_type="translation_model_or_sku_mismatch",
                    risk_level="high",
                    problematic_text=", ".join(sorted(set(en_models).symmetric_difference(cn_models))[:6]),
                    issue_description="English and Chinese copy do not contain the same SKU/model codes.",
                    suggested_action="Synchronize SKU/model text before final export.",
                    category="translation_issues",
                    status="unresolved",
                    rewrite_attempts=[],
                )
                self.state.add_category_issue("translation_issues", issue)

            for term, aliases in TRANSLATION_GLOSSARY.items():
                if term not in english.lower():
                    continue
                if any(alias in chinese for alias in aliases):
                    continue
                issue = build_issue(
                    item,
                    issue_type="translation_terminology_mismatch",
                    risk_level="medium",
                    problematic_text=term,
                    issue_description=f"English uses canonical term '{term}', but Chinese copy lacks the expected mapped term.",
                    suggested_action="Align Chinese terminology with the project glossary.",
                    category="translation_issues",
                    status="unresolved",
                    rewrite_attempts=[],
                )
                self.state.add_category_issue("translation_issues", issue)

            for variant in item.get("sku_copy_variants") or []:
                if not isinstance(variant, dict):
                    continue
                en_variant = str(variant.get("english_copy") or "")
                cn_variant = str(variant.get("chinese_copy") or "")
                en_variant_measurements = sorted(set(extract_measurements(en_variant)))
                cn_variant_measurements = sorted(set(extract_measurements(cn_variant)))
                if en_variant_measurements == cn_variant_measurements:
                    continue
                issue = build_variant_issue(
                    item,
                    variant,
                    issue_type="translation_numeric_or_unit_mismatch",
                    risk_level="high",
                    problematic_text=", ".join(sorted(set(en_variant_measurements).symmetric_difference(cn_variant_measurements))[:6]),
                    issue_description="SKU variant English and Chinese copy do not contain the same numeric/unit facts.",
                    suggested_action="Synchronize the SKU-specific Chinese copy before export.",
                    category="translation_issues",
                    status="unresolved",
                )
                self.state.add_category_issue("translation_issues", issue)

    def apply_translation_suggestion(self, item: dict[str, Any], raw_issue: dict[str, Any]) -> bool:
        if raw_issue.get("judgement") == "uncertain":
            return False
        changed = False
        suggested_parts = raw_issue.get("suggested_copy_parts")
        if isinstance(suggested_parts, dict) and isinstance(item.get("copy_parts"), dict):
            for name, value in suggested_parts.items():
                if name in item["copy_parts"] and isinstance(value, str) and value.strip():
                    if same_numbers_and_units(item["copy_parts"][name].get("english_copy", ""), value):
                        item["copy_parts"][name]["chinese_copy"] = value.strip()
                        changed = True
        suggested_cn = str(raw_issue.get("suggested_chinese_copy") or "").strip()
        if suggested_cn and not isinstance(item.get("copy_parts"), dict):
            if same_numbers_and_units(item.get("english_copy", ""), suggested_cn):
                item["chinese_copy"] = suggested_cn
                changed = True
        if changed:
            item.update(self.normalize_item(item))
        return changed

    def check_format(self, items: list[dict[str, Any]]) -> None:
        generator: OpenAICopyGenerator | None = None
        for item in items:
            before = json_clone(item)
            formatted = self.normalize_item(item)
            if collect_english_text(before) != collect_english_text(formatted):
                self.state.add_correction(
                    formatted,
                    {
                        "issue_type": "format_normalization",
                        "round": 1,
                        "method": "local_formatter",
                        "english_before": collect_english_text(before),
                        "english_after": collect_english_text(formatted),
                    },
                )
            item.clear()
            item.update(formatted)
            failures = failed_length_checks(item)
            if failures:
                attempts: list[dict[str, Any]] = []
                if generator is None:
                    generator = OpenAICopyGenerator()
                current = json_clone(item)
                for round_index in range(1, MAX_QA_REWRITE_ROUNDS + 1):
                    try:
                        repaired = generator.repair_length(
                            current,
                            failures,
                            self.qa_repair_context(),
                            round_index,
                        )
                    except Exception as exc:  # noqa: BLE001
                        attempts.append({"round": round_index, "method": "openai_length_repair", "status": "failed", "error": sanitize_error(exc)})
                        continue
                    if not repaired:
                        attempts.append({"round": round_index, "method": "openai_length_repair", "status": "empty_response"})
                        continue
                    candidate = self.normalize_item(merge_repair_payload(current, repaired))
                    next_failures = failed_length_checks(candidate)
                    attempts.append(
                        {
                            "round": round_index,
                            "method": "openai_length_repair",
                            "status": "passed" if not next_failures else "still_failed",
                            "remaining_failures": next_failures,
                        }
                    )
                    current = candidate
                    failures = next_failures
                    if not failures:
                        break
                item.clear()
                item.update(current)
                if failures:
                    issue = build_issue(
                        item,
                        issue_type="length_rule_unresolved",
                        risk_level="medium",
                        problematic_text=collect_english_text(item)[:120],
                        issue_description="Copy still violates character limits after maximum QA rewrite rounds.",
                        suggested_action="Manually shorten or expand the marked field before final export.",
                        category="format_issues",
                        status="unresolved",
                        rewrite_attempts=attempts,
                    )
                    issue["failed_length_checks"] = failures
                    self.state.add_category_issue("format_issues", issue)
                else:
                    self.state.add_category_issue(
                        "format_issues",
                        {
                            **build_issue(
                                item,
                                issue_type="length_rule_corrected",
                                risk_level="low",
                                problematic_text="character length",
                                issue_description="Length rule was corrected automatically.",
                                suggested_action="No action required.",
                                category="format_issues",
                                status="corrected",
                                rewrite_attempts=attempts,
                            ),
                            "status": "corrected",
                        },
                    )
                    self.state.add_correction(
                        item,
                        {
                            "issue_type": "length_rule",
                            "rounds": len(attempts),
                            "method": "openai_length_repair",
                            "rewrite_attempts": attempts,
                        },
                    )

            if item.get("module") == "Bullet Point":
                self.check_bullet_format(item)

    def check_bullet_format(self, item: dict[str, Any]) -> None:
        text = str(item.get("english_copy") or "")
        problems = []
        if ":" not in text:
            problems.append("missing_colon")
        if text.rstrip().endswith((".", "!", "?", ";")):
            problems.append("terminal_punctuation")
        label = text.split(":", 1)[0].strip() if ":" in text else ""
        if label and label != title_case_like(label):
            problems.append("heading_not_title_case")
        if not problems:
            return
        issue = build_issue(
            item,
            issue_type="bullet_format_issue",
            risk_level="medium",
            problematic_text=text[:120],
            issue_description=f"Bullet Point format issue: {', '.join(problems)}.",
            suggested_action="Use Title: Description, Title Case heading, clear subject, no terminal punctuation.",
            category="format_issues",
            status="unresolved",
            rewrite_attempts=[],
        )
        self.state.add_category_issue("format_issues", issue)

    def check_strategy_consistency(self, items: list[dict[str, Any]]) -> None:
        self.check_bullet_rank_order(items)
        self.check_primary_module_alignment(items)
        self.check_new_claims_against_strategy(items)
        self.check_excessive_repetition(items)

    def check_bullet_rank_order(self, items: list[dict[str, Any]]) -> None:
        bullet_items = sorted(
            [item for item in items if item.get("module") == "Bullet Point"],
            key=lambda item: int(item.get("sequence") or 999),
        )
        for expected_rank, item in enumerate(bullet_items[:5], start=1):
            ranks = extract_selling_ranks(item)
            if expected_rank in ranks:
                continue
            issue = build_issue(
                item,
                issue_type="selling_point_rank_order_mismatch",
                risk_level="medium",
                problematic_text=str(item.get("submodule") or item.get("english_copy") or ""),
                issue_description=f"Bullet Point {expected_rank} does not carry Strategy TOP{expected_rank}.",
                suggested_action="Do not reorder selling points in Content; align BP order with StrategyResult.",
                category="strategy_issues",
                status="unresolved",
                rewrite_attempts=[],
            )
            self.state.add_category_issue("strategy_issues", issue)

    def check_primary_module_alignment(self, items: list[dict[str, Any]]) -> None:
        for item in items:
            expected_rank = expected_primary_rank(item)
            if expected_rank is None:
                continue
            text = collect_english_text(item).lower()
            terms = RANK_ALIGNMENT_TERMS.get(expected_rank, [])
            if any(term in text for term in terms):
                continue
            issue = build_issue(
                item,
                issue_type="primary_module_strategy_drift",
                risk_level="medium",
                problematic_text=collect_english_text(item)[:120],
                issue_description=f"This module is mapped to Strategy TOP{expected_rank}, but its copy does not clearly express that theme.",
                suggested_action="Rewrite within the existing Content Mapping; do not create a new strategy.",
                category="strategy_issues",
                status="unresolved",
                rewrite_attempts=[],
            )
            self.state.add_category_issue("strategy_issues", issue)

    def check_new_claims_against_strategy(self, items: list[dict[str, Any]]) -> None:
        allowed_text = json.dumps(
            {
                "facts": self.fact_index.allowed_facts,
                "strategy": compact_strategy_context(self.strategy_result),
                "research_keywords": self.research_result.keyword_context,
            },
            ensure_ascii=False,
        ).lower()
        claim_patterns = {
            "certified": "Certification claim not present in StrategyResult/facts.",
            "certification": "Certification claim not present in StrategyResult/facts.",
            "warranty": "Warranty claim not present in StrategyResult/facts.",
            "waterproof": "Waterproof claim not present in StrategyResult/facts.",
            "weatherproof": "Weatherproof claim not present in StrategyResult/facts.",
            "uv resistant": "UV resistance claim not present in StrategyResult/facts.",
            "rustproof": "Rustproof claim not present in StrategyResult/facts.",
            "pest": "Pest-control claim not present in StrategyResult/facts.",
            "mold": "Mold-related claim not present in StrategyResult/facts.",
            "guarantee": "Guarantee claim not present in StrategyResult/facts.",
            "best": "Absolute superiority claim not present in StrategyResult/facts.",
        }
        for item in items:
            text = collect_english_text(item)
            lower = text.lower()
            for phrase, desc in claim_patterns.items():
                if phrase not in lower:
                    continue
                if phrase in allowed_text:
                    continue
                issue = build_issue(
                    item,
                    issue_type="unsupported_new_strategy_claim",
                    risk_level="high",
                    problematic_text=phrase,
                    issue_description=desc,
                    suggested_action="Remove the claim or add verified evidence in an earlier workflow stage.",
                    category="strategy_issues",
                    status="unresolved",
                    rewrite_attempts=[],
                )
                self.state.add_category_issue("strategy_issues", issue)

    def check_excessive_repetition(self, items: list[dict[str, Any]]) -> None:
        seen: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            text = normalize_repetition_text(collect_english_text(item))
            if len(text) < 45:
                continue
            seen.setdefault(text, []).append(item)
        for text, repeated_items in seen.items():
            if len(repeated_items) < 2:
                continue
            first = repeated_items[0]
            issue = build_issue(
                first,
                issue_type="mechanical_repetition",
                risk_level="low",
                problematic_text=text[:120],
                issue_description=f"Same or near-identical copy appears in {len(repeated_items)} modules.",
                suggested_action="Vary module expression while keeping the same StrategyResult.",
                category="strategy_issues",
                status="unresolved",
                rewrite_attempts=[],
            )
            issue["related_items"] = [item.get("id") for item in repeated_items]
            self.state.add_category_issue("strategy_issues", issue)

    def find_forbidden_hits(self, items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        if not self.forbidden_words:
            return {}
        compiled = [(entry, compile_forbidden_pattern(entry.word)) for entry in self.forbidden_words if entry.word]
        hits: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            item_id = str(item.get("id") or "")
            segments = iter_english_segments(item)
            for variant in item.get("sku_copy_variants") or []:
                if isinstance(variant, dict):
                    segments.append(
                        {
                            "field": f"sku_copy_variants.{variant.get('sku_id')}.english_copy",
                            "part": None,
                            "sku_id": str(variant.get("sku_id") or ""),
                            "text": str(variant.get("english_copy") or ""),
                            "length_rule_id": "unbounded",
                        }
                    )
            for segment in segments:
                text = segment["text"]
                if not re.search(r"[A-Za-z]", text):
                    continue
                for entry, pattern in compiled:
                    for match in pattern.finditer(text):
                        hits.setdefault(item_id, []).append(
                            {
                                "word": entry.word,
                                "risk_level": entry.risk_level,
                                "meaning": entry.meaning,
                                "solution": entry.solution,
                                "source": entry.source,
                                "field": segment["field"],
                                "part": segment.get("part"),
                                "start": match.start(),
                                "end": match.end(),
                                "snippet": snippet(text, match.start(), match.end()),
                            }
                        )
        return hits

    def add_final_unresolved_forbidden(self, items: list[dict[str, Any]], hits: dict[str, list[dict[str, Any]]]) -> None:
        existing_keys = {
            (
                issue.get("content_item_id"),
                issue.get("issue_type"),
                issue.get("problematic_text"),
                issue.get("status"),
            )
            for issue in self.state.forbidden_word_issues
        }
        for item in items:
            item_hits = hits.get(str(item.get("id") or ""), [])
            if not item_hits:
                continue
            words = ", ".join(unique([hit["word"] for hit in item_hits])[:6])
            key = (item.get("id"), "forbidden_word_unresolved", words, "unresolved")
            if key in existing_keys:
                continue
            issue = build_issue(
                item,
                issue_type="forbidden_word_unresolved",
                risk_level=max_risk([hit["risk_level"] for hit in item_hits]),
                problematic_text=words,
                issue_description="Forbidden or sensitive word remains in final QA copy.",
                suggested_action="Manually rewrite the marked word or phrase before final publishing.",
                category="forbidden_word_issues",
                status="unresolved",
                rewrite_attempts=[],
            )
            issue["forbidden_hits"] = item_hits
            self.state.add_category_issue("forbidden_word_issues", issue)

    def add_final_unresolved_format(self, items: list[dict[str, Any]], failures: list[dict[str, Any]]) -> None:
        if not failures:
            return
        existing = {
            (issue.get("content_item_id"), issue.get("issue_type"))
            for issue in self.state.format_issues
            if issue.get("status") == "unresolved"
        }
        item_by_id = {str(item.get("id") or ""): item for item in items}
        for failure in failures:
            item = item_by_id.get(str(failure.get("id") or ""))
            if not item or (item.get("id"), "length_rule_unresolved") in existing:
                continue
            issue = build_issue(
                item,
                issue_type="length_rule_unresolved",
                risk_level="medium",
                problematic_text=collect_english_text(item)[:120],
                issue_description="Copy violates configured character rules.",
                suggested_action="Manually adjust field length before final export.",
                category="format_issues",
                status="unresolved",
                rewrite_attempts=[],
            )
            issue["failed_length_checks"] = failure.get("failed_length_checks", [])
            self.state.add_category_issue("format_issues", issue)

    def sync_chinese_if_needed(self, item: dict[str, Any], before_english: str, reason: str) -> bool:
        after_english = collect_english_text(item)
        if before_english and before_english == after_english:
            return True
        agent = self.get_ai_agent()
        if agent is None:
            if before_english and before_english != after_english:
                self.add_chinese_sync_failure(item, reason, [])
            return False
        try:
            synced = agent.sync_chinese(
                {
                    "reason": reason,
                    "content_item": compact_item_for_qa(item),
                    "instruction": "Generate Chinese from the final English only. Keep numbers, units, product facts, and SKU scope identical.",
                }
            )
        except Exception:
            if before_english and before_english != after_english:
                self.add_chinese_sync_failure(item, reason, [])
            return False
        if not isinstance(synced, dict):
            if before_english and before_english != after_english:
                self.add_chinese_sync_failure(item, reason, [])
            return False
        changed = False
        if isinstance(item.get("copy_parts"), dict) and isinstance(synced.get("copy_parts"), dict):
            for name, value in synced["copy_parts"].items():
                if name in item["copy_parts"] and isinstance(value, dict) and value.get("chinese_copy"):
                    item["copy_parts"][name]["chinese_copy"] = str(value["chinese_copy"]).strip()
                    changed = True
        elif synced.get("chinese_copy"):
            item["chinese_copy"] = str(synced["chinese_copy"]).strip()
            changed = True
        if changed:
            item.update(attach_length_metadata(item))
        elif before_english and before_english != after_english:
            self.add_chinese_sync_failure(item, reason, [])
        return changed

    def add_chinese_sync_failure(self, item: dict[str, Any], reason: str, attempts: list[dict[str, Any]]) -> None:
        issue = build_issue(
            item,
            issue_type="chinese_sync_after_english_correction_failed",
            risk_level="high",
            problematic_text=collect_chinese_text(item)[:120],
            issue_description=f"English copy changed during QA correction, but Chinese synchronization did not complete for reason: {reason}.",
            suggested_action="Regenerate or manually align Chinese copy from the final English before export.",
            category="translation_issues",
            status="unresolved",
            rewrite_attempts=attempts,
        )
        self.state.add_category_issue("translation_issues", issue)

    def qa_repair_context(self) -> dict[str, Any]:
        return {
            "qa_version": QA_VERSION,
            "allowed_product_facts": self.fact_index.allowed_facts[:120],
            "strategy_context": compact_strategy_context(self.strategy_result),
            "format_rules_version": FORMAT_RULES_VERSION,
        }

    def get_ai_agent(self) -> "OpenAIQAAgent | None":
        if self.ai_agent is not None:
            return self.ai_agent
        try:
            agent = OpenAIQAAgent()
            if not agent.available:
                return None
            self.ai_agent = agent
            return agent
        except Exception:
            return None

    def build_data_quality(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "status": "usable_with_unresolved_items" if self.state.unresolved_issues else "passed",
            "checked_content_items": len(items),
            "sku_variant_count": sum(len(item.get("sku_copy_variants") or []) for item in items),
            "fact_source": "planning_context.standard_chinese_parameter_table",
            "specific_parameters_must_have_parameter_reference": True,
            "forbidden_word_source": self.forbidden_source,
            "forbidden_word_count": len(self.forbidden_words),
            "translation_check": self.ai_translation_meta,
            "unresolved_issue_count": len(self.state.unresolved_issues),
            "correction_count": len(self.state.correction_history),
        }


class OpenAIQAAgent:
    def __init__(self) -> None:
        self.generator = OpenAICopyGenerator()
        self.available = bool(self.generator.settings.api_key and self.generator.client is not None)
        self.model = self.generator.settings.model

    @property
    def calls(self) -> list[dict[str, Any]]:
        return list(self.generator.calls)

    def rewrite(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        output, _meta = self.generator._call_json(
            purpose=f"qa_rewrite_{payload.get('issue_type')}_round_{payload.get('round')}",
            system_prompt=qa_rewrite_system_prompt(),
            user_prompt=json.dumps(payload, ensure_ascii=False, indent=2),
            temperature=0.15,
            schema=qa_rewrite_schema(),
        )
        parsed = parse_json_object(output)
        return parsed if isinstance(parsed, dict) else None

    def sync_chinese(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        output, _meta = self.generator._call_json(
            purpose="qa_sync_chinese",
            system_prompt=qa_sync_chinese_system_prompt(),
            user_prompt=json.dumps(payload, ensure_ascii=False, indent=2),
            temperature=0.1,
            schema=qa_rewrite_schema(),
        )
        parsed = parse_json_object(output)
        return parsed if isinstance(parsed, dict) else None

    def translation_check(self, payload: dict[str, Any]) -> dict[str, Any]:
        output, _meta = self.generator._call_json(
            purpose="qa_translation_semantic_check",
            system_prompt=qa_translation_check_system_prompt(),
            user_prompt=json.dumps(payload, ensure_ascii=False, indent=2),
            temperature=0,
            schema=qa_translation_check_schema(),
        )
        parsed = parse_json_object(output)
        return parsed if isinstance(parsed, dict) else {"issues": []}


class FactIndex:
    def __init__(self, planning_context: Any) -> None:
        self.planning_context = planning_context
        self.allowed_facts = collect_allowed_facts(planning_context)
        self.raw_fact_text = json.dumps(self.allowed_facts, ensure_ascii=False)
        self.common_measurements: set[str] = set()
        self.all_measurements: set[str] = set()
        self.sku_measurements: dict[str, set[str]] = {}
        self.measurement_to_skus: dict[str, set[str]] = {}
        self._build_measurements()

    def _build_measurements(self) -> None:
        for fact in self.allowed_facts:
            scope = str(fact.get("scope") or "")
            value = str(fact.get("value") or "")
            if value:
                for measurement in extract_measurements(value):
                    self.all_measurements.add(measurement)
                    if scope == "ALL":
                        self.common_measurements.add(measurement)
            values_by_sku = fact.get("values_by_sku") or {}
            if not isinstance(values_by_sku, dict):
                continue
            unique_values = unique([str(value).strip() for value in values_by_sku.values() if str(value).strip()])
            common_across_skus = len(unique_values) == 1
            for sku_id, sku_value in values_by_sku.items():
                for measurement in extract_measurements(str(sku_value)):
                    self.all_measurements.add(measurement)
                    self.sku_measurements.setdefault(str(sku_id), set()).add(measurement)
                    self.measurement_to_skus.setdefault(measurement, set()).add(str(sku_id))
                    if common_across_skus:
                        self.common_measurements.add(measurement)

    def summary(self) -> dict[str, Any]:
        return {
            "fact_count": len(self.allowed_facts),
            "common_measurement_count": len(self.common_measurements),
            "all_measurement_count": len(self.all_measurements),
            "sku_count": len(self.sku_measurements),
        }


def run_qa(project: PlanningProject, progress: ProgressCallback | None = None) -> None:
    engine = QAEngine(project)
    project.qa_result = engine.run(progress=progress)


def collect_allowed_facts(planning_context: Any) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    try:
        facts.extend(build_allowed_product_facts(planning_context))
    except Exception:
        facts = []

    for key, value in (planning_context.product_common_parameters or {}).items():
        if str(value).strip():
            facts.append({"parameter": str(key), "scope": "ALL", "value": str(value).strip(), "source": "product_common_parameters"})

    for row in planning_context.standard_chinese_parameter_table or []:
        parameter = str(getattr(row, "parameter", "") or "").strip()
        values = [str(value).strip() for value in getattr(row, "values", []) if str(value).strip()]
        values_by_sku = getattr(row, "values_by_sku", None) or {}
        if values_by_sku:
            unique_values = unique(list(values_by_sku.values()))
            if len(unique_values) == 1:
                facts.append({"parameter": parameter, "scope": "ALL", "value": str(unique_values[0]), "source": "standard_chinese_parameter_table"})
            else:
                facts.append(
                    {
                        "parameter": parameter,
                        "scope": "SKU_SPECIFIC",
                        "values_by_sku": {str(key): str(value) for key, value in values_by_sku.items() if str(value).strip()},
                        "source": "standard_chinese_parameter_table",
                    }
                )
        elif values:
            unique_values = unique(values)
            facts.append({"parameter": parameter, "scope": "ALL", "value": " / ".join(unique_values), "source": "standard_chinese_parameter_table"})

    for parameter in planning_context.research_brief.core_product_parameters or []:
        if hasattr(parameter, "model_dump"):
            data = parameter.model_dump(mode="json")
        else:
            data = dict(parameter)
        facts.append(
            {
                "parameter": data.get("name", ""),
                "scope": "ALL" if data.get("scope") == "product_common" else "SKU_SPECIFIC",
                "value": data.get("value", ""),
                "values_by_sku": data.get("values_by_sku", {}),
                "source": "research_brief.core_product_parameters",
            }
        )
    return dedupe_facts(facts)


def dedupe_facts(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for fact in facts:
        key = json.dumps(fact, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        out.append(fact)
    return out


def load_forbidden_entries() -> list[ForbiddenEntry]:
    entries: list[ForbiddenEntry] = []
    for path in forbidden_csv_candidates():
        if path.exists():
            entries.extend(load_forbidden_csv(path))
            break
    for path in forbidden_backend_candidates():
        if path.exists():
            entries.extend(load_backend_forbidden_words(path))
            break
    if not entries:
        entries.extend(ForbiddenEntry(word=word, source="built_in_fallback") for word in BUILT_IN_FORBIDDEN_WORDS)
    return dedupe_forbidden(entries)


def forbidden_word_source_summary() -> dict[str, Any]:
    csv_paths = [str(path) for path in forbidden_csv_candidates() if path.exists()]
    backend_paths = [str(path) for path in forbidden_backend_candidates() if path.exists()]
    return {
        "csv": csv_paths[0] if csv_paths else "",
        "backend_text_formatter": backend_paths[0] if backend_paths else "",
        "fallback_used": not csv_paths and not backend_paths,
    }


def forbidden_csv_candidates() -> list[Path]:
    explicit = [os.getenv("SUPER_PLANNER_FORBIDDEN_WORDS_PATH", ""), os.getenv("QC_FORBIDDEN_WORDS_PATH", "")]
    paths = [Path(value) for value in explicit if value.strip()]
    for root in workbench_root_candidates():
        paths.append(root / "planning_qc" / "data" / "forbidden_words_english.csv")
    return unique_paths(paths)


def forbidden_backend_candidates() -> list[Path]:
    return unique_paths([root / "backend.py" for root in workbench_root_candidates()])


def workbench_root_candidates() -> list[Path]:
    candidates = []
    if os.getenv("SUPER_PLANNER_WORKBENCH_ROOT"):
        candidates.append(Path(os.getenv("SUPER_PLANNER_WORKBENCH_ROOT", "")))
    candidates.append(WORKBENCH_ROOT)
    candidates.append(Path("D:/\u667a\u80fd\u7ffb\u8bd1\u5de5\u4f5c\u53f01.2"))
    return unique_paths(candidates)


def unique_paths(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        raw = str(path)
        if not raw or raw in seen:
            continue
        seen.add(raw)
        result.append(path)
    return result


def load_forbidden_csv(path: Path) -> list[ForbiddenEntry]:
    entries = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            for row in reader:
                word = one_line(row.get("word", ""))
                if word:
                    entries.append(
                        ForbiddenEntry(
                            word=word,
                            meaning=one_line(row.get("meaning", "")),
                            solution=one_line(row.get("solution", "")),
                            source=one_line(row.get("source", "")),
                            note=one_line(row.get("note", "")),
                        )
                    )
    except Exception:
        return []
    return entries


def load_backend_forbidden_words(path: Path) -> list[ForbiddenEntry]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []
    words: list[str] = []
    for class_node in [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TextFormatter"]:
        for node in class_node.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(target, ast.Name) and target.id == "FORBIDDEN_WORDS" for target in node.targets):
                continue
            try:
                value = ast.literal_eval(node.value)
            except Exception:
                value = []
            if isinstance(value, list):
                words.extend(str(item).strip() for item in value if str(item).strip())
    return [ForbiddenEntry(word=word, source="workbench_backend_text_formatter") for word in words]


def dedupe_forbidden(entries: list[ForbiddenEntry]) -> list[ForbiddenEntry]:
    seen: set[str] = set()
    result: list[ForbiddenEntry] = []
    for entry in entries:
        key = entry.word.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(entry)
    return sorted(result, key=lambda item: len(item.word), reverse=True)


def compile_forbidden_pattern(word: str) -> re.Pattern[str]:
    escaped = re.escape(word.strip())
    if re.fullmatch(r"[A-Za-z]+", word.strip()):
        return re.compile(rf"(?i)(?<![A-Za-z]){escaped}(?![A-Za-z])")
    return re.compile(escaped, re.IGNORECASE)


def safe_forbidden_replacement(word: str) -> str:
    replacements = {
        "best": "practical",
        "perfect": "well-suited",
        "most": "many",
        "ultimate": "focused",
        "high quality": "well-built",
        "durable": "sturdy",
        "protection": "coverage",
        "protect": "help cover",
        "safe": "user-conscious",
        "safety": "careful use",
        "waterproof": "covered",
        "weatherproof": "outdoor-ready",
        "resistant": "built for",
        "anti-slip": "textured",
        "non-slip": "textured",
        "free-standing": "standalone",
        "free standing": "standalone",
        "uv": "sun exposure",
        "ultraviolet": "sun exposure",
        "guarantee": "support",
        "guarantees": "supports",
        "guaranteed": "designed",
        "warranty": "support terms",
        "warranties": "support terms",
        "free": "included",
        "remove": "clear",
        "removing": "clearing",
        "moisture": "damp conditions",
        "aesthetic": "visual",
        "enjoyable": "pleasant",
        "price": "details",
        "sale": "listing",
        "certified": "documented",
        "certification": "documentation",
    }
    return replacements.get(word.lower(), "")


def collect_english_text(item: dict[str, Any]) -> str:
    return "\n".join(segment["text"] for segment in iter_english_segments(item) if segment["text"]).strip()


def collect_chinese_text(item: dict[str, Any]) -> str:
    if isinstance(item.get("copy_parts"), dict):
        order = item.get("copy_part_order") or list(item["copy_parts"].keys())
        values = []
        for name in order:
            part = item["copy_parts"].get(name)
            if isinstance(part, dict) and str(part.get("chinese_copy") or "").strip():
                values.append(str(part.get("chinese_copy")).strip())
        return "\n".join(values).strip()
    return str(item.get("chinese_copy") or "").strip()


def iter_english_segments(item: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(item.get("copy_parts"), dict):
        segments = []
        for name, part in item["copy_parts"].items():
            if not isinstance(part, dict):
                continue
            segments.append(
                {
                    "field": f"copy_parts.{name}.english_copy",
                    "part": name,
                    "text": str(part.get("english_copy") or ""),
                    "length_rule_id": part.get("length_rule_id") or item.get("length_rule_id"),
                }
            )
        return segments
    return [
        {
            "field": "english_copy",
            "part": None,
            "text": str(item.get("english_copy") or ""),
            "length_rule_id": item.get("length_rule_id"),
        }
    ]


def set_segment_text(item: dict[str, Any], segment: dict[str, Any], *, english_copy: str | None = None, chinese_copy: str | None = None) -> None:
    part_name = segment.get("part")
    if part_name and isinstance(item.get("copy_parts"), dict) and part_name in item["copy_parts"]:
        if english_copy is not None:
            item["copy_parts"][part_name]["english_copy"] = english_copy
        if chinese_copy is not None:
            item["copy_parts"][part_name]["chinese_copy"] = chinese_copy
        return
    if english_copy is not None:
        item["english_copy"] = english_copy
    if chinese_copy is not None:
        item["chinese_copy"] = chinese_copy


def replace_text_everywhere(value: Any, replacements: dict[str, str]) -> None:
    if isinstance(value, dict):
        for key, nested in list(value.items()):
            if isinstance(nested, str):
                updated = nested
                for pattern, replacement in replacements.items():
                    updated = re.sub(pattern, replacement, updated)
                value[key] = clean_after_replacement(updated)
            else:
                replace_text_everywhere(nested, replacements)
    elif isinstance(value, list):
        for nested in value:
            replace_text_everywhere(nested, replacements)


def replace_measurement_text(text: str, measurement: str, replacement: str) -> str:
    normalized_target = normalize_measurement(measurement)

    def repl(match: re.Match[str]) -> str:
        if normalize_measurement(match.group(0)) == normalized_target:
            return replacement
        return match.group(0)

    return clean_after_replacement(MEASUREMENT_RE.sub(repl, text))


def clean_after_replacement(text: str) -> str:
    text = re.sub(r"\s{2,}", " ", str(text or ""))
    text = re.sub(r"\s+([,;:])", r"\1", text)
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"\s+\n", "\n", text)
    return text.strip()


MEASUREMENT_RE = re.compile(
    r"(?i)\b\d+(?:\.\d+)?(?:\s*[-\u2013]\s*\d+(?:\.\d+)?)?(?:\s*x\s*\d+(?:\.\d+)?(?:\s*x\s*\d+(?:\.\d+)?)?)?\s*(?:in|ft|mm|cm|m|kg|g|lbs?|oz|v|w|hz|%)\b"
)


def extract_measurements(text: str) -> list[str]:
    normalized = normalize_measurement_text(text)
    return unique([normalize_measurement(match.group(0)) for match in MEASUREMENT_RE.finditer(normalized)])


def normalize_measurement_text(text: str) -> str:
    text = str(text or "").replace("\u00d7", "x").replace("*", "x")
    text = re.sub(r"(?<=\d)\s*[xX]\s*(?=\d)", " x ", text)
    text = re.sub(r"(?<=\d)\s*-\s*(?=\d)", "-", text)
    text = re.sub(r"(\d)(inches|inch)\b", r"\1 in", text, flags=re.IGNORECASE)
    text = re.sub(r"(\d)(feet|foot)\b", r"\1 ft", text, flags=re.IGNORECASE)
    text = re.sub(r"(\d)(mm|cm|kg|lbs?|oz|ft|in|m|v|w|hz)\b", r"\1 \2", text, flags=re.IGNORECASE)
    return text


def normalize_measurement(value: str) -> str:
    text = normalize_measurement_text(value).lower()
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\blb\b", "lbs", text)
    text = re.sub(r"\blbs\b", "lbs", text)
    return text


MODEL_CODE_RE = re.compile(r"\b[A-Z]{2,}[A-Z0-9-]{3,}\b")


def extract_model_codes(text: str) -> list[str]:
    return [code for code in MODEL_CODE_RE.findall(str(text or "")) if re.search(r"\d", code)]


TRANSLATION_GLOSSARY: dict[str, list[str]] = {
    "fir wood": ["\u6749\u6728", "\u6728\u8d28", "\u6728\u6846"],
    "wood frame": ["\u6728", "\u6846"],
    "pc panel": ["PC", "\u677f"],
    "pc panels": ["PC", "\u677f"],
    "walk-in": ["\u53ef\u8fdb\u5165", "\u8fdb\u5165\u5f0f", "\u6b65\u5165"],
    "sloped roof": ["\u659c\u9876"],
    "shelves": ["\u68da", "\u67b6"],
    "hooks": ["\u6302\u94a9"],
    "slat hanging panels": ["\u677f\u6761", "\u6302\u677f"],
}


RANK_ALIGNMENT_TERMS: dict[int, list[str]] = {
    1: ["shelf", "shelves", "hook", "hooks", "slat", "storage", "organized", "organizing"],
    2: ["walk-in", "step-in", "space", "size", "sku", "room"],
    3: ["wood", "fir", "frame", "garden", "patio", "backyard"],
    4: ["pc", "polycarbonate", "panel", "panels", "covered"],
    5: ["sloped", "roof", "profile", "garden-house"],
}


def extract_selling_ranks(item: dict[str, Any]) -> list[int]:
    ranks = []
    for ref in item.get("selling_point_reference") or []:
        if not isinstance(ref, dict):
            continue
        try:
            ranks.append(int(ref.get("rank")))
        except (TypeError, ValueError):
            continue
    return ranks


def expected_primary_rank(item: dict[str, Any]) -> int | None:
    module = item.get("module")
    submodule = str(item.get("submodule") or "")
    sequence = int_or_none(item.get("sequence"))
    if module == "Bullet Point" and sequence in {1, 2, 3, 4, 5}:
        return sequence
    if module == "Supporting Image Copy":
        mapping = {"F2": 1, "F3": 2, "F4": 3, "F5": 4, "F6": 5, "F7": 2}
        return mapping.get(submodule)
    if module == "Premium A+":
        mapping = {2: 1, 3: 2, 4: 4, 5: 5}
        return mapping.get(sequence)
    return None


def int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def compact_strategy_context(strategy_result: Any) -> dict[str, Any]:
    return {
        "user_purchase_reason": strategy_result.user_purchase_reason,
        "communication_strategy": strategy_result.communication_strategy,
        "competitive_strategy": strategy_result.competitive_strategy,
        "selling_point_ranking": strategy_result.selling_point_ranking,
        "needs_verification": strategy_result.needs_verification,
    }


def compact_item_for_qa(item: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "id",
        "module",
        "submodule",
        "sequence",
        "sku_scope",
        "copy_shape",
        "primary_part",
        "length_rule_id",
        "english_copy",
        "chinese_copy",
        "copy_parts",
        "length_checks",
        "strategy_reference",
        "selling_point_reference",
        "parameter_reference",
        "evidence_reference",
        "confidence",
        "needs_verification",
    ]
    return {key: item.get(key) for key in keys if key in item}


def compact_item_for_translation_check(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "module": item.get("module"),
        "submodule": item.get("submodule"),
        "sku_scope": item.get("sku_scope"),
        "english_copy": collect_english_text(item),
        "chinese_copy": collect_chinese_text(item),
        "copy_parts": item.get("copy_parts") if isinstance(item.get("copy_parts"), dict) else None,
        "sku_copy_variants": item.get("sku_copy_variants") or [],
    }


def qa_rewrite_system_prompt() -> str:
    return """
You are QA Engine V0.1 for an Amazon US planning workflow.
Return only one valid JSON object.
You may only revise the provided ContentResult item to resolve the stated QA issue.
Preserve StrategyResult, SKU scope, selling point rank, verified product facts, parameter values, and evidence references.
Generate English first, then Chinese from final English with matching meaning.
Do not add certifications, warranties, absolute claims, performance promises, competitor comparisons, ASIN, BSR, sales, search volume, review ratio, market share, or price unless verified in the payload.
"""


def qa_sync_chinese_system_prompt() -> str:
    return """
You are a bilingual QA synchronizer.
Return only JSON for the same ContentResult item.
Do not rewrite strategy or English. Generate Chinese from the final English only.
Keep all numbers, units, SKU/model codes, materials, scope, and product facts identical.
"""


def qa_translation_check_system_prompt() -> str:
    return """
You are a bilingual QA checker for Amazon planning copy.
Return one JSON object with an "issues" array. Return an empty array when all items pass.
Check whether each English copy and Chinese copy express the same meaning.
Prioritize hard mismatches: numbers, units, SKU/model codes, materials, certifications, unsupported promises, reversed meaning, or missing core product facts.
Do not flag harmless word order, natural localization, or shorter Chinese phrasing when the same facts are preserved.
If Chinese should be corrected, provide suggested_chinese_copy for plain items or suggested_copy_parts for structured copy parts.
"""


def qa_rewrite_schema() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": "qa_rewrite_result",
        "schema": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "id": {"type": "string"},
                "english_copy": {"type": "string"},
                "chinese_copy": {"type": "string"},
                "copy_parts": {"type": "object", "additionalProperties": True},
                "notes": {"type": "string"},
            },
        },
        "strict": False,
    }


def qa_translation_check_schema() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": "qa_translation_check_result",
        "schema": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": True,
                        "properties": {
                            "id": {"type": "string"},
                            "judgement": {"type": "string"},
                            "risk_level": {"type": "string"},
                            "problematic_text": {"type": "string"},
                            "issue_description": {"type": "string"},
                            "suggested_action": {"type": "string"},
                            "suggested_chinese_copy": {"type": "string"},
                            "suggested_copy_parts": {"type": "object", "additionalProperties": True},
                        },
                    },
                }
            },
            "required": ["issues"],
        },
        "strict": False,
    }


def build_issue(
    item: dict[str, Any],
    *,
    issue_type: str,
    risk_level: str,
    problematic_text: str,
    issue_description: str,
    suggested_action: str,
    category: str,
    status: str,
    rewrite_attempts: list[dict[str, Any]],
) -> dict[str, Any]:
    english = collect_english_text(item)
    chinese = collect_chinese_text(item)
    span = find_problem_span(english, problematic_text, field="english_copy")
    if span is None:
        span = find_problem_span(chinese, problematic_text, field="chinese_copy")
    if span is None and problematic_text:
        span = {"field": "english_copy", "text": problematic_text, "start": -1, "end": -1}
    return {
        "sku_scope": item.get("sku_scope", "ALL"),
        "content_item_id": item.get("id", ""),
        "module": item.get("module", ""),
        "submodule": item.get("submodule", ""),
        "sequence": item.get("sequence"),
        "issue_category": category,
        "issue_type": issue_type,
        "risk_level": normalize_risk(risk_level),
        "problematic_text": problematic_text,
        "problematic_span": span,
        "red_mark_spans": [span] if span else [],
        "english_copy": english,
        "chinese_copy": chinese,
        "issue_description": issue_description,
        "rewrite_attempts": rewrite_attempts,
        "suggested_action": suggested_action,
        "status": status,
    }


def build_variant_issue(
    item: dict[str, Any],
    variant: dict[str, Any],
    *,
    issue_type: str,
    risk_level: str,
    problematic_text: str,
    issue_description: str,
    suggested_action: str,
    category: str = "parameter_issues",
    status: str,
) -> dict[str, Any]:
    english = str(variant.get("english_copy") or "")
    chinese = str(variant.get("chinese_copy") or "")
    span = find_problem_span(english, problematic_text, field="sku_copy_variants.english_copy")
    if span is None:
        span = find_problem_span(chinese, problematic_text, field="sku_copy_variants.chinese_copy")
    if span is None and problematic_text:
        span = {"field": "sku_copy_variants.english_copy", "text": problematic_text, "start": -1, "end": -1}
    return {
        "sku_scope": "SKU_SPECIFIC",
        "sku_id": variant.get("sku_id", ""),
        "content_item_id": item.get("id", ""),
        "module": item.get("module", ""),
        "submodule": item.get("submodule", ""),
        "sequence": item.get("sequence"),
        "issue_category": category,
        "issue_type": issue_type,
        "risk_level": normalize_risk(risk_level),
        "problematic_text": problematic_text,
        "problematic_span": span,
        "red_mark_spans": [span] if span else [],
        "english_copy": english,
        "chinese_copy": chinese,
        "issue_description": issue_description,
        "rewrite_attempts": [],
        "suggested_action": suggested_action,
        "status": status,
    }


def find_problem_span(text: str, problematic_text: str, *, field: str) -> dict[str, Any] | None:
    if not text or not problematic_text:
        return None
    lower = text.lower()
    for candidate in [problematic_text, *[part.strip() for part in problematic_text.split(",") if part.strip()]]:
        if not candidate:
            continue
        index = lower.find(candidate.lower())
        if index >= 0:
            return {"field": field, "text": text[index : index + len(candidate)], "start": index, "end": index + len(candidate)}
    return None


def same_numbers_and_units(english: str, chinese: str) -> bool:
    return sorted(set(extract_measurements(english))) == sorted(set(extract_measurements(chinese)))


def find_item(items: list[dict[str, Any]], item_id: str) -> dict[str, Any] | None:
    for item in items:
        if str(item.get("id") or "") == item_id:
            return item
    return None


def normalize_risk(value: Any, default: str = "medium") -> str:
    text = str(value or default).strip().lower()
    return text if text in {"high", "medium", "low"} else default


def max_risk(values: list[str]) -> str:
    order = {"high": 3, "medium": 2, "low": 1}
    if not values:
        return "low"
    return max((normalize_risk(value, "low") for value in values), key=lambda item: order[item])


def derive_overall_risk(unresolved: list[dict[str, Any]], state: QAState) -> str:
    if any(issue.get("risk_level") == "high" for issue in unresolved):
        return "high"
    if any(issue.get("risk_level") == "medium" for issue in unresolved):
        return "medium"
    if state.forbidden_word_issues or state.parameter_issues or state.translation_issues or state.format_issues or state.strategy_issues:
        return "low"
    return "low"


def dedupe_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for issue in issues:
        key = json.dumps(
            {
                "item": issue.get("content_item_id"),
                "sku": issue.get("sku_id"),
                "type": issue.get("issue_type"),
                "text": issue.get("problematic_text"),
                "status": issue.get("status"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(issue)
    return output


def json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def one_line(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def snippet(text: str, start: int, end: int, width: int = 80) -> str:
    left = max(0, start - width)
    right = min(len(text), end + width)
    return text[left:right].replace("\n", " ")


def normalize_repetition_text(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text or "").lower()).strip()
    text = re.sub(r"\b(the|a|an|and|with|for|to|in|of)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_case_like(text: str) -> str:
    words = str(text or "").split(" ")
    small = {"a", "an", "the", "and", "but", "or", "for", "nor", "on", "at", "to", "from", "by", "with", "in", "of", "as"}
    result = []
    for index, word in enumerate(words):
        if not word:
            continue
        clean = re.sub(r"[^A-Za-z0-9+]", "", word)
        if index > 0 and clean.lower() in small:
            result.append(word.lower())
        elif clean in {"PC", "SKU", "A+", "US"}:
            result.append(clean)
        else:
            result.append(word[:1].upper() + word[1:])
    return " ".join(result)


def find_brand_name(planning_context: Any) -> str:
    for key, value in (planning_context.product_common_parameters or {}).items():
        if re.search(r"brand|\u54c1\u724c", str(key), re.IGNORECASE) and str(value).strip():
            return str(value).strip()
    for row in planning_context.standard_chinese_parameter_table or []:
        name = str(getattr(row, "parameter", "") or "")
        if not re.search(r"brand|\u54c1\u724c", name, re.IGNORECASE):
            continue
        values = unique([str(value).strip() for value in getattr(row, "values", []) if str(value).strip()])
        if len(values) == 1:
            return values[0]
    return DEFAULT_BRAND_NAME


def sanitize_error(exc: Exception) -> str:
    text = str(exc)
    text = re.sub(r"sk-[A-Za-z0-9_\-]+", "sk-***", text)
    text = re.sub(r"Bearer\s+[A-Za-z0-9_\-.]+", "Bearer ***", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()[:500]


BUILT_IN_FORBIDDEN_WORDS = [
    "best",
    "perfect",
    "guarantee",
    "guaranteed",
    "warranty",
    "waterproof",
    "weatherproof",
    "safe",
    "100% quality guaranteed",
    "free",
    "anti",
    "anti-mold",
    "mold",
    "pest",
    "uv",
    "ultraviolet",
    "certified",
    "certification",
]
