# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any, Iterable

from super_planner.core.exceptions import WorkflowBusinessInterruption, WorkflowSystemError
from super_planner.schemas.project import PlanningProject, ResearchCoreParameter, StrategyResult
from super_planner.schemas.workflow import ConfirmationQuestion, ConfirmationRequest
from super_planner.services.research_providers.base import ProgressCallback, emit_progress


STRATEGY_VERSION = "strategy-4.0-v0.1"
SOURCE_PRIORITY = ("sorftime_data", "web_evidence", "spec_fact", "user_input", "model_inference", "needs_verification")


def run_strategy(project: PlanningProject, progress: ProgressCallback | None = None) -> None:
    engine = StrategyEngine(project)
    if engine.research_status != "completed":
        raise WorkflowSystemError("Strategy requires a completed ResearchResult. Please contact IT.")

    critical_gaps = engine.critical_gaps()
    if critical_gaps:
        project.strategy_result = StrategyResult(
            status="waiting_confirmation",
            summary="策略策划缺少关键方向信息，等待用户补充后继续。",
            strategy_version=STRATEGY_VERSION,
            generated_at=datetime.utcnow(),
            needs_verification=[
                {
                    "item": gap,
                    "source_type": "needs_verification",
                    "impact": "该信息缺失会影响核心策略方向判断。",
                    "confidence": 1.0,
                }
                for gap in critical_gaps
            ],
            raw_payload={"strategy_version": STRATEGY_VERSION, "critical_gaps": critical_gaps},
        )
        raise WorkflowBusinessInterruption(engine.confirmation_request(critical_gaps))

    project.strategy_result = engine.build(progress=progress)


class StrategyEngine:
    def __init__(self, project: PlanningProject) -> None:
        self.project = project
        self.planning_context = project.planning_context
        self.research_result = project.research_result
        self.research_status = (project.research_result.status or "").strip()
        self.brief = self.planning_context.research_brief
        self.keyword = (
            self.brief.english_core_keyword
            or self.brief.raw_keywords
            or project.user_input.keywords
            or ""
        ).strip()
        self.overview = (
            str(self.planning_context.user_input.get("overview") or "").strip()
            or str(self.planning_context.facts.get("overview") or "").strip()
            or project.user_input.overview.strip()
        )
        self.parameters = list(self.brief.core_product_parameters or [])
        research_evidence = [normalize_evidence(item) for item in (project.research_result.evidence or [])]
        evidence_by_id = {str(item.get("id")): item for item in research_evidence if item.get("id")}
        planning_evidence = build_planning_strategy_evidence(project, evidence_by_id)
        self.evidence = research_evidence + planning_evidence
        self.evidence_by_id = {str(item.get("id")): item for item in self.evidence if item.get("id")}
        self.human_strategy_note = str(
            (self.planning_context.human_notes or {}).get("strategy_core_direction_confirmation") or ""
        ).strip()

    def critical_gaps(self) -> list[str]:
        gaps: list[str] = []
        category = text_of((self.research_result.market_context or {}).get("category_hypothesis"))
        if not self.keyword and not category and not self.human_strategy_note:
            gaps.append("缺少可用于判断产品类目/策略类目的英文核心关键词或市场类目。")

        capability_count = len([item for item in self.capability_candidates() if item.get("supported")])
        if capability_count < 3 and not self.human_strategy_note:
            gaps.append("缺少足够的真实产品能力，无法形成可靠卖点排序。")

        return gaps

    def confirmation_request(self, gaps: list[str]) -> ConfirmationRequest:
        background = {
            "known_keyword": self.keyword,
            "known_category": text_of((self.research_result.market_context or {}).get("category_hypothesis")),
            "known_product_parameters": [serialize_parameter(item) for item in self.parameters[:8]],
            "research_missing_data": (self.research_result.data_quality or {}).get("missing_data", []),
            "critical_gaps": gaps,
        }
        return ConfirmationRequest(
            id=uuid.uuid4().hex,
            step_id="strategy",
            title="策略策划需要补充关键方向",
            reason="缺少足够信息判断产品类目、核心用户或真实卖点，需用户补充后继续。",
            questions=[
                ConfirmationQuestion(
                    id="strategy_core_direction_confirmation",
                    prompt="请补充该产品最重要的真实使用场景、目标用户、或必须强调的真实产品能力。",
                    background=json.dumps(background, ensure_ascii=False, indent=2),
                )
            ],
        )

    def build(self, progress: ProgressCallback | None = None) -> StrategyResult:
        emit_progress(progress, "正在分析用户核心需求")
        capabilities = self.capability_candidates()
        needs = self.build_user_needs()
        focus_points = self.build_focus_points(capabilities)
        pain_points = self.build_pain_points()
        purchase_barriers = self.build_purchase_barriers()

        emit_progress(progress, "正在分析竞争格局")
        product_category = self.build_product_category()
        strategy_category = self.build_strategy_category(product_category)
        target_audience = self.build_target_audience()
        main_uses = self.build_main_uses()
        main_scenario = self.build_main_scenario(target_audience)
        secondary_scenario = self.build_secondary_scenarios()
        user_mindset = self.build_user_mindset(target_audience, needs, focus_points, purchase_barriers)
        competitive_landscape = self.build_competitive_landscape()

        emit_progress(progress, "正在识别竞争机会")
        competitive_opportunity = self.build_competitive_opportunity(capabilities)
        needs_verification = self.build_needs_verification(capabilities)

        emit_progress(progress, "正在形成用户购买理由")
        user_purchase_reason = self.build_user_purchase_reason(
            capabilities=capabilities,
            needs=needs,
            competitive_opportunity=competitive_opportunity,
        )
        competitive_strategy = self.build_competitive_strategy(
            product_category=product_category,
            user_purchase_reason=user_purchase_reason,
            competitive_opportunity=competitive_opportunity,
        )

        emit_progress(progress, "正在进行卖点排序")
        selling_point_ranking = self.build_selling_point_ranking(capabilities, needs)

        emit_progress(progress, "正在整理整体沟通策略")
        communication_strategy = self.build_communication_strategy(
            user_purchase_reason=user_purchase_reason,
            selling_point_ranking=selling_point_ranking,
            purchase_barriers=purchase_barriers,
        )
        confidence = self.build_confidence(selling_point_ranking, needs_verification)

        return StrategyResult(
            status="completed",
            summary=f"策略策划完成：已形成 {self.keyword or '当前产品'} 的用户购买理由、竞争策略、沟通主线与 TOP1-TOP5 卖点排序。",
            strategy_version=STRATEGY_VERSION,
            generated_at=datetime.utcnow(),
            product_category=product_category,
            strategy_category=strategy_category,
            user_mindset=user_mindset,
            target_audience=target_audience,
            main_uses=main_uses,
            main_scenario=main_scenario,
            secondary_scenario=secondary_scenario,
            user_needs_summary=needs,
            focus_points=focus_points,
            pain_points=pain_points,
            purchase_barriers=purchase_barriers,
            competitive_landscape=competitive_landscape,
            competitive_opportunity=competitive_opportunity,
            competitive_strategy=competitive_strategy,
            user_purchase_reason=user_purchase_reason,
            communication_strategy=communication_strategy,
            selling_point_ranking=selling_point_ranking,
            evidence=self.evidence,
            confidence=confidence,
            needs_verification=needs_verification,
            pillars=[
                "用户需求",
                "竞争机会",
                "用户购买理由",
                "沟通主线",
                "卖点排序",
            ],
            raw_payload={
                "strategy_version": STRATEGY_VERSION,
                "methodology": {
                    "chain": [
                        "用户需求",
                        "用户关注点",
                        "竞争情况",
                        "产品事实",
                        "竞争机会",
                        "用户购买理由",
                        "卖点排序",
                        "整体沟通策略",
                    ],
                    "reference_frameworks": [
                        "JTBD",
                        "buying trigger",
                        "evaluation criteria",
                        "trust barrier",
                        "proof requirement",
                        "conversion driver",
                        "competitor mindset",
                    ],
                },
                "case_knowledge": {"status": "reserved", "used": False},
                "input_snapshot": self.input_snapshot(),
            },
        )

    def build_product_category(self) -> dict[str, Any]:
        category = text_of((self.research_result.market_context or {}).get("category_hypothesis"))
        category = category or f"{self.keyword} / Amazon US"
        return judgement(
            value=category,
            source_type=source_type_of((self.research_result.market_context or {}).get("category_hypothesis")) or "model_inference",
            evidence_ids=evidence_ids_of((self.research_result.market_context or {}).get("category_hypothesis")) or ["user-input-keyword"],
            confidence=confidence_of((self.research_result.market_context or {}).get("category_hypothesis"), default=0.72),
        )

    def build_strategy_category(self, product_category: dict[str, Any]) -> dict[str, Any]:
        tier = text_of(self.research_result.market_tier)
        if "中高" in tier or "mid" in tier.lower():
            category = "中高配后院园艺温室策略"
        elif "高" in tier or "premium" in tier.lower():
            category = "高关注度户外园艺装备策略"
        else:
            category = "Amazon US 家庭园艺温室策略"
        return judgement(
            value=category,
            source_type="model_inference",
            evidence_ids=unique(product_category.get("evidence_ids", []) + evidence_ids_of(self.research_result.market_tier)),
            confidence=min(0.86, average_confidence([product_category, self.research_result.market_tier], default=0.74)),
        )

    def build_target_audience(self) -> list[dict[str, Any]]:
        audience_items = list_dicts((self.research_result.audience_demand_context or {}).get("audiences"))
        if not audience_items:
            audience_items = [
                {
                    "value": "Amazon US 后院园艺用户",
                    "source_type": "model_inference",
                    "evidence_ids": ["user-input-keyword", "user-input-overview"],
                    "confidence": 0.6,
                }
            ]
        result = []
        for index, item in enumerate(audience_items[:4], start=1):
            value = text_of(item)
            result.append(
                {
                    "segment": value,
                    "priority": index,
                    "role": infer_audience_role(value),
                    "why_relevant": "该人群的购买判断与种植空间、庭院使用、收纳组织或外观协调直接相关。",
                    "source_type": source_type_of(item) or "model_inference",
                    "evidence_ids": evidence_ids_of(item),
                    "confidence": confidence_of(item, default=0.7),
                }
            )
        return result

    def build_main_uses(self) -> list[dict[str, Any]]:
        jobs = list_dicts((self.research_result.audience_demand_context or {}).get("jobs_to_be_done"))
        uses = []
        for item in jobs[:4]:
            uses.append(
                judgement(
                    value=text_of(item),
                    source_type=source_type_of(item) or "model_inference",
                    evidence_ids=evidence_ids_of(item),
                    confidence=confidence_of(item, default=0.68),
                )
            )
        if not uses:
            uses = [
                judgement(
                    "在后院或庭院中完成植物摆放、种植保护与日常照料。",
                    "model_inference",
                    ["user-input-overview"],
                    0.64,
                )
            ]
        return uses

    def build_main_scenario(self, target_audience: list[dict[str, Any]]) -> dict[str, Any]:
        evidence_ids = ["user-input-overview", "user-input-keyword"]
        for item in target_audience[:2]:
            evidence_ids.extend(item.get("evidence_ids", []))
        return {
            "scenario": "后院 / 庭院中的可进入式园艺温室空间",
            "usage_context": "用户需要一个能放置植物、进入打理、同时收纳工具和花盆的户外空间。",
            "why_primary": "该场景同时承接 Research 中的 audience、JTBD、walk-in 体量、收纳组织和庭院外观信号。",
            "source_type": "model_inference",
            "evidence_ids": valid_evidence_ids(self.evidence_by_id, evidence_ids),
            "confidence": 0.82,
        }

    def build_secondary_scenarios(self) -> list[dict[str, Any]]:
        candidates = [
            ("季节性植物保护 / 育苗延展", ["user-input-overview"]),
            ("园艺工具、盆栽和小型用品集中收纳", ["user-input-overview", "spec-fact-3"]),
            ("庭院景观化摆放与日常观赏", ["user-input-overview", "spec-fact-2"]),
        ]
        return [
            judgement(value=value, source_type="model_inference", evidence_ids=ids, confidence=0.68 + index * 0.04)
            for index, (value, ids) in enumerate(candidates)
        ]

    def build_user_mindset(
        self,
        target_audience: list[dict[str, Any]],
        needs: list[dict[str, Any]],
        focus_points: list[dict[str, Any]],
        purchase_barriers: list[dict[str, Any]],
    ) -> dict[str, Any]:
        primary_audience = target_audience[0]["segment"] if target_audience else "目标用户"
        primary_need = needs[0]["need"] if needs else "稳定完成核心使用任务"
        focus = "、".join([item.get("point", "") for item in focus_points[:3] if item.get("point")])
        barrier = purchase_barriers[0].get("barrier", "") if purchase_barriers else "是否足够可信"
        return {
            "summary": judgement(
                value=f"{primary_audience}不是只在买一个温室外壳，而是在判断它能否长期承担{primary_need}，并且是否值得放进可见的后院空间。",
                source_type="model_inference",
                evidence_ids=unique(["user-input-overview"] + collect_ids(target_audience[:2]) + collect_ids(needs[:2])),
                confidence=0.8,
            ),
            "decision_chain": {
                "buying_trigger": first_value((self.research_result.audience_demand_context or {}).get("buying_triggers"))
                or "需要更稳定、更好整理的后院种植空间。",
                "evaluation_criteria": focus or "空间、材质、结构、收纳、通风、安装可信度",
                "trust_barrier": barrier,
                "proof_requirement": first_value((self.research_result.audience_demand_context or {}).get("proof_requirements"))
                or "需要清楚证明尺寸、材质、安装、耐候和真实可用空间。",
                "conversion_driver": "把植物保护、日常打理、工具收纳和庭院观感放在同一条决策逻辑里解释。",
            },
        }

    def build_user_needs(self) -> list[dict[str, Any]]:
        items = []
        jobs = list_dicts((self.research_result.audience_demand_context or {}).get("jobs_to_be_done"))
        triggers = list_dicts((self.research_result.audience_demand_context or {}).get("buying_triggers"))
        voc = list_dicts((self.research_result.voc_context or {}).get("review_based_findings"))
        voc.extend(list_dicts((self.research_result.voc_context or {}).get("inferred_needs_or_concerns")))

        for item in jobs + triggers + voc:
            text = text_of(item)
            if not text:
                continue
            items.append(
                {
                    "need": normalize_need_text(text),
                    "why_it_matters": infer_need_impact(text),
                    "source_type": source_type_of(item) or "model_inference",
                    "evidence_ids": evidence_ids_of(item),
                    "confidence": confidence_of(item, default=0.66),
                }
            )

        return dedupe_by_text(items, "need")[:8]

    def build_focus_points(self, capabilities: list[dict[str, Any]]) -> list[dict[str, Any]]:
        criteria = list_dicts((self.research_result.audience_demand_context or {}).get("evaluation_criteria"))
        focus_points = []
        for item in criteria[:8]:
            focus_points.append(
                {
                    "point": text_of(item),
                    "buyer_question": buyer_question_for(text_of(item)),
                    "source_type": source_type_of(item) or "model_inference",
                    "evidence_ids": evidence_ids_of(item),
                    "confidence": confidence_of(item, default=0.66),
                }
            )
        if focus_points:
            return focus_points

        for capability in capabilities[:6]:
            focus_points.append(
                {
                    "point": capability["label"],
                    "buyer_question": buyer_question_for(capability["label"]),
                    "source_type": capability["product_source_type"],
                    "evidence_ids": capability["evidence_ids"],
                    "confidence": capability["confidence"],
                }
            )
        return focus_points

    def build_pain_points(self) -> list[dict[str, Any]]:
        points = []
        for item in list_dicts((self.research_result.voc_context or {}).get("review_based_findings")):
            points.append(
                {
                    "pain": text_of(item),
                    "evidence_status": "review_based",
                    "source_type": source_type_of(item) or "web_evidence",
                    "evidence_ids": evidence_ids_of(item),
                    "confidence": confidence_of(item, default=0.76),
                }
            )
        for item in list_dicts((self.research_result.voc_context or {}).get("inferred_needs_or_concerns")):
            points.append(
                {
                    "pain": text_of(item),
                    "evidence_status": "inference_not_review_claim",
                    "source_type": "model_inference" if source_type_of(item) != "spec_fact" else "spec_fact",
                    "evidence_ids": evidence_ids_of(item),
                    "confidence": confidence_of(item, default=0.62),
                }
            )
        return dedupe_by_text(points, "pain")[:8]

    def build_purchase_barriers(self) -> list[dict[str, Any]]:
        barriers = []
        source_items = list_dicts((self.research_result.audience_demand_context or {}).get("trust_barriers"))
        source_items.extend(list_dicts((self.research_result.voc_context or {}).get("limitations")))
        for item in source_items:
            barriers.append(
                {
                    "barrier": text_of(item),
                    "proof_needed": proof_needed_for(text_of(item)),
                    "source_type": source_type_of(item) or "model_inference",
                    "evidence_ids": evidence_ids_of(item),
                    "confidence": confidence_of(item, default=0.66),
                }
            )
        if self.research_result.data_quality:
            for item in list_dicts(self.research_result.data_quality.get("missing_data"))[:4]:
                barriers.append(
                    {
                        "barrier": text_of(item) or str(item.get("item") or ""),
                        "proof_needed": "后续接入 Sorftime 或人工确认后补齐。",
                        "source_type": "needs_verification",
                        "evidence_ids": evidence_ids_of(item),
                        "confidence": 0.9,
                    }
                )
        return dedupe_by_text([item for item in barriers if item.get("barrier")], "barrier")[:9]

    def build_competitive_landscape(self) -> dict[str, Any]:
        context = self.research_result.competitor_context or {}
        pool = list_dicts(context.get("candidate_pool"))
        representatives = list_dicts(context.get("representative_competitors"))
        selection_logic = list_dicts(context.get("selection_logic"))
        verified_competitors = [
            item for item in pool + representatives if item.get("asin") or source_type_of(item) in {"web_evidence", "sorftime_data"}
        ]
        evidence_status = "verified_competitors_available" if verified_competitors else "competitor_types_only_needs_verification"
        return {
            "market_tier": self.research_result.market_tier,
            "candidate_pool": pool,
            "representative_competitors": representatives,
            "selection_logic": selection_logic,
            "evidence_status": evidence_status,
            "interpretation": judgement(
                value=(
                    "当前竞争判断应围绕 wooden/cedar walk-in greenhouse、polycarbonate greenhouse、带收纳温室等产品类型展开；"
                    "由于缺少稳定 Amazon ASIN 级数据，不能把候选产品当作已验证竞品。"
                ),
                source_type="model_inference",
                evidence_ids=unique(collect_ids(pool[:3]) + collect_ids(representatives[:3]) + ["user-input-keyword"]),
                confidence=0.72 if verified_competitors else 0.55,
            ),
        }

    def build_competitive_opportunity(self, capabilities: list[dict[str, Any]]) -> list[dict[str, Any]]:
        opportunities = []
        for item in list_dicts(self.research_result.opportunity_signals)[:8]:
            opportunities.append(
                {
                    "opportunity": text_of(item),
                    "strategic_implication": strategic_implication_for(text_of(item)),
                    "source_type": source_type_of(item) or "model_inference",
                    "evidence_ids": evidence_ids_of(item),
                    "confidence": confidence_of(item, default=0.66),
                }
            )

        storage = first_supported(capabilities, "storage")
        if storage:
            opportunities.insert(
                0,
                {
                    "opportunity": "从单纯植物遮护升级为“可整理、可进入、可日常打理的 garden workspace”。",
                    "strategic_implication": "后续内容应优先证明它解决种植和收纳同时存在的任务，而不是只罗列温室参数。",
                    "source_type": "model_inference",
                    "evidence_ids": storage["evidence_ids"],
                    "confidence": min(0.88, storage["confidence"]),
                },
            )
        return dedupe_by_text(opportunities, "opportunity")[:8]

    def build_competitive_strategy(
        self,
        product_category: dict[str, Any],
        user_purchase_reason: dict[str, Any],
        competitive_opportunity: list[dict[str, Any]],
    ) -> dict[str, Any]:
        ids = unique(
            product_category.get("evidence_ids", [])
            + user_purchase_reason.get("evidence_ids", [])
            + collect_ids(competitive_opportunity[:3])
        )
        return {
            "strategy": "不要把本品当作低价基础温室去打参数堆叠；应把它解释为兼顾庭院美感、进入式使用和收纳组织的中高配园艺空间。",
            "what_to_win": [
                "赢下希望 greenhouse 同时承担种植、收纳和庭院观感的用户。",
                "赢下对 mini / PE cover 临时棚不满意、但又不只看极高端 glass greenhouse 的用户。",
                "用清晰尺寸、材质、安装和耐候证据降低高客单大件的决策阻力。",
            ],
            "what_to_avoid": [
                "避免使用未经验证的销量、排名、评论比例或市场份额作为策略依据。",
                "避免把 VOC 假设写成真实评论结论。",
                "避免在缺少竞品 ASIN 验证时做精确竞品压制话术。",
            ],
            "source_type": "model_inference",
            "evidence_ids": valid_evidence_ids(self.evidence_by_id, ids),
            "confidence": min(0.82, average_confidence([product_category, user_purchase_reason] + competitive_opportunity[:3], 0.7)),
        }

    def build_user_purchase_reason(
        self,
        capabilities: list[dict[str, Any]],
        needs: list[dict[str, Any]],
        competitive_opportunity: list[dict[str, Any]],
    ) -> dict[str, Any]:
        top_caps = [item for item in capabilities if item.get("supported")][:4]
        cap_labels = [item["label"] for item in top_caps]
        need_text = strip_terminal_punctuation(needs[0]["need"] if needs else "获得稳定、好打理的后院种植空间")
        reason = (
            "用户应该选择当前产品的核心理由是：它不是只提供一个温室遮护壳，而是把"
            f"{join_cn(cap_labels[:3])}组合成一个可进入、可整理、适合日常照料的后院园艺空间，"
            f"更贴近用户想完成的任务：{need_text}。"
        )
        if any(item.get("source_type") == "needs_verification" for item in competitive_opportunity):
            reason += " 但竞品差异仍需后续 ASIN 和评论数据验证。"
        evidence_ids = unique(collect_ids(top_caps) + collect_ids(needs[:2]) + collect_ids(competitive_opportunity[:2]))
        return {
            "core_reason": reason,
            "logic_chain": [
                {"step": "用户强需求", "value": need_text},
                {"step": "核心场景", "value": "后院 / 庭院中的可进入式园艺温室空间"},
                {"step": "产品真实能力", "value": join_cn(cap_labels[:4])},
                {
                    "step": "竞品/市场机会",
                    "value": first_value(competitive_opportunity) or "以可整理的中高配 garden workspace 形成差异机会。",
                },
            ],
            "source_type": "model_inference",
            "evidence_ids": valid_evidence_ids(self.evidence_by_id, evidence_ids),
            "confidence": round(
                min(0.84, average_confidence(top_caps + needs[:2] + competitive_opportunity[:2], default=0.7)),
                2,
            ),
        }

    def build_communication_strategy(
        self,
        user_purchase_reason: dict[str, Any],
        selling_point_ranking: list[dict[str, Any]],
        purchase_barriers: list[dict[str, Any]],
    ) -> dict[str, Any]:
        ids = unique(user_purchase_reason.get("evidence_ids", []) + collect_ids(selling_point_ranking[:3]))
        return {
            "main_line": "围绕“可进入、可整理、能融入庭院的木质 garden workspace”沟通，而不是从单个参数直接讲卖点。",
            "message_hierarchy": [
                {
                    "layer": "先讲任务",
                    "content": "用户需要一个能种植、收纳、日常打理的后院空间。",
                },
                {
                    "layer": "再讲产品能力",
                    "content": "用 walk-in 空间、木质框架、PC panels、shelves/hooks/slat panels、ventilation 等真实能力承接任务。",
                },
                {
                    "layer": "最后讲可信证明",
                    "content": "后续文案和图片应补足尺寸口径、安装、耐候、可用空间、收纳布局等证据。",
                },
            ],
            "proof_requirements": [
                item.get("proof_needed")
                for item in purchase_barriers[:5]
                if item.get("proof_needed")
            ],
            "content_boundaries": [
                "本阶段不生成标题、五点、A+、图片文案或最终 Excel。",
                "后续内容不得使用未经验证的销量、BSR、搜索量、评论比例、市场份额。",
            ],
            "source_type": "model_inference",
            "evidence_ids": valid_evidence_ids(self.evidence_by_id, ids),
            "confidence": min(0.83, average_confidence([user_purchase_reason] + selling_point_ranking[:3], default=0.72)),
        }

    def build_selling_point_ranking(self, capabilities: list[dict[str, Any]], needs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates = [score_capability(item, self.research_result, needs) for item in capabilities if item.get("supported")]
        candidates.sort(key=lambda item: item["score"], reverse=True)
        ranking = []
        for rank, item in enumerate(candidates[:5], start=1):
            ranking.append(
                {
                    "rank": rank,
                    "selling_point": item["selling_point"],
                    "corresponding_user_need": item["user_need"],
                    "corresponding_product_fact": item["product_fact"],
                    "corresponding_market_competitor_basis": item["market_basis"],
                    "user_perceived_value": item["perceived_value"],
                    "reason_for_ranking": item["reason_for_ranking"],
                    "ranking_factors": item["ranking_factors"],
                    "source_type": "model_inference",
                    "product_fact_source_type": item["product_source_type"],
                    "evidence_ids": valid_evidence_ids(self.evidence_by_id, item["evidence_ids"]),
                    "confidence": item["confidence"],
                    "needs_verification": item["needs_verification"],
                }
            )

        while len(ranking) < 5:
            rank = len(ranking) + 1
            ranking.append(
                {
                    "rank": rank,
                    "selling_point": "待补充/待验证卖点",
                    "corresponding_user_need": "当前 Planning Context 与 ResearchResult 中缺少足够证据。",
                    "corresponding_product_fact": "无可安全引用的新增产品事实。",
                    "corresponding_market_competitor_basis": "不可为了凑满卖点而编造竞品或用户数据。",
                    "user_perceived_value": "待验证",
                    "reason_for_ranking": "证据不足，保留占位供后续补充真实资料或 Sorftime 数据后判断。",
                    "ranking_factors": {
                        "user_need_strength": 0.0,
                        "perceived_value": 0.0,
                        "product_advantage_strength": 0.0,
                        "competitor_differentiation": 0.0,
                        "evidence_reliability": 0.0,
                        "communication_value": 0.0,
                    },
                    "source_type": "needs_verification",
                    "product_fact_source_type": "needs_verification",
                    "evidence_ids": [],
                    "confidence": 0.0,
                    "needs_verification": ["缺少可支撑该卖点的产品事实或市场依据。"],
                }
            )
        return ranking

    def build_needs_verification(self, capabilities: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items = []
        for item in list_dicts((self.research_result.data_quality or {}).get("missing_data")):
            value = text_of(item) or str(item.get("item") or "")
            if not value:
                continue
            items.append(
                {
                    "item": value,
                    "impact": item.get("impact") or "会降低策略结论的市场验证强度。",
                    "source_type": "needs_verification",
                    "evidence_ids": evidence_ids_of(item),
                    "confidence": confidence_of(item, default=0.9),
                }
            )

        capability_map = {item["key"]: item for item in capabilities}
        if capability_map.get("wood_frame") and capability_map["wood_frame"].get("supported"):
            items.append(
                {
                    "item": "木质框架长期户外耐候、防腐、防开裂证据。",
                    "impact": "影响木质外观与中高配路线的可信度。",
                    "source_type": "needs_verification",
                    "evidence_ids": capability_map["wood_frame"]["evidence_ids"],
                    "confidence": 0.78,
                }
            )
        if capability_map.get("walk_in_space") and capability_map["walk_in_space"].get("supported"):
            items.append(
                {
                    "item": "整体尺寸、内部可用空间、带棚架/不带屋檐等尺寸口径确认。",
                    "impact": "影响大件产品购买前的空间判断和退货风险。",
                    "source_type": "needs_verification",
                    "evidence_ids": capability_map["walk_in_space"]["evidence_ids"],
                    "confidence": 0.86,
                }
            )
        if capability_map.get("reinforced_frame") and capability_map["reinforced_frame"].get("supported"):
            items.append(
                {
                    "item": "reinforced frame 的具体结构证明与安装稳定性证据。",
                    "impact": "影响结构可靠性卖点是否能在 Task 5 中放大。",
                    "source_type": "needs_verification",
                    "evidence_ids": capability_map["reinforced_frame"]["evidence_ids"],
                    "confidence": 0.72,
                }
            )
        return dedupe_by_text(items, "item")[:12]

    def build_confidence(self, ranking: list[dict[str, Any]], needs_verification: list[dict[str, Any]]) -> dict[str, Any]:
        research_quality = self.research_result.data_quality or {}
        research_confidence = float_or_default(research_quality.get("confidence"), 0.65)
        ranking_confidence = average_confidence(ranking[:5], default=0.55)
        missing_penalty = min(0.18, len(needs_verification) * 0.015)
        overall = max(0.1, min(0.9, (research_confidence * 0.45) + (ranking_confidence * 0.55) - missing_penalty))
        return {
            "overall": round(overall, 2),
            "research_confidence": research_confidence,
            "selling_point_ranking_confidence": round(ranking_confidence, 2),
            "evidence_summary": evidence_summary(self.evidence),
            "missing_data_count": len(needs_verification),
            "risk_notes": [
                "ResearchResult 中的 model_inference 没有被升级为 verified fact。",
                "缺少真实评论数据时，VOC 仅作为需求/顾虑假设使用。",
                "缺少 ASIN 级竞品数据时，竞争策略只做方向判断，不做精确压制。",
            ],
        }

    def input_snapshot(self) -> dict[str, Any]:
        return {
            "planning_context_used": {
                "status": self.planning_context.status,
                "english_core_keyword": self.keyword,
                "product_overview": self.overview,
                "core_product_parameters": [serialize_parameter(item) for item in self.parameters],
                "human_strategy_note_used": bool(self.human_strategy_note),
            },
            "research_result_used": {
                "status": self.research_result.status,
                "provider": self.research_result.research_provider,
                "web_search_used": self.research_result.web_search_used,
                "target_market": self.research_result.target_market,
                "market_context": self.research_result.market_context,
                "keyword_context": self.research_result.keyword_context,
                "audience_demand_context": self.research_result.audience_demand_context,
                "competitor_context": self.research_result.competitor_context,
                "voc_context": self.research_result.voc_context,
                "market_tier": self.research_result.market_tier,
                "opportunity_signals": self.research_result.opportunity_signals,
                "data_quality": self.research_result.data_quality,
                "evidence_count": len(self.evidence),
            },
        }

    def capability_candidates(self) -> list[dict[str, Any]]:
        text = searchable_product_text(self.project)
        return [
            self.capability(
                key="storage_workspace",
                label="内置 shelves / hooks / slat hanging panels 的收纳组织能力",
                patterns=[r"shelves?", r"hooks?", r"slat", r"storage", r"棚架", r"挂", r"收纳"],
                user_need="把植物、花盆和园艺工具集中放置，减少额外收纳配置。",
                perceived_value="用户能把 greenhouse 当成日常可用的园艺工作空间，而不是单一遮护棚。",
                market_basis="ResearchResult 指出 storage-integrated greenhouse / garden workspace 是值得 Task 4 进一步判断的机会信号。",
                base_scores=(0.92, 0.9, 0.86, 0.78, 0.86, 0.9),
                text=text,
                strategy_evidence_topics=[],
            ),
            self.capability(
                key="walk_in_space",
                label="walk-in 可进入式空间与多 SKU 尺寸覆盖",
                patterns=[r"walk[- ]?in", r"可进入", r"单品尺寸", r"内部空间", r"sku", r"\d+\s*x\s*\d+"],
                user_need="需要比 mini greenhouse 更大的站立、进入和日常打理空间。",
                perceived_value="降低弯腰、搬动和空间不足带来的使用阻力。",
                market_basis="ResearchResult 将本品判断为 walk-in greenhouse，并把 mini/PE 临时棚排除为非核心对标。",
                base_scores=(0.9, 0.86, 0.88, 0.66, 0.86, 0.82),
                text=text,
                strategy_evidence_topics=["internal-space"],
            ),
            self.capability(
                key="wood_frame",
                label="杉木 / 木质框架带来的庭院外观与材质感",
                patterns=[r"wood", r"wooden", r"cedar", r"杉木", r"木头", r"木质", r"natural"],
                user_need="希望 greenhouse 不只实用，还能与后院、花园和户外空间风格协调。",
                perceived_value="让大型温室从临时工具感转向更可接受的庭院设施感。",
                market_basis="ResearchResult 认为 wooden/cedar greenhouse 更强调外观、庭院融合度和中高配感知。",
                base_scores=(0.78, 0.82, 0.9, 0.72, 0.86, 0.84),
                text=text,
                strategy_evidence_topics=["coating"],
            ),
            self.capability(
                key="polycarbonate_panels",
                label="multi-layer PC / polycarbonate panels",
                patterns=[r"polycarbonate", r"\bpc\b", r"PC板", r"板子", r"multi-layer"],
                user_need="需要比软膜 cover 更稳定、更长期的植物保护结构。",
                perceived_value="提升用户对耐用性、采光和季节性保护的基础信任。",
                market_basis="ResearchResult 将 polycarbonate panel 视为该类目主要覆盖材料之一。",
                base_scores=(0.8, 0.82, 0.84, 0.64, 0.78, 0.78),
                text=text,
                strategy_evidence_topics=["pc-panels"],
            ),
            self.capability(
                key="sloped_roof",
                label="sloped roof 斜顶识别结构",
                patterns=[r"sloped roof", r"斜顶", r"roof", r"屋顶"],
                user_need="希望大型户外温室在造型上更接近庭院建筑，而非临时棚。",
                perceived_value="提供清晰的外观识别点，也为后续解释排水、空间感或建筑感预留方向。",
                market_basis="ResearchResult 把 sloped roof 作为明确的视觉区分信号，但相关功能价值仍需后续证明。",
                base_scores=(0.7, 0.74, 0.82, 0.68, 0.76, 0.8),
                text=text,
                strategy_evidence_topics=["roof-structure"],
            ),
            self.capability(
                key="ventilation",
                label="bottom ventilation grilles / 通风设计",
                patterns=[r"ventilation", r"vent", r"grilles?", r"通风"],
                user_need="需要温室内部空气流动，降低闷热、湿度和植物照料的不确定性。",
                perceived_value="让用户觉得温室不是封闭箱体，而是考虑了植物日常环境管理。",
                market_basis="ResearchResult 将 airflow / humidity management 标记为需要验证但值得关注的功能信号。",
                base_scores=(0.64, 0.7, 0.78, 0.56, 0.72, 0.62),
                text=text,
                strategy_evidence_topics=[],
            ),
            self.capability(
                key="reinforced_frame",
                label="reinforced frame 强化框架",
                patterns=[r"reinforced", r"frame", r"框架", r"强化", r"加固"],
                user_need="购买大件户外结构时需要降低晃动、安装和长期稳定性的担忧。",
                perceived_value="为耐用和结构可靠提供基础信号，但必须补充具体证明。",
                market_basis="ResearchResult 将 durability proof 和 installation proof 列为关键转化门槛。",
                base_scores=(0.7, 0.72, 0.8, 0.58, 0.7, 0.68),
                text=text,
                strategy_evidence_topics=[],
            ),
        ]

    def capability(
        self,
        *,
        key: str,
        label: str,
        patterns: list[str],
        user_need: str,
        perceived_value: str,
        market_basis: str,
        base_scores: tuple[float, float, float, float, float, float],
        text: str,
        strategy_evidence_topics: list[str],
    ) -> dict[str, Any]:
        supported = any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)
        evidence_ids = capability_evidence_ids(
            self.parameters,
            self.evidence_by_id,
            patterns,
            strategy_evidence_topics,
        )
        if "user-input-overview" in self.evidence_by_id and any(
            re.search(pattern, self.overview, re.IGNORECASE) for pattern in patterns
        ):
            evidence_ids.append("user-input-overview")
        if "user-input-keyword" in self.evidence_by_id and any(
            re.search(pattern, self.keyword, re.IGNORECASE) for pattern in patterns
        ):
            evidence_ids.append("user-input-keyword")
        if self.human_strategy_note and any(re.search(pattern, self.human_strategy_note, re.IGNORECASE) for pattern in patterns):
            supported = True

        evidence_ids = valid_evidence_ids(self.evidence_by_id, unique(evidence_ids))
        source_type = dominant_source_type([self.evidence_by_id.get(item, {}) for item in evidence_ids]) or "model_inference"
        evidence_score = evidence_reliability([self.evidence_by_id.get(item, {}) for item in evidence_ids])
        confidence = min(0.92, (sum(base_scores) / len(base_scores)) * 0.82 + evidence_score * 0.18)

        return {
            "key": key,
            "label": label,
            "supported": supported,
            "product_fact": label if supported else "未在 Planning Context 中确认",
            "product_source_type": source_type,
            "evidence_ids": evidence_ids,
            "user_need": user_need,
            "perceived_value": perceived_value,
            "market_basis": market_basis,
            "base_scores": base_scores,
            "confidence": round(confidence if supported else 0.0, 2),
        }


def score_capability(capability: dict[str, Any], research_result: Any, needs: list[dict[str, Any]]) -> dict[str, Any]:
    need_strength, perceived_value, product_strength, competitor_diff, evidence_value, communication_value = capability["base_scores"]
    need_text = " ".join([item.get("need", "") for item in needs[:5]])
    label = capability["label"]
    if related(label, need_text):
        need_strength = min(0.96, need_strength + 0.04)

    missing_data = json.dumps((research_result.data_quality or {}).get("missing_data", []), ensure_ascii=False)
    needs_verification = []
    if "ASIN" in missing_data or "竞品" in missing_data:
        competitor_diff = max(0.3, competitor_diff - 0.08)
        needs_verification.append("缺少 Amazon ASIN 级竞品验证，竞品差异度为方向性判断。")
    if any(word in label.lower() for word in ["reinforced", "frame", "wood", "cedar", "杉木", "框架"]):
        needs_verification.append("需要补充材质、结构、耐候或安装证明后才能在内容中强放大。")

    score = (
        need_strength * 0.24
        + perceived_value * 0.2
        + product_strength * 0.2
        + competitor_diff * 0.14
        + evidence_value * 0.12
        + communication_value * 0.1
    )
    return {
        "selling_point": capability["label"],
        "user_need": capability["user_need"],
        "product_fact": capability["product_fact"],
        "market_basis": capability["market_basis"],
        "perceived_value": capability["perceived_value"],
        "reason_for_ranking": (
            "排序综合了用户需求强度、用户感知价值、产品事实强度、竞品差异度、Evidence 可信度和传播价值；"
            f"该点的综合分为 {score:.2f}。"
        ),
        "ranking_factors": {
            "user_need_strength": round(need_strength, 2),
            "perceived_value": round(perceived_value, 2),
            "product_advantage_strength": round(product_strength, 2),
            "competitor_differentiation": round(competitor_diff, 2),
            "evidence_reliability": round(evidence_value, 2),
            "communication_value": round(communication_value, 2),
        },
        "product_source_type": capability["product_source_type"],
        "evidence_ids": capability["evidence_ids"],
        "confidence": capability["confidence"],
        "needs_verification": needs_verification,
        "score": score,
    }


def searchable_product_text(project: PlanningProject) -> str:
    planning = project.planning_context
    parts = [
        project.user_input.keywords,
        project.user_input.overview,
        str(planning.user_input),
        str(planning.facts),
        str(planning.product_common_parameters),
        str(planning.sku_differential_parameters),
    ]
    for item in planning.standard_chinese_parameter_table or []:
        parts.append(item.parameter)
        parts.extend(item.values or [])
        parts.append(str(item.values_by_sku or {}))
    for item in planning.research_brief.core_product_parameters or []:
        parts.append(item.name)
        parts.append(item.value)
        parts.append(str(item.values_by_sku or {}))
    return "\n".join(parts)


def capability_evidence_ids(
    parameters: Iterable[ResearchCoreParameter],
    evidence_by_id: dict[str, dict[str, Any]],
    patterns: list[str],
    strategy_evidence_topics: list[str],
) -> list[str]:
    ids: list[str] = []
    for index, parameter in enumerate(parameters, start=1):
        text = " ".join([parameter.name, parameter.value, json.dumps(parameter.values_by_sku, ensure_ascii=False)])
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns):
            evidence_id = f"spec-fact-{index}"
            if evidence_id in evidence_by_id:
                ids.append(evidence_id)
    for topic in strategy_evidence_topics:
        evidence_id = f"strategy-spec-fact-{topic}"
        if evidence_id in evidence_by_id:
            ids.append(evidence_id)
    return ids


def build_planning_strategy_evidence(
    project: PlanningProject,
    existing_evidence_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    topics = [
        ("pc-panels", [r"polycarbonate", r"\bPC\b", r"PC板", r"板子"], "标准参数表包含 PC / polycarbonate panel 相关规格。"),
        ("roof-structure", [r"sloped roof", r"斜顶", r"屋顶"], "标准参数表包含 roof / 斜顶结构相关信息。"),
        ("internal-space", [r"内部空间", r"可进入", r"walk[- ]?in"], "标准参数表包含内部空间或可进入式空间相关尺寸。"),
        ("coating", [r"木蜡油", r"涂层", r"coating"], "标准参数表包含木材涂层或表面处理相关信息。"),
    ]
    rows = project.planning_context.standard_chinese_parameter_table or []
    additions: list[dict[str, Any]] = []
    for topic, patterns, claim in topics:
        matches = []
        for row in rows:
            row_text = json.dumps(
                {
                    "parameter": row.parameter,
                    "values": row.values[:4],
                    "values_by_sku": dict(list((row.values_by_sku or {}).items())[:4]),
                },
                ensure_ascii=False,
            )
            if any(re.search(pattern, row_text, re.IGNORECASE) for pattern in patterns):
                matches.append(
                    {
                        "parameter": row.parameter,
                        "values": row.values[:4],
                        "values_by_sku": dict(list((row.values_by_sku or {}).items())[:4]),
                    }
                )
        if not matches:
            continue
        evidence_id = f"strategy-spec-fact-{topic}"
        if evidence_id in existing_evidence_by_id:
            continue
        additions.append(
            {
                "id": evidence_id,
                "source_type": "spec_fact",
                "claim": claim,
                "source": "planning_context.standard_chinese_parameter_table",
                "details": matches[:3],
                "confidence": 0.92,
            }
        )
    return additions


def serialize_parameter(parameter: ResearchCoreParameter) -> dict[str, Any]:
    if hasattr(parameter, "model_dump"):
        return parameter.model_dump(mode="json")
    return parameter.dict()


def normalize_evidence(item: dict[str, Any]) -> dict[str, Any]:
    data = dict(item or {})
    if "source_type" not in data:
        data["source_type"] = "model_inference"
    if "id" in data:
        data["id"] = str(data["id"])
    return data


def evidence_text(item: dict[str, Any]) -> str:
    return json.dumps(item, ensure_ascii=False)


def judgement(value: str, source_type: str, evidence_ids: list[str], confidence: float) -> dict[str, Any]:
    return {
        "value": value,
        "source_type": source_type,
        "evidence_ids": unique(evidence_ids),
        "confidence": round(float_or_default(confidence, 0.6), 2),
    }


def text_of(item: Any) -> str:
    if item is None:
        return ""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in ("value", "term", "tier_judgement", "summary", "name", "product_type", "opportunity", "need", "pain", "barrier", "item"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    if isinstance(item, list):
        return first_value(item)
    return str(item).strip()


def first_value(item: Any) -> str:
    for value in list_dicts(item):
        text = text_of(value)
        if text:
            return text
    if isinstance(item, dict):
        return text_of(item)
    if isinstance(item, str):
        return item.strip()
    return ""


def list_dicts(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def evidence_ids_of(item: Any) -> list[str]:
    if isinstance(item, dict):
        ids = item.get("evidence_ids") or item.get("evidence") or []
        if isinstance(ids, str):
            return [ids]
        if isinstance(ids, list):
            return [str(value) for value in ids if value]
    return []


def source_type_of(item: Any) -> str:
    if isinstance(item, dict):
        value = item.get("source_type")
        if isinstance(value, str) and value:
            return value
    return ""


def confidence_of(item: Any, default: float = 0.6) -> float:
    if isinstance(item, dict):
        return float_or_default(item.get("confidence"), default)
    return default


def float_or_default(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def collect_ids(items: Iterable[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for item in items:
        ids.extend(evidence_ids_of(item))
    return unique(ids)


def valid_evidence_ids(evidence_by_id: dict[str, dict[str, Any]], evidence_ids: Iterable[str]) -> list[str]:
    return [item for item in unique([str(value) for value in evidence_ids if value]) if item in evidence_by_id]


def unique(values: Iterable[Any]) -> list[Any]:
    result = []
    seen = set()
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
        if not value or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def dominant_source_type(items: Iterable[dict[str, Any]]) -> str:
    types = {source_type_of(item) for item in items if source_type_of(item)}
    for source_type in SOURCE_PRIORITY:
        if source_type in types:
            return source_type
    return ""


def evidence_reliability(items: Iterable[dict[str, Any]]) -> float:
    scores = {
        "sorftime_data": 0.95,
        "web_evidence": 0.82,
        "spec_fact": 0.9,
        "user_input": 0.78,
        "model_inference": 0.55,
        "needs_verification": 0.25,
    }
    values = [scores.get(source_type_of(item), 0.45) for item in items if item]
    if not values:
        return 0.35
    return sum(values) / len(values)


def average_confidence(items: Iterable[Any], default: float = 0.6) -> float:
    values = [confidence_of(item, default) for item in items if item]
    if not values:
        return default
    return sum(values) / len(values)


def evidence_summary(evidence: list[dict[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for item in evidence:
        source_type = source_type_of(item) or "unknown"
        summary[source_type] = summary.get(source_type, 0) + 1
    return summary


def dedupe_by_text(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for item in items:
        text = str(item.get(key) or "").strip()
        normalized = re.sub(r"\s+", " ", text.lower())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(item)
    return result


def normalize_need_text(text: str) -> str:
    text = text.strip()
    if text.startswith("potential demand/concern hypothesis："):
        return text.replace("potential demand/concern hypothesis：", "").strip()
    if text.startswith("potential demand/concern hypothesis:"):
        return text.replace("potential demand/concern hypothesis:", "").strip()
    return text


def strip_terminal_punctuation(text: str) -> str:
    return re.sub(r"[。.!！]+$", "", text.strip())


def infer_need_impact(text: str) -> str:
    lower = text.lower()
    if any(word in lower for word in ("shelves", "hooks", "slat", "storage", "收纳", "整理")):
        return "它把购买理由从单一植物保护推进到日常组织效率和空间利用。"
    if any(word in lower for word in ("walk", "space", "空间", "尺寸")):
        return "它直接影响大件温室是否好进出、好打理、能放下真实植物和工具。"
    if any(word in lower for word in ("wood", "cedar", "木", "杉")):
        return "它影响用户是否愿意把温室长期摆在可见庭院里。"
    if any(word in lower for word in ("install", "assembly", "安装")):
        return "它是大件产品下单前的重要风险门槛。"
    if any(word in lower for word in ("vent", "airflow", "humidity", "通风")):
        return "它关系到植物日常养护环境是否可控。"
    return "它会影响用户从关注参数转向判断产品是否能完成真实任务。"


def buyer_question_for(text: str) -> str:
    lower = text.lower()
    if any(word in lower for word in ("wood", "cedar", "木", "杉")):
        return "这个木质结构是否适合长期户外使用，并且外观是否值得放在庭院里？"
    if any(word in lower for word in ("walk", "space", "尺寸", "height", "footprint")):
        return "我能不能站进去、放下植物和工具，尺寸会不会买错？"
    if any(word in lower for word in ("polycarbonate", "pc", "panel", "板")):
        return "面板是否比软膜更稳、更适合长期保护植物？"
    if any(word in lower for word in ("shelf", "hook", "slat", "storage", "收纳", "棚架")):
        return "它是否能减少额外架子和工具收纳的麻烦？"
    if any(word in lower for word in ("vent", "通风", "airflow")):
        return "植物日常养护时空气流动是否足够？"
    if any(word in lower for word in ("install", "assembly", "安装")):
        return "安装是否复杂，买回家后能不能顺利搭起来？"
    return "这个点是否能降低购买风险或提升日常使用价值？"


def proof_needed_for(text: str) -> str:
    lower = text.lower()
    if any(word in lower for word in ("review", "评论", "voc")):
        return "需要真实评论样本或 Sorftime review-analysis 支撑。"
    if any(word in lower for word in ("asin", "listing", "竞品", "价格", "bsr", "销量", "搜索量")):
        return "需要 Sorftime / Amazon US listing / keyword 数据验证。"
    if any(word in lower for word in ("wood", "cedar", "木", "杉", "耐候", "防腐")):
        return "需要材质、防腐、涂层、户外耐候或质保证据。"
    if any(word in lower for word in ("尺寸", "space", "footprint", "overall")):
        return "需要清晰尺寸图、内部可用空间和 SKU 差异表。"
    if any(word in lower for word in ("install", "assembly", "安装")):
        return "需要安装步骤、配件清单、人数/时间预期或说明书证据。"
    return "需要在 Task 5 前补充可被用户理解的证明素材。"


def strategic_implication_for(text: str) -> str:
    lower = text.lower()
    if any(word in lower for word in ("storage", "shelves", "hooks", "slat", "收纳", "整理")):
        return "优先把它转译为用户任务价值：更好摆放植物、更好收纳工具、更容易日常照料。"
    if any(word in lower for word in ("sloped", "roof", "斜顶")):
        return "可作为视觉识别和结构理解入口，但功能承诺需要后续证据。"
    if any(word in lower for word in ("size", "尺寸", "footprint", "usable")):
        return "后续内容应把尺寸讲清楚，以降低大件购买前的不确定性。"
    if any(word in lower for word in ("vent", "airflow", "通风")):
        return "可作为环境管理信号，但是否主打取决于评论和竞品验证。"
    return "可作为 Task 5 内容取舍的候选策略信号，需要继续看证据强度。"


def infer_audience_role(text: str) -> str:
    lower = text.lower()
    if any(word in lower for word in ("home", "家庭", "后院", "庭院", "backyard")):
        return "家庭园艺 / 后院使用者"
    if any(word in lower for word in ("hobby", "爱好")):
        return "园艺爱好者"
    if any(word in lower for word in ("aesthetic", "美观", "户外")):
        return "重视庭院观感的户外空间用户"
    return "潜在 Amazon US 买家"


def related(label: str, text: str) -> bool:
    label_lower = label.lower()
    text_lower = text.lower()
    groups = [
        ("storage", "shelf", "hook", "slat", "收纳", "整理", "棚架"),
        ("walk", "space", "尺寸", "可进入", "内部空间"),
        ("wood", "cedar", "木", "杉"),
        ("polycarbonate", "pc", "panel", "板"),
        ("vent", "通风", "airflow"),
    ]
    for group in groups:
        if any(word in label_lower for word in group) and any(word in text_lower for word in group):
            return True
    return False


def first_supported(capabilities: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    for item in capabilities:
        if item.get("supported") and key in item.get("key", ""):
            return item
    return None


def join_cn(values: list[str]) -> str:
    values = [value for value in values if value]
    if not values:
        return "已确认的核心能力"
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return "和".join(values)
    return "、".join(values)
