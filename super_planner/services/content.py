# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from typing import Any

from super_planner.core.exceptions import WorkflowBusinessInterruption, WorkflowSystemError
from super_planner.schemas.project import ContentResult, PlanningProject
from super_planner.schemas.workflow import ConfirmationQuestion, ConfirmationRequest
from super_planner.services.content_openai import MAX_LENGTH_REWRITE_ROUNDS, OpenAICopyGenerator
from super_planner.services.content_rules import (
    FORMAT_RULES_VERSION,
    LENGTH_RULES,
    attach_length_metadata,
    best_length_candidate,
    failed_length_checks,
    item_length_ok,
)
from super_planner.services.research_providers.base import ProgressCallback, emit_progress


CONTENT_VERSION = "content-engine-v0.1.1-openai-copy"
DEFAULT_BRAND_NAME = os.getenv("SUPER_PLANNER_DEFAULT_BRAND", "VEVOR").strip() or "VEVOR"


def run_content(project: PlanningProject, progress: ProgressCallback | None = None) -> None:
    engine = ContentEngine(project)
    if engine.strategy_status != "completed":
        raise WorkflowSystemError("Content requires a completed StrategyResult. Please contact IT.")

    critical_gaps = engine.critical_gaps()
    if critical_gaps:
        project.content_result = ContentResult(
            status="waiting_confirmation",
            summary="文案生成缺少可执行的策略依据，等待用户补充后继续。",
            content_version=CONTENT_VERSION,
            generated_at=datetime.utcnow(),
            needs_verification=[
                {
                    "item": gap,
                    "impact": "该缺口会导致文案无法安全生成。",
                    "source_type": "needs_verification",
                    "confidence": 1.0,
                }
                for gap in critical_gaps
            ],
            raw_payload={"content_version": CONTENT_VERSION, "critical_gaps": critical_gaps},
        )
        raise WorkflowBusinessInterruption(engine.confirmation_request(critical_gaps))

    project.content_result = engine.build(progress=progress)


class ContentEngine:
    def __init__(self, project: PlanningProject) -> None:
        self.project = project
        self.planning_context = project.planning_context
        self.research_result = project.research_result
        self.strategy_result = project.strategy_result
        self.strategy_status = (project.strategy_result.status or "").strip()
        self.evidence = list(project.strategy_result.evidence or project.research_result.evidence or [])
        self.evidence_by_id = {str(item.get("id")): item for item in self.evidence if isinstance(item, dict) and item.get("id")}
        self.keyword = (
            self.planning_context.research_brief.english_core_keyword
            or self.planning_context.research_brief.raw_keywords
            or project.user_input.keywords
            or "Wooden Greenhouse"
        ).strip()
        self.sku_ids = [sku.sku_id for sku in self.planning_context.sku_list or [] if sku.sku_id]
        self.top_points = usable_selling_points(project.strategy_result.selling_point_ranking)
        self.needs_verification = list(project.strategy_result.needs_verification or [])
        self.core_terms = extract_keyword_terms(project.research_result.keyword_context, "core_terms")
        self.attribute_terms = extract_keyword_terms(project.research_result.keyword_context, "attribute_terms")
        self.use_case_terms = extract_keyword_terms(project.research_result.keyword_context, "use_case_terms")
        self.size_values_by_sku = find_parameter_values_by_sku(self.planning_context, ["单品尺寸", "overall size", "product size"])

    def critical_gaps(self) -> list[str]:
        gaps = []
        if not self.top_points:
            gaps.append("StrategyResult 中没有可进入文案生成的真实卖点排序。")
        if not (self.strategy_result.user_purchase_reason or {}).get("core_reason"):
            gaps.append("StrategyResult 缺少 User Purchase Reason，无法形成统一传播主线。")
        return gaps

    def confirmation_request(self, gaps: list[str]) -> ConfirmationRequest:
        background = {
            "strategy_status": self.strategy_result.status,
            "strategy_summary": self.strategy_result.summary,
            "selling_point_ranking": self.strategy_result.selling_point_ranking,
            "critical_gaps": gaps,
        }
        return ConfirmationRequest(
            id=uuid.uuid4().hex,
            step_id="content",
            title="文案生成需要补充策略依据",
            reason="缺少可执行的用户购买理由或真实卖点排序，需用户确认后继续。",
            questions=[
                ConfirmationQuestion(
                    id="content_strategy_confirmation",
                    prompt="请补充可用于文案生成的核心传播主线或真实卖点依据。",
                    background=json.dumps(background, ensure_ascii=False, indent=2),
                )
            ],
        )

    def build(self, progress: ProgressCallback | None = None) -> ContentResult:
        emit_progress(progress, "正在规划内容结构")
        mapping = self.build_content_mapping()

        emit_progress(progress, "正在映射核心卖点")
        item_specs = self.build_content_item_specs(mapping)
        generator = OpenAICopyGenerator()

        emit_progress(progress, "正在生成中英文文案")
        openai_payload = generator.generate_initial_payload(self.build_openai_generation_payload(mapping, item_specs))

        emit_progress(progress, "正在生成Supporting Image文案")
        items = self.merge_openai_items(item_specs, openai_payload)

        emit_progress(progress, "正在生成A+文案")
        items, length_rewrite_summary = self.enforce_length_rules(items, generator)

        emit_progress(progress, "正在生成Bullet Points")

        modules = group_modules(items)
        minimal_checks = run_minimal_checks(items)
        confidence = build_content_confidence(items, self.strategy_result, self.needs_verification)
        generator_meta = generator.meta()
        generation_apis = unique([call.get("api", "") for call in generator_meta.get("calls", [])])

        emit_progress(progress, "文案生成完成")
        return ContentResult(
            status="completed",
            summary=f"文案生成完成：已通过 OpenAI 生成 Item Name、Item Highlight、F1-F8、Premium A+、BP1-BP5 和 Product Description。",
            content_version=CONTENT_VERSION,
            generated_at=datetime.utcnow(),
            generation_provider="openai",
            generation_model=generator_meta.get("model", ""),
            generation_api=", ".join(generation_apis),
            format_rules_version=FORMAT_RULES_VERSION,
            content_mapping=mapping,
            content_items=items,
            modules=modules,
            evidence=self.evidence,
            confidence=confidence,
            needs_verification=self.needs_verification,
            length_rewrite_summary=length_rewrite_summary,
            minimal_checks=minimal_checks,
            drafts=build_legacy_drafts(modules),
            raw_payload={
                "content_version": CONTENT_VERSION,
                "generation_mode": "openai_copy_generation_with_length_control",
                "openai": generator_meta,
                "openai_response_notes": openai_payload.get("notes", ""),
                "input_snapshot": self.input_snapshot(),
                "guardrails": [
                    "No spec re-parse.",
                    "No market re-research.",
                    "StrategyResult is the only strategy source.",
                    "No unverified numeric market claims.",
                    "SKU-specific parameters stay SKU-scoped.",
                    "No direct string truncation for length control.",
                ],
            },
        )

    def build_content_mapping(self) -> dict[str, Any]:
        selling_point_distribution = []
        module_map = {
            1: {"primary": ["Supporting Image Copy F2", "Bullet Point 1", "Premium A+ Module 2"], "secondary": ["Product Description"]},
            2: {"primary": ["Supporting Image Copy F3", "Bullet Point 2", "Premium A+ Module 3"], "secondary": ["F7 SKU Size Reference"]},
            3: {"primary": ["Supporting Image Copy F4", "Bullet Point 3", "Premium A+ Module 4"], "secondary": ["Title"]},
            4: {"primary": ["Supporting Image Copy F5", "Bullet Point 4", "Premium A+ Module 4"], "secondary": ["Title"]},
            5: {"primary": ["Supporting Image Copy F6", "Bullet Point 5", "Premium A+ Module 5"], "secondary": ["Title"]},
        }
        for point in self.top_points[:5]:
            rank = int(point.get("rank") or len(selling_point_distribution) + 1)
            selling_point_distribution.append(
                {
                    "rank": rank,
                    "selling_point": point.get("selling_point", ""),
                    "primary_modules": module_map.get(rank, {}).get("primary", []),
                    "secondary_modules": module_map.get(rank, {}).get("secondary", []),
                    "avoid_repetition_rule": "同一卖点在主承接模块讲完整，在其他模块只做关键词或证据补充。",
                    "evidence_reference": point.get("evidence_ids", []),
                    "confidence": point.get("confidence", 0),
                }
            )

        return {
            "content_version": CONTENT_VERSION,
            "strategy_source": {
                "strategy_version": self.strategy_result.strategy_version,
                "user_purchase_reason": (self.strategy_result.user_purchase_reason or {}).get("core_reason", ""),
                "communication_main_line": (self.strategy_result.communication_strategy or {}).get("main_line", ""),
                "competitive_strategy": (self.strategy_result.competitive_strategy or {}).get("strategy", ""),
            },
            "module_responsibilities": [
                {
                    "module": "Title / Title Reference",
                    "role": "压缩表达类目、核心关键词和最重要的真实能力；Title Reference 说明关键词与结构取舍。",
                    "main_strategy_reference": ["product_category", "communication_strategy", "selling_point_ranking"],
                    "sku_scope_policy": "ALL；不写入 SKU 独有尺寸。",
                },
                {
                    "module": "Supporting Image Copy F1-F8",
                    "role": "F1 承接 User Purchase Reason；F2-F6 分别承接 TOP1-TOP5；F7 解释 SKU 尺寸；F8 承担 Proof/预期管理。",
                    "main_strategy_reference": ["user_purchase_reason", "selling_point_ranking", "purchase_barriers"],
                    "sku_scope_policy": "F1-F6/F8 为 ALL；F7 生成 SKU-specific variants。",
                },
                {
                    "module": "Premium A+",
                    "role": "用更完整的叙事展开场景、解决方案、材料/结构、规格理解和信任证明。",
                    "main_strategy_reference": ["communication_strategy", "competitive_strategy", "focus_points"],
                    "sku_scope_policy": "ALL；涉及尺寸时只提示按所选 SKU 查看。",
                },
                {
                    "module": "Bullet Point 1-5",
                    "role": "按 Strategy TOP1-TOP5 生成五点，不重新排序。",
                    "main_strategy_reference": ["selling_point_ranking"],
                    "sku_scope_policy": "ALL；不套用单 SKU 参数。",
                },
                {
                    "module": "Product Description / Transition",
                    "role": "用移动端友好的段落串联用户任务、核心场景、产品事实和购买前注意事项。",
                    "main_strategy_reference": ["user_mindset", "user_purchase_reason", "purchase_barriers"],
                    "sku_scope_policy": "ALL；规格细节进入选择前确认提醒。",
                },
            ],
            "selling_point_distribution": selling_point_distribution,
            "parameter_usage": {
                "all_sku_safe": ["材质", "外观件主要材质", "PC板/PC面板", "结构形态"],
                "sku_specific": [
                    {
                        "parameter": "单品尺寸",
                        "module": "Supporting Image Copy F7",
                        "sku_ids": list(self.size_values_by_sku.keys()),
                    }
                ],
            },
            "copy_boundaries": [
                "不重新决定产品定位、购买理由或卖点排序。",
                "不使用 StrategyResult.needs_verification 作为确定事实。",
                "不生成图片、构图、视觉参考或设计要求。",
            ],
        }

    def build_content_item_specs(self, mapping: dict[str, Any]) -> list[dict[str, Any]]:
        p1 = point_by_rank(self.top_points, 1)
        p2 = point_by_rank(self.top_points, 2)
        p3 = point_by_rank(self.top_points, 3)
        p4 = point_by_rank(self.top_points, 4)
        p5 = point_by_rank(self.top_points, 5)
        top_refs = self.top_points[:5]
        purchase_reason_ids = (self.strategy_result.user_purchase_reason or {}).get("evidence_ids", [])
        title_ids = unique(flatten([point.get("evidence_ids", []) for point in top_refs[:5]]))
        brand = find_brand_name(self.planning_context)
        brand_source = find_brand_source(self.planning_context)
        brand_missing_note = [] if brand else ["品牌字段缺失；Item Name 使用 Brand 占位，发布前必须替换为真实品牌。"]

        specs = [
            self.spec(
                module="Title",
                submodule="Item Name",
                sequence=1,
                length_rule_id="item_name",
                copy_task=(
                    "Generate the Amazon Item Name using the required structure: brand + core keyword + core parameter + keyword. "
                    "Use the supplied brand context. Do not include SKU-specific dimensions."
                ),
                english_seed=f"{brand} Wooden Greenhouse, Walk-In Sloped Roof Kit with 5-6 mm PC Panels",
                chinese_seed="Brand 木质温室，5-6 mm PC 板，可进入式斜顶温室套件",
                strategy_reference=["product_category", "communication_strategy", "selling_point_ranking"],
                selling_point_reference=rank_refs(top_refs[:5]),
                evidence_reference=title_ids,
                confidence=min_confidence(top_refs[:5], 0.72),
                needs_verification=collect_point_needs(top_refs[:5], max_items=2) + brand_missing_note,
                extra={"brand_status": brand_source, "brand": brand},
            ),
            self.spec(
                module="Title Reference",
                submodule="Item Highlight",
                sequence=1,
                length_rule_id="item_highlight",
                copy_task=(
                    "Generate Item Highlight in 120-125 characters. Express scenario, purchase reason/function, and long-tail search intent. "
                    "Avoid repeating the same information already expressed in Item Name. Do not mention accessories or certifications."
                ),
                english_seed="For Backyard Gardeners Creating an Organized Walk-In Plant Care Space with Shelves, Hooks and Hanging Panels",
                chinese_seed="适合后院园艺用户打造带棚架、挂钩和挂板的可进入式植物照料空间",
                strategy_reference=["keyword_context", "content_mapping", "user_purchase_reason"],
                selling_point_reference=rank_refs(top_refs[:5]),
                evidence_reference=unique(["user-input-keyword"] + title_ids),
                confidence=0.78,
                needs_verification=["关键词搜索量缺失，当前 Item Highlight 词序基于 ResearchResult 关键词分层和 StrategyResult 判断。"],
            ),
            self.spec(
                module="eBay Title",
                submodule="eBay Title",
                sequence=1,
                length_rule_id="ebay_title",
                copy_task="Generate an eBay title within 80 characters. Keep it concise, factual, and keyword-led.",
                english_seed="Wooden Walk-In Greenhouse with Sloped Roof, PC Panels and Shelves",
                chinese_seed="木质可进入式温室，配斜顶、PC 板和棚架",
                strategy_reference=["keyword_context", "selling_point_ranking"],
                selling_point_reference=rank_refs(top_refs[:4]),
                evidence_reference=title_ids,
                confidence=0.76,
                needs_verification=collect_point_needs(top_refs[:4], max_items=2),
            ),
        ]

        supporting_specs = [
            ("F1", 1, [p1, p2, p3], "Express the User Purchase Reason: a walk-in backyard garden workspace for growing, organizing, and daily plant care.", "Walk-In Garden Workspace", "Turn one backyard greenhouse into a place for growing, organizing, and caring for plants."),
            ("F2", 2, [p1], "Carry TOP1: built-in shelves, hooks, and slat hanging panels for organization.", "Built-In Storage", "Shelves, hooks, and slat panels help plants and garden tools stay organized."),
            ("F3", 3, [p2], "Carry TOP2: walk-in space and multi-SKU size coverage without putting one SKU size into ALL copy.", "Step-In Plant Care", "The walk-in format gives you room to arrange pots and handle daily greenhouse tasks."),
            ("F4", 4, [p3], "Carry TOP3: natural wood frame and backyard appearance.", "Natural Wood Frame", "The wood frame helps the greenhouse feel more at home in a patio or garden setting."),
            ("F5", 5, [p4], "Carry TOP4: multi-layer PC / polycarbonate panels as a factual covered growing structure.", "PC Panel Coverage", "Multi-layer PC panels help create a covered growing area for seasonal plant care."),
            ("F6", 6, [p5], "Carry TOP5: sloped roof visual structure and finished backyard profile.", "Sloped Roof Profile", "A clean roof line gives the greenhouse a more finished garden-house look."),
            ("F7", 7, [p2], "Explain SKU size selection. Do not put a single SKU dimension in ALL copy. SKU variants are generated from Task 2 parameters.", "Choose Your Size", "Review the selected SKU size before planning the footprint and interior layout."),
            ("F8", 8, [p2], "Handle proof and expectation management: setup planning, footprint, selected SKU size, and installation needs.", "Plan Before Setup", "Confirm the footprint, selected size, and installation needs before assembly."),
        ]
        for slot, sequence, points, task, title_seed, desc_seed in supporting_specs:
            variants = self.build_sku_size_variants() if slot == "F7" else []
            specs.append(
                self.spec(
                    module="Supporting Image Copy",
                    submodule=slot,
                    sequence=sequence,
                    sku_scope="SKU_SPECIFIC" if slot == "F7" and variants else "ALL",
                    length_rule_id="supporting_image_title",
                    copy_shape="supporting_image",
                    primary_part="title",
                    copy_task=task,
                    english_seed=f"{title_seed}\n{desc_seed}",
                    chinese_seed="",
                    strategy_reference=["user_purchase_reason", "communication_strategy"] if slot == "F1" else ["selling_point_ranking", "communication_strategy"],
                    selling_point_reference=rank_refs(points),
                    evidence_reference=purchase_reason_ids if slot == "F1" else collect_strategy_ids([point for point in points if point]),
                    confidence=min_confidence(points, 0.78),
                    needs_verification=(shared_verification(self.needs_verification) if slot == "F1" else collect_point_needs(points, max_items=2)),
                    parameter_reference=(
                        [{"parameter": "单品尺寸", "scope": "sku_variable", "sku_ids": list(self.size_values_by_sku.keys())}]
                        if slot == "F7" and variants
                        else None
                    ),
                    extra={
                        "copy_parts": {
                            "title": {"length_rule_id": "supporting_image_title", "english_copy": title_seed, "chinese_copy": ""},
                            "description": {"length_rule_id": "supporting_image_description", "english_copy": desc_seed, "chinese_copy": ""},
                        },
                        "copy_part_order": ["title", "description"],
                        "sku_scope_detail": {
                            "sku_ids": list(self.size_values_by_sku.keys()) if slot == "F7" and variants else self.sku_ids,
                            "reason": "F7 包含 SKU 独有尺寸，因此生成 sku_copy_variants。" if slot == "F7" and variants else "",
                        },
                        "sku_copy_variants": variants,
                    },
                )
            )

        aplus_specs = [
            ("A+ Module 1", 1, [p1, p2, p3], "Introduce the overall communication line: more than a plant shelter, a walk-in backyard garden workspace.", "Garden Workspace", "More Than a Plant Shelter", "Create a walk-in backyard space for growing, organizing, and everyday plant care."),
            ("A+ Module 2", 2, [p1], "Expand organization: shelves, hooks, and slat hanging panels.", "Organized Storage", "Organized from the Start", "Built-in shelves, hooks, and slat hanging panels give plants, pots, and tools their own place."),
            ("A+ Module 3", 3, [p2], "Expand walk-in use: arranging, watering, checking, and daily care.", "Walk-In Access", "Room to Step In and Work", "The walk-in format supports daily watering, arranging, checking, and plant care routines."),
            ("A+ Module 4", 4, [p3, p4], "Connect wood frame and PC panels as factual structure and garden appearance.", "Wood and PC", "Wood Frame + PC Panels", "A natural wood frame and multi-layer PC panels balance backyard style with greenhouse function."),
            ("A+ Module 5", 5, [p5], "Expand sloped roof as visual/backyard profile, not unsupported drainage performance.", "Sloped Roof", "Cleaner Backyard Profile", "The sloped roof shape gives the greenhouse a more architectural garden look."),
            ("A+ Module 6", 6, [p2], "Handle specification understanding and setup planning by selected SKU.", "Setup Planning", "Plan by SKU Before Setup", "Review the selected size, usable space, and shelf layout before assembly."),
        ]
        for submodule, sequence, points, task, tag_seed, title_seed, desc_seed in aplus_specs:
            specs.append(
                self.spec(
                    module="Premium A+",
                    submodule=submodule,
                    sequence=sequence,
                    length_rule_id="premium_aplus_title",
                    copy_shape="premium_aplus",
                    primary_part="title",
                    copy_task=task,
                    english_seed=f"{tag_seed}\n{title_seed}\n{desc_seed}",
                    chinese_seed="",
                    strategy_reference=["communication_strategy", "competitive_strategy", "focus_points"],
                    selling_point_reference=rank_refs(points),
                    evidence_reference=collect_strategy_ids([point for point in points if point]) or purchase_reason_ids,
                    confidence=min_confidence(points, 0.8),
                    needs_verification=collect_point_needs(points, max_items=3) or shared_verification(self.needs_verification),
                    parameter_reference=(
                        [{"parameter": "单品尺寸", "scope": "sku_variable", "sku_ids": list(self.size_values_by_sku.keys())}]
                        if sequence == 6
                        else None
                    ),
                    extra={
                        "copy_parts": {
                            "tag": {"length_rule_id": "premium_aplus_tag", "english_copy": tag_seed, "chinese_copy": ""},
                            "title": {"length_rule_id": "premium_aplus_title", "english_copy": title_seed, "chinese_copy": ""},
                            "description": {"length_rule_id": "premium_aplus_description", "english_copy": desc_seed, "chinese_copy": ""},
                        },
                        "copy_part_order": ["tag", "title", "description"],
                    },
                )
            )

        specs.extend(
            [
                self.spec(
                    module="Premium A+",
                    submodule="Introduction-2 Question",
                    sequence=90,
                    length_rule_id="introduction_2_question",
                    copy_task="Generate a customer-facing question about why this greenhouse is more than a temporary plant cover.",
                    english_seed="Need a greenhouse that also works as an organized backyard garden space?",
                    chinese_seed="是否需要一个不只遮护植物、还能整理后院园艺空间的温室？",
                    strategy_reference=["user_mindset", "user_purchase_reason"],
                    selling_point_reference=rank_refs([p1, p2]),
                    evidence_reference=purchase_reason_ids,
                    confidence=0.78,
                    needs_verification=shared_verification(self.needs_verification),
                ),
                self.spec(
                    module="Premium A+",
                    submodule="Introduction-2 Answer",
                    sequence=91,
                    length_rule_id="introduction_2_answer",
                    copy_task="Answer the Introduction-2 question using only verified product facts and StrategyResult.",
                    english_seed="This walk-in wooden greenhouse combines PC panels, shelves, hooks, and slat hanging panels so plant care and storage stay in one place.",
                    chinese_seed="这款可进入式木质温室结合 PC 板、棚架、挂钩和挂板，让植物照料与收纳集中在一个空间中。",
                    strategy_reference=["user_purchase_reason", "communication_strategy"],
                    selling_point_reference=rank_refs([p1, p2, p4]),
                    evidence_reference=unique(purchase_reason_ids + collect_strategy_ids([p1, p2, p4])),
                    confidence=0.8,
                    needs_verification=shared_verification(self.needs_verification),
                ),
            ]
        )

        bullet_seeds = {
            1: ("Organized Greenhouse Workspace", "Built-in shelves, hooks, and slat hanging panels help keep plants, pots, and garden tools arranged inside one walk-in greenhouse"),
            2: ("Walk-In Plant Care Space", "The walk-in design gives gardeners room to arrange potted plants and handle daily greenhouse care"),
            3: ("Natural Wood Frame", "The wood frame gives the greenhouse a warmer garden look for patios, backyards, and outdoor growing areas"),
            4: ("PC Panel Coverage", "Multi-layer PC panels help create a covered growing area for seasonal plant care"),
            5: ("Sloped Roof Design", "The sloped roof profile adds a clean garden-house look while setup details stay tied to the selected SKU"),
        }
        for rank in range(1, 6):
            point = point_by_rank(self.top_points, rank)
            title_seed, desc_seed = bullet_seeds[rank]
            specs.append(
                self.spec(
                    module="Bullet Point",
                    submodule=f"BP{rank}",
                    sequence=rank,
                    length_rule_id="bullet_point",
                    copy_task=(
                        f"Generate Bullet Point {rank} using Strategy TOP{rank}. Format exactly as Title: Description. "
                        "Use a clear product subject. Do not end with punctuation."
                    ),
                    english_seed=f"{title_seed}: {desc_seed}",
                    chinese_seed="",
                    strategy_reference=["selling_point_ranking", "communication_strategy"],
                    selling_point_reference=rank_refs([point]),
                    evidence_reference=(point or {}).get("evidence_ids", []),
                    confidence=float((point or {}).get("confidence") or 0.68),
                    needs_verification=list((point or {}).get("needs_verification") or []),
                    parameter_reference=parameter_refs_from_evidence(self.evidence_by_id, (point or {}).get("evidence_ids", [])),
                )
            )

        ids = unique(purchase_reason_ids + collect_strategy_ids(self.top_points[:5]))
        specs.extend(
            [
                self.spec(
                    module="Product Description",
                    submodule="Transition",
                    sequence=1,
                    length_rule_id="unbounded",
                    copy_task="Generate a short transition line connecting plant shelter to organized garden workspace.",
                    english_seed="From plant shelter to garden workspace, this wooden greenhouse is planned around everyday backyard plant care.",
                    chinese_seed="从植物遮护到花园工作空间，这款木质温室围绕日常后院植物照料展开。",
                    strategy_reference=["communication_strategy", "user_mindset"],
                    selling_point_reference=rank_refs(self.top_points[:3]),
                    evidence_reference=ids,
                    confidence=0.82,
                    needs_verification=shared_verification(self.needs_verification),
                ),
                self.spec(
                    module="Product Description",
                    submodule="Product Description",
                    sequence=2,
                    length_rule_id="unbounded",
                    copy_task=(
                        "Generate a concise product description with 3-4 short paragraphs. "
                        "Do not add new strategy. Mention SKU dimensions only as a selected-SKU check, not exact shared values."
                    ),
                    english_seed="Give plants and garden tools a more organized backyard space with a walk-in wooden greenhouse built around everyday care.",
                    chinese_seed="用可进入式木质温室，为植物和园艺工具提供更有序的后院空间。",
                    strategy_reference=["user_purchase_reason", "communication_strategy", "purchase_barriers"],
                    selling_point_reference=rank_refs(self.top_points[:5]),
                    parameter_reference=[
                        {"parameter": "单品尺寸", "scope": "sku_variable", "usage": "提醒按所选 SKU 核对，不在共享描述中写入具体值。"}
                    ],
                    evidence_reference=ids,
                    confidence=0.8,
                    needs_verification=[
                        "最终 QA 需要核对描述中所有 SKU 相关提醒是否与规格表一致。",
                        "缺少真实 review-backed VOC，描述中的顾虑处理基于 Strategy inference。",
                    ],
                ),
            ]
        )
        return specs

    def spec(
        self,
        *,
        module: str,
        submodule: str,
        sequence: int,
        length_rule_id: str,
        copy_task: str,
        english_seed: str,
        chinese_seed: str,
        strategy_reference: list[str],
        selling_point_reference: list[dict[str, Any]],
        evidence_reference: list[str],
        confidence: float,
        needs_verification: list[str],
        sku_scope: str = "ALL",
        copy_shape: str = "plain",
        primary_part: str = "english_copy",
        parameter_reference: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        evidence_ids = valid_evidence_ids(self.evidence_by_id, evidence_reference)
        params = parameter_reference if parameter_reference is not None else parameter_refs_from_evidence(self.evidence_by_id, evidence_ids)
        data = {
            "id": content_id(module, submodule, sequence),
            "module": module,
            "submodule": submodule,
            "sequence": sequence,
            "sku_scope": sku_scope,
            "sku_scope_detail": {"sku_ids": self.sku_ids if sku_scope == "ALL" else []},
            "copy_shape": copy_shape,
            "primary_part": primary_part,
            "length_rule_id": length_rule_id,
            "copy_task": copy_task,
            "english_seed": english_seed,
            "chinese_seed": chinese_seed,
            "strategy_reference": strategy_reference,
            "selling_point_reference": selling_point_reference,
            "parameter_reference": params,
            "evidence_reference": evidence_ids,
            "confidence": round(max(0.0, min(0.95, confidence)), 2),
            "needs_verification": unique(needs_verification),
        }
        if extra:
            data.update(extra)
        return data

    def build_sku_size_variants(self) -> list[dict[str, Any]]:
        variants = []
        for sku_id, value in self.size_values_by_sku.items():
            safe_value = clean_parameter_value(value)
            variants.append(
                {
                    "sku_id": sku_id,
                    "english_copy": f"Selected SKU Size - {safe_value}",
                    "chinese_copy": f"所选 SKU 尺寸 - {value}",
                    "character_count": len(f"Selected SKU Size - {safe_value}"),
                    "parameter_reference": [{"parameter": "单品尺寸", "sku_id": sku_id, "value": value}],
                }
            )
        return variants

    def build_openai_generation_payload(self, mapping: dict[str, Any], item_specs: list[dict[str, Any]]) -> dict[str, Any]:
        brand = find_brand_name(self.planning_context)
        brand_source = find_brand_source(self.planning_context)
        return {
            "task": "Task 5.1 OpenAI Copy Generation",
            "fixed_market": "Amazon US",
            "generation_boundary": "Strategy decides what to say; OpenAI only decides wording.",
            "brand_context": {
                "brand": brand,
                "brand_status": brand_source,
                "instruction": "Use this brand in Item Name. If brand_status is default_config, it is the configured demo default and must not be replaced with Brand.",
            },
            "planning_context_used": {
                "english_core_keyword": self.keyword,
                "sku_ids": self.sku_ids,
                "product_overview": self.project.user_input.overview,
                "allowed_product_facts": build_allowed_product_facts(self.planning_context),
            },
            "research_result_used": {
                "keyword_context": self.research_result.keyword_context,
                "data_quality": self.research_result.data_quality,
            },
            "strategy_result_used": {
                "strategy_version": self.strategy_result.strategy_version,
                "user_purchase_reason": self.strategy_result.user_purchase_reason,
                "communication_strategy": self.strategy_result.communication_strategy,
                "competitive_strategy": self.strategy_result.competitive_strategy,
                "selling_point_ranking": self.strategy_result.selling_point_ranking,
                "needs_verification": self.strategy_result.needs_verification,
            },
            "content_mapping": mapping,
            "length_rules": {
                rule_id: {
                    "character_min": rule.min_chars,
                    "character_max": rule.max_chars,
                }
                for rule_id, rule in LENGTH_RULES.items()
            },
            "content_item_specs": [compact_spec_for_openai(item) for item in item_specs],
            "output_schema": {
                "items": [
                    {
                        "id": "same id as content_item_specs",
                        "english_copy": "required for plain items",
                        "chinese_copy": "same meaning as final English",
                        "copy_parts": "required for supporting_image and premium_aplus shapes",
                        "notes": "optional",
                    }
                ]
            },
        }

    def merge_openai_items(self, item_specs: list[dict[str, Any]], openai_payload: dict[str, Any]) -> list[dict[str, Any]]:
        generated_items = openai_payload.get("items", [])
        by_id = {str(item.get("id")): item for item in generated_items if isinstance(item, dict) and item.get("id")}
        items = []
        for spec in item_specs:
            generated = by_id.get(spec["id"], {})
            item = materialize_item_from_spec(spec, generated)
            item["openai_generated"] = bool(generated)
            item["openai_generation_notes"] = generated.get("notes", "") if isinstance(generated, dict) else ""
            if not generated:
                item["needs_verification"] = unique(
                    list(item.get("needs_verification") or []) + ["OpenAI 未返回该内容项，当前使用 seed 文案占位。"]
                )
            item = apply_project_fact_guardrails(item, self.planning_context)
            item = attach_length_metadata(item)
            items.append(item)
        return items

    def enforce_length_rules(
        self,
        items: list[dict[str, Any]],
        generator: OpenAICopyGenerator,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        finalized = []
        repair_context = {
            "keyword": self.keyword,
            "allowed_product_facts": build_allowed_product_facts(self.planning_context),
            "content_boundary": "Do not change strategy, facts, SKU scope, or evidence references.",
        }
        for item in items:
            candidates = [item]
            history = []
            current = item
            for round_index in range(1, MAX_LENGTH_REWRITE_ROUNDS + 1):
                failures = failed_length_checks(current)
                if not failures:
                    break
                before = {
                    "round": round_index,
                    "failed_length_checks": failures,
                    "english_copy_before": current.get("english_copy", ""),
                }
                repaired = generator.repair_length(current, failures, repair_context, round_index)
                if not repaired:
                    before["repair_error"] = "OpenAI did not return a parseable repair payload."
                    history.append(before)
                    break
                current = merge_repair_payload(current, repaired)
                current = apply_project_fact_guardrails(current, self.planning_context)
                current = attach_length_metadata(current)
                before["length_checks_after"] = current.get("length_checks", {})
                before["english_copy_after"] = current.get("english_copy", "")
                history.append(before)
                candidates.append(current)

            best = best_length_candidate(candidates) if history else current
            best["rewrite_rounds"] = len(history)
            best["length_rewrite_history"] = history
            best["length_best_effort"] = not item_length_ok(best)
            if best["length_best_effort"]:
                best["needs_verification"] = unique(
                    list(best.get("needs_verification") or [])
                    + ["长度规则经过 3 轮模型修写后仍未完全合规，需 Task 6 QA 人工处理。"]
                )
            finalized.append(best)
        return finalized, summarize_length_rewrites(finalized)

    def generate_title_items(self, mapping: dict[str, Any]) -> list[dict[str, Any]]:
        title = (
            "Wooden Greenhouse with Sloped Roof, Walk-In Outdoor Garden Greenhouse "
            "with Multi-Layer PC Panels, Shelves, Hooks and Slat Panels"
        )
        chinese = "斜顶木质温室，户外可进入式花园温室，配多层 PC 板、棚架、挂钩和挂板"
        top_refs = self.top_points[:5]
        title_ids = unique(flatten([point.get("evidence_ids", []) for point in top_refs[:5]]))
        return [
            self.item(
                module="Title",
                submodule="Title",
                sequence=1,
                english_copy=title,
                chinese_copy=chinese,
                strategy_reference=["product_category", "communication_strategy", "selling_point_ranking"],
                selling_point_reference=rank_refs(top_refs[:5]),
                evidence_reference=title_ids,
                confidence=min_confidence(top_refs[:5], 0.72),
                needs_verification=collect_point_needs(top_refs[:5], max_items=2),
            ),
            self.item(
                module="Title Reference",
                submodule="Title Reference",
                sequence=1,
                english_copy=(
                    "Keyword route: wooden greenhouse, walk-in greenhouse, polycarbonate greenhouse, "
                    "outdoor garden greenhouse. Keep exact SKU dimensions outside the shared title."
                ),
                chinese_copy="标题关键词路径：wooden greenhouse、walk-in greenhouse、polycarbonate greenhouse、outdoor garden greenhouse。共享标题不写入单 SKU 尺寸。",
                strategy_reference=["keyword_context", "content_mapping"],
                selling_point_reference=rank_refs(top_refs[:5]),
                evidence_reference=unique(["user-input-keyword"] + title_ids),
                confidence=0.78,
                needs_verification=["关键词搜索量缺失，当前标题词序基于 ResearchResult 的关键词分层和 StrategyResult 判断。"],
            ),
        ]

    def generate_supporting_image_items(self, mapping: dict[str, Any]) -> list[dict[str, Any]]:
        p1 = point_by_rank(self.top_points, 1)
        p2 = point_by_rank(self.top_points, 2)
        p3 = point_by_rank(self.top_points, 3)
        p4 = point_by_rank(self.top_points, 4)
        p5 = point_by_rank(self.top_points, 5)
        purchase_reason_ids = (self.strategy_result.user_purchase_reason or {}).get("evidence_ids", [])
        items = [
            self.item(
                module="Supporting Image Copy",
                submodule="F1",
                sequence=1,
                english_copy="A Walk-In Garden Workspace - Grow, organize, and care for plants in one backyard greenhouse.",
                chinese_copy="可进入的花园工作空间 - 在一个后院温室里完成种植、收纳与日常照料。",
                strategy_reference=["user_purchase_reason", "communication_strategy"],
                selling_point_reference=rank_refs([p1, p2, p3]),
                evidence_reference=purchase_reason_ids,
                confidence=0.82,
                needs_verification=shared_verification(self.needs_verification),
            ),
            self.item_from_point(
                point=p1,
                module="Supporting Image Copy",
                submodule="F2",
                sequence=2,
                english_copy="Built-In Storage for Daily Care - Shelves, hooks, and slat panels help keep plants and tools organized.",
                chinese_copy="内置收纳，方便日常打理 - 棚架、挂钩和挂板帮助整理植物与园艺工具。",
            ),
            self.item_from_point(
                point=p2,
                module="Supporting Image Copy",
                submodule="F3",
                sequence=3,
                english_copy="Step Inside to Tend Your Plants - Walk-in space gives you room to arrange pots and handle routine care.",
                chinese_copy="走进温室照料植物 - 可进入式空间方便摆放花盆并完成日常养护。",
            ),
            self.item_from_point(
                point=p3,
                module="Supporting Image Copy",
                submodule="F4",
                sequence=4,
                english_copy="Natural Wood Look for the Backyard - A wood frame brings a warmer garden presence than a basic utility shelter.",
                chinese_copy="更适合后院的自然木质感 - 木质框架让温室更像庭院设施，而不只是工具棚。",
            ),
            self.item_from_point(
                point=p4,
                module="Supporting Image Copy",
                submodule="F5",
                sequence=5,
                english_copy="Multi-Layer PC Panel Coverage - Helps create a covered growing area for seasonal plant care.",
                chinese_copy="多层 PC 板覆盖 - 帮助形成适合季节性植物照料的覆盖种植空间。",
            ),
            self.item_from_point(
                point=p5,
                module="Supporting Image Copy",
                submodule="F6",
                sequence=6,
                english_copy="Sloped Roof Garden Profile - A clean roof line gives the greenhouse a more finished backyard look.",
                chinese_copy="斜顶花园造型 - 清晰屋顶线条让温室在后院摆放更完整。",
            ),
            self.generate_f7_size_item(),
            self.item(
                module="Supporting Image Copy",
                submodule="F8",
                sequence=8,
                english_copy="Know Your Setup Before You Build - Confirm footprint, selected SKU size, and installation needs before assembly.",
                chinese_copy="搭建前先确认关键信息 - 下单和组装前确认占地、所选 SKU 尺寸与安装需求。",
                strategy_reference=["purchase_barriers", "needs_verification"],
                selling_point_reference=rank_refs([p2]),
                evidence_reference=unique((p2 or {}).get("evidence_ids", []) + ["strategy-spec-fact-internal-space"]),
                confidence=0.78,
                needs_verification=[
                    "尺寸口径、内部空间、带棚架/不带屋檐等信息需在 Task 6 做参数核对。",
                    "安装稳定性与具体安装条件需补充证明。",
                ],
            ),
        ]
        return [item for item in items if item]

    def generate_f7_size_item(self) -> dict[str, Any]:
        variants = []
        for sku_id, value in self.size_values_by_sku.items():
            safe_value = clean_parameter_value(value)
            variants.append(
                {
                    "sku_id": sku_id,
                    "english_copy": f"Selected SKU Size - {safe_value}",
                    "chinese_copy": f"所选 SKU 尺寸 - {value}",
                    "parameter_reference": [{"parameter": "单品尺寸", "sku_id": sku_id, "value": value}],
                }
            )
        parameter_reference = [
            {"parameter": "单品尺寸", "scope": "sku_variable", "sku_ids": list(self.size_values_by_sku.keys())}
        ] if variants else []
        needs = [] if variants else ["未找到可用于 F7 的 SKU 尺寸参数。"]
        return self.item(
            module="Supporting Image Copy",
            submodule="F7",
            sequence=7,
            sku_scope="SKU_SPECIFIC" if variants else "ALL",
            english_copy="Choose the Size That Fits Your Garden - Use the selected SKU size copy for the exact version.",
            chinese_copy="选择适合花园的尺寸 - 具体尺寸按所选 SKU 使用对应文案。",
            strategy_reference=["focus_points", "purchase_barriers"],
            selling_point_reference=rank_refs([point_by_rank(self.top_points, 2)]),
            parameter_reference=parameter_reference,
            evidence_reference=["strategy-spec-fact-internal-space"],
            confidence=0.82 if variants else 0.45,
            needs_verification=needs + ["Task 6 需要核对每个 SKU 的尺寸字段是否可直接用于最终页面。"],
            extra={
                "sku_scope_detail": {
                    "sku_ids": list(self.size_values_by_sku.keys()) if variants else self.sku_ids,
                    "reason": "F7 包含 SKU 独有尺寸，因此生成 sku_copy_variants。",
                },
                "sku_copy_variants": variants,
            },
        )

    def generate_premium_aplus_items(self, mapping: dict[str, Any]) -> list[dict[str, Any]]:
        p1 = point_by_rank(self.top_points, 1)
        p2 = point_by_rank(self.top_points, 2)
        p3 = point_by_rank(self.top_points, 3)
        p4 = point_by_rank(self.top_points, 4)
        p5 = point_by_rank(self.top_points, 5)
        return [
            self.item(
                module="Premium A+",
                submodule="A+ Module 1",
                sequence=1,
                english_copy="More Than a Plant Shelter - Create a walk-in backyard space for growing, organizing, and everyday plant care.",
                chinese_copy="不只是植物遮护 - 打造可进入的后院空间，用于种植、收纳和日常养护。",
                strategy_reference=["user_purchase_reason", "communication_strategy"],
                selling_point_reference=rank_refs([p1, p2, p3]),
                evidence_reference=(self.strategy_result.user_purchase_reason or {}).get("evidence_ids", []),
                confidence=0.82,
                needs_verification=shared_verification(self.needs_verification),
            ),
            self.item_from_point(
                point=p1,
                module="Premium A+",
                submodule="A+ Module 2",
                sequence=2,
                english_copy="Organized from the Start - Built-in shelves, hooks, and slat hanging panels help give plants, pots, and tools their own place.",
                chinese_copy="从一开始就更好整理 - 内置棚架、挂钩和挂板，让植物、花盆和工具各有位置。",
            ),
            self.item_from_point(
                point=p2,
                module="Premium A+",
                submodule="A+ Module 3",
                sequence=3,
                english_copy="Room to Step In and Work - The walk-in format supports daily watering, arranging, checking, and plant care routines.",
                chinese_copy="可进入、可操作 - walk-in 结构支持浇水、摆放、检查和日常植物照料。",
            ),
            self.item(
                module="Premium A+",
                submodule="A+ Module 4",
                sequence=4,
                english_copy="Wood Frame + PC Panels - A natural wood frame and multi-layer PC panels balance backyard style with greenhouse function.",
                chinese_copy="木质框架 + PC 板 - 自然木质框架与多层 PC 板，兼顾庭院观感与温室功能。",
                strategy_reference=["selling_point_ranking", "communication_strategy"],
                selling_point_reference=rank_refs([p3, p4]),
                evidence_reference=unique((p3 or {}).get("evidence_ids", []) + (p4 or {}).get("evidence_ids", [])),
                confidence=min_confidence([p3, p4], 0.76),
                needs_verification=collect_point_needs([p3, p4], max_items=3),
            ),
            self.item_from_point(
                point=p5,
                module="Premium A+",
                submodule="A+ Module 5",
                sequence=5,
                english_copy="A Cleaner Backyard Profile - The sloped roof shape gives the greenhouse a more architectural garden look.",
                chinese_copy="更清爽的后院轮廓 - 斜顶造型让温室更接近庭院建筑感。",
            ),
            self.item(
                module="Premium A+",
                submodule="A+ Module 6",
                sequence=6,
                english_copy="Plan by SKU Before Setup - Review the selected size, usable space, and shelf layout before assembly.",
                chinese_copy="按 SKU 规划后再搭建 - 组装前确认所选尺寸、可用空间和棚架布局。",
                strategy_reference=["purchase_barriers", "needs_verification"],
                selling_point_reference=rank_refs([p2]),
                parameter_reference=[{"parameter": "单品尺寸", "scope": "sku_variable", "sku_ids": list(self.size_values_by_sku.keys())}],
                evidence_reference=unique((p2 or {}).get("evidence_ids", []) + ["strategy-spec-fact-internal-space"]),
                confidence=0.8,
                needs_verification=["Task 6 需要按 SKU 核对尺寸、内部空间和棚架相关参数。"],
            ),
        ]

    def generate_bullet_items(self, mapping: dict[str, Any]) -> list[dict[str, Any]]:
        bullets = []
        templates = {
            1: (
                "Organized Greenhouse Workspace: Built-in shelves, hooks, and slat hanging panels help keep plants, pots, and garden tools arranged inside one walk-in greenhouse.",
                "有序的温室工作空间：内置棚架、挂钩和挂板，帮助把植物、花盆和园艺工具集中整理在一个可进入式温室中。",
            ),
            2: (
                "Walk-In Plant Care Space: The walk-in design gives you room to step inside, arrange potted plants, and handle daily greenhouse care with less cramped movement.",
                "可进入式植物照料空间：walk-in 设计方便进入、摆放盆栽并完成日常温室打理，减少局促感。",
            ),
            3: (
                "Natural Wood Frame for Backyard Placement: The wood frame gives the greenhouse a warmer garden look for patios, backyards, and outdoor growing areas.",
                "适合后院摆放的自然木质框架：木质框架带来更温和的庭院观感，适合露台、后院和户外种植区。",
            ),
            4: (
                "Multi-Layer PC Panel Coverage: PC panels help create a covered growing area for seasonal plant care while keeping the structure more finished than soft cover shelters.",
                "多层 PC 板覆盖：PC 板帮助形成覆盖式种植空间，适合季节性植物照料，也比软膜棚更有成型结构感。",
            ),
            5: (
                "Sloped Roof with Setup Awareness: The sloped roof profile adds a clean garden-house look; confirm your selected SKU size and installation needs before setup.",
                "斜顶造型并重视搭建确认：斜顶轮廓带来清爽的花园屋感；搭建前请确认所选 SKU 尺寸和安装需求。",
            ),
        }
        for rank in range(1, 6):
            point = point_by_rank(self.top_points, rank)
            english, chinese = templates[rank]
            bullets.append(
                self.item_from_point(
                    point=point,
                    module="Bullet Point",
                    submodule=f"BP{rank}",
                    sequence=rank,
                    english_copy=english,
                    chinese_copy=chinese,
                    fallback_confidence=0.68,
                )
            )
        return bullets

    def generate_description_items(self, mapping: dict[str, Any]) -> list[dict[str, Any]]:
        ids = unique((self.strategy_result.user_purchase_reason or {}).get("evidence_ids", []) + collect_strategy_ids(self.top_points[:5]))
        transition_en = "From plant shelter to garden workspace, this wooden greenhouse is planned around how home gardeners actually use a backyard growing space."
        transition_cn = "从植物遮护到花园工作空间，这款木质温室围绕家庭园艺用户真实使用后院种植空间的方式展开。"
        description_en = (
            "Give your plants a more organized place to grow and give yourself a more practical space to care for them. "
            "This wooden greenhouse combines a walk-in format, multi-layer PC panels, built-in shelves, hooks, and slat hanging panels so planting, arranging, and tool storage can happen in one backyard structure.\n\n"
            "The natural wood frame helps the greenhouse feel more at home in a patio, backyard, or garden setting. "
            "Instead of treating the greenhouse as a temporary cover, the content direction should present it as a daily garden workspace for users who care about both function and outdoor appearance.\n\n"
            "Use the shelves and hanging areas to organize potted plants, small supplies, and frequently used garden tools. "
            "The walk-in space supports routine plant checks, watering, and seasonal care without turning every task into a separate setup process.\n\n"
            "Before purchase and assembly, review the selected SKU size, footprint, usable interior space, and installation requirements. "
            "Exact dimensions vary by SKU, so final listing content should keep SKU-specific measurements tied to the selected version."
        )
        description_cn = (
            "为植物提供更有序的生长空间，也为自己提供更实用的日常照料空间。这款木质温室结合 walk-in 可进入式结构、多层 PC 板、内置棚架、挂钩和挂板，让种植、摆放和工具收纳集中在一个后院结构中完成。\n\n"
            "自然木质框架让温室更容易融入露台、后院或花园环境。内容表达不应把它当作临时遮盖棚，而应呈现为兼顾功能与户外观感的日常园艺工作空间。\n\n"
            "用户可以利用棚架和挂放区域整理盆栽、小件用品和常用园艺工具。可进入式空间也支持日常查看植物、浇水和季节性养护，减少每次打理都重新布置的麻烦。\n\n"
            "购买和组装前，应确认所选 SKU 的尺寸、占地、内部可用空间与安装要求。不同 SKU 的精确尺寸不同，最终 Listing 中的具体测量值必须绑定到对应 SKU。"
        )
        return [
            self.item(
                module="Product Description",
                submodule="Transition",
                sequence=1,
                english_copy=transition_en,
                chinese_copy=transition_cn,
                strategy_reference=["communication_strategy", "user_mindset"],
                selling_point_reference=rank_refs(self.top_points[:3]),
                evidence_reference=ids,
                confidence=0.82,
                needs_verification=shared_verification(self.needs_verification),
            ),
            self.item(
                module="Product Description",
                submodule="Product Description",
                sequence=2,
                english_copy=description_en,
                chinese_copy=description_cn,
                strategy_reference=["user_purchase_reason", "communication_strategy", "purchase_barriers"],
                selling_point_reference=rank_refs(self.top_points[:5]),
                parameter_reference=[
                    {"parameter": "单品尺寸", "scope": "sku_variable", "usage": "提醒按所选 SKU 核对，不在共享描述中写入具体值。"}
                ],
                evidence_reference=ids,
                confidence=0.8,
                needs_verification=[
                    "最终 QA 需要核对描述中所有 SKU 相关提醒是否与规格表一致。",
                    "缺少真实 review-backed VOC，描述中的顾虑处理基于 Strategy inference。",
                ],
            ),
        ]

    def item_from_point(
        self,
        *,
        point: dict[str, Any] | None,
        module: str,
        submodule: str,
        sequence: int,
        english_copy: str,
        chinese_copy: str,
        fallback_confidence: float = 0.7,
    ) -> dict[str, Any]:
        point = point or {}
        return self.item(
            module=module,
            submodule=submodule,
            sequence=sequence,
            english_copy=english_copy,
            chinese_copy=chinese_copy,
            strategy_reference=["selling_point_ranking", "communication_strategy"],
            selling_point_reference=rank_refs([point]),
            parameter_reference=parameter_refs_from_evidence(self.evidence_by_id, point.get("evidence_ids", [])),
            evidence_reference=point.get("evidence_ids", []),
            confidence=float(point.get("confidence") or fallback_confidence),
            needs_verification=list(point.get("needs_verification") or []),
        )

    def item(
        self,
        *,
        module: str,
        submodule: str,
        sequence: int,
        english_copy: str,
        chinese_copy: str,
        strategy_reference: list[str],
        selling_point_reference: list[dict[str, Any]],
        evidence_reference: list[str],
        confidence: float,
        needs_verification: list[str],
        sku_scope: str | None = None,
        parameter_reference: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        evidence_ids = valid_evidence_ids(self.evidence_by_id, evidence_reference)
        params = parameter_reference if parameter_reference is not None else parameter_refs_from_evidence(self.evidence_by_id, evidence_ids)
        scope = sku_scope or "ALL"
        data = {
            "id": content_id(module, submodule, sequence),
            "module": module,
            "submodule": submodule,
            "sequence": sequence,
            "sku_scope": scope,
            "sku_scope_detail": {"sku_ids": self.sku_ids if scope == "ALL" else []},
            "english_copy": safe_copy(english_copy),
            "chinese_copy": chinese_copy.strip(),
            "strategy_reference": strategy_reference,
            "selling_point_reference": selling_point_reference,
            "parameter_reference": params,
            "evidence_reference": evidence_ids,
            "confidence": round(max(0.0, min(0.95, confidence)), 2),
            "needs_verification": unique(needs_verification),
        }
        if extra:
            data.update(extra)
        return data

    def input_snapshot(self) -> dict[str, Any]:
        return {
            "planning_context_used": {
                "status": self.planning_context.status,
                "english_core_keyword": self.keyword,
                "sku_count": len(self.sku_ids),
                "size_values_by_sku_count": len(self.size_values_by_sku),
                "source_policy": self.planning_context.source_policy,
            },
            "research_result_used": {
                "status": self.research_result.status,
                "provider": self.research_result.research_provider,
                "keyword_context": self.research_result.keyword_context,
                "data_quality": self.research_result.data_quality,
            },
            "strategy_result_used": {
                "status": self.strategy_result.status,
                "strategy_version": self.strategy_result.strategy_version,
                "user_purchase_reason": self.strategy_result.user_purchase_reason,
                "communication_strategy": self.strategy_result.communication_strategy,
                "selling_point_ranking": self.strategy_result.selling_point_ranking,
                "needs_verification": self.strategy_result.needs_verification,
            },
        }


def compact_spec_for_openai(spec: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "id",
        "module",
        "submodule",
        "sequence",
        "sku_scope",
        "copy_shape",
        "primary_part",
        "length_rule_id",
        "copy_task",
        "english_seed",
        "chinese_seed",
        "copy_parts",
        "selling_point_reference",
        "parameter_reference",
        "needs_verification",
        "brand_status",
        "brand",
    ]
    return {key: spec.get(key) for key in keys if key in spec}


def materialize_item_from_spec(spec: dict[str, Any], generated: dict[str, Any]) -> dict[str, Any]:
    item = {
        key: value
        for key, value in spec.items()
        if key
        not in {
            "copy_task",
            "english_seed",
            "chinese_seed",
        }
    }
    shape = spec.get("copy_shape", "plain")
    if shape in {"supporting_image", "premium_aplus"}:
        item["copy_parts"] = materialize_copy_parts(spec, generated)
    else:
        item["english_copy"] = str(generated.get("english_copy") or spec.get("english_seed") or "").strip()
        item["chinese_copy"] = str(generated.get("chinese_copy") or spec.get("chinese_seed") or "").strip()
    return item


def materialize_copy_parts(spec: dict[str, Any], generated: dict[str, Any]) -> dict[str, Any]:
    expected_parts = spec.get("copy_parts") or {}
    generated_parts = generated.get("copy_parts") if isinstance(generated, dict) else {}
    if not isinstance(generated_parts, dict):
        generated_parts = {}
    output = {}
    for part_name, part_spec in expected_parts.items():
        generated_part = generated_parts.get(part_name, {}) if isinstance(generated_parts.get(part_name), dict) else {}
        english_key = f"english_{part_name}"
        chinese_key = f"chinese_{part_name}"
        output[part_name] = {
            "length_rule_id": part_spec.get("length_rule_id", "unbounded"),
            "english_copy": str(
                generated_part.get("english_copy")
                or generated.get(english_key, "")
                or part_spec.get("english_copy", "")
            ).strip(),
            "chinese_copy": str(
                generated_part.get("chinese_copy")
                or generated.get(chinese_key, "")
                or part_spec.get("chinese_copy", "")
            ).strip(),
        }
    return output


def merge_repair_payload(item: dict[str, Any], repaired: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(item, ensure_ascii=False))
    if isinstance(merged.get("copy_parts"), dict):
        repaired_parts = repaired.get("copy_parts")
        if isinstance(repaired_parts, dict):
            for name, value in repaired_parts.items():
                if not isinstance(value, dict):
                    continue
                if name not in merged["copy_parts"]:
                    continue
                if value.get("english_copy"):
                    merged["copy_parts"][name]["english_copy"] = value["english_copy"]
                if value.get("chinese_copy"):
                    merged["copy_parts"][name]["chinese_copy"] = value["chinese_copy"]
        elif repaired.get("english_copy"):
            primary = merged.get("primary_part") or next(iter(merged["copy_parts"].keys()), "")
            if primary in merged["copy_parts"]:
                merged["copy_parts"][primary]["english_copy"] = repaired.get("english_copy", "")
                merged["copy_parts"][primary]["chinese_copy"] = repaired.get("chinese_copy", merged["copy_parts"][primary].get("chinese_copy", ""))
    else:
        if repaired.get("english_copy"):
            merged["english_copy"] = repaired["english_copy"]
        if repaired.get("chinese_copy"):
            merged["chinese_copy"] = repaired["chinese_copy"]
    if repaired.get("notes"):
        merged["openai_repair_notes"] = repaired["notes"]
    return merged


def apply_project_fact_guardrails(item: dict[str, Any], planning_context: Any) -> dict[str, Any]:
    guarded = json.loads(json.dumps(item, ensure_ascii=False))
    fact_text = json.dumps(build_allowed_product_facts(planning_context), ensure_ascii=False).lower()
    if "杉木" in fact_text and "cedar" not in fact_text:
        replace_cedar_terms(guarded)
    return guarded


def replace_cedar_terms(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in list(value.items()):
            if isinstance(nested, str):
                value[key] = replace_cedar_text(nested)
            else:
                replace_cedar_terms(nested)
    elif isinstance(value, list):
        for nested in value:
            replace_cedar_terms(nested)


def replace_cedar_text(text: str) -> str:
    text = re.sub(r"\bNatural Cedar Frame\b", "Natural Fir Wood Frame", text, flags=re.IGNORECASE)
    text = re.sub(r"\bcedar wood\b", "fir wood", text, flags=re.IGNORECASE)
    text = re.sub(r"\bcedar frame\b", "fir wood frame", text, flags=re.IGNORECASE)
    text = re.sub(r"\bcedar\b", "fir wood", text, flags=re.IGNORECASE)
    text = text.replace("冷杉木", "杉木")
    return text


def summarize_length_rewrites(items: list[dict[str, Any]]) -> dict[str, Any]:
    rewritten = [item for item in items if int(item.get("rewrite_rounds") or 0) > 0]
    unresolved = [item for item in items if item.get("length_best_effort")]
    return {
        "max_rounds": MAX_LENGTH_REWRITE_ROUNDS,
        "rewritten_count": len(rewritten),
        "rewritten_items": [
            {
                "id": item.get("id"),
                "module": item.get("module"),
                "submodule": item.get("submodule"),
                "rewrite_rounds": item.get("rewrite_rounds"),
                "length_checks": item.get("length_checks"),
            }
            for item in rewritten
        ],
        "unresolved_count": len(unresolved),
        "unresolved_items": [
            {
                "id": item.get("id"),
                "module": item.get("module"),
                "submodule": item.get("submodule"),
                "length_checks": item.get("length_checks"),
            }
            for item in unresolved
        ],
    }


def find_brand_name(planning_context: Any) -> str:
    verified = find_verified_brand_name(planning_context)
    return verified or DEFAULT_BRAND_NAME


def find_brand_source(planning_context: Any) -> str:
    return "spec_fact" if find_verified_brand_name(planning_context) else "default_config"


def find_verified_brand_name(planning_context: Any) -> str:
    candidates = []
    for key, value in (planning_context.product_common_parameters or {}).items():
        if re.search(r"品牌|brand", str(key), re.IGNORECASE) and str(value).strip():
            candidates.append(str(value).strip())
    for row in planning_context.standard_chinese_parameter_table or []:
        name = str(getattr(row, "parameter", "") or "")
        if not re.search(r"品牌|brand", name, re.IGNORECASE):
            continue
        values = [str(value).strip() for value in getattr(row, "values", []) if str(value).strip()]
        unique_values = unique(values)
        if len(unique_values) == 1:
            candidates.append(unique_values[0])
    for candidate in candidates:
        if candidate and candidate not in {"是", "否", "无", "/", "N/A", "n/a"}:
            return candidate
    return ""


def build_allowed_product_facts(planning_context: Any) -> list[dict[str, Any]]:
    wanted = [
        "叶子类目",
        "英文品名",
        "类型",
        "款式",
        "颜色",
        "材质",
        "PC空心板",
        "油漆",
        "外观件主要材质",
        "内部空间",
        "单品尺寸",
        "带棚架尺寸",
        "不带屋檐尺寸",
    ]
    facts = []
    rows = {str(getattr(row, "parameter", "")): row for row in planning_context.standard_chinese_parameter_table or []}
    for name in wanted:
        row = rows.get(name)
        if not row:
            continue
        values = [str(value).strip() for value in getattr(row, "values", []) if str(value).strip()]
        if not values:
            continue
        unique_values = unique(values)
        if len(unique_values) == 1:
            facts.append({"parameter": name, "scope": "ALL", "value": unique_values[0]})
        else:
            values_by_sku = getattr(row, "values_by_sku", None) or {}
            facts.append(
                {
                    "parameter": name,
                    "scope": "SKU_SPECIFIC",
                    "values_by_sku": {str(key): str(value) for key, value in values_by_sku.items() if str(value).strip()},
                }
            )
    for parameter in planning_context.research_brief.core_product_parameters or []:
        data = parameter.model_dump(mode="json") if hasattr(parameter, "model_dump") else parameter.dict()
        if not any(item.get("parameter") == data.get("name") for item in facts):
            facts.append(
                {
                    "parameter": data.get("name", ""),
                    "scope": data.get("scope", ""),
                    "value": data.get("value", ""),
                    "values_by_sku": data.get("values_by_sku", {}),
                }
            )
    return facts


def usable_selling_points(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for point in points or []:
        if not isinstance(point, dict):
            continue
        if "待补充" in str(point.get("selling_point") or ""):
            continue
        if float_or_default(point.get("confidence"), 0) <= 0:
            continue
        result.append(point)
    return sorted(result, key=lambda item: int(item.get("rank") or 999))


def point_by_rank(points: list[dict[str, Any]], rank: int) -> dict[str, Any] | None:
    for point in points:
        if int(point.get("rank") or 0) == rank:
            return point
    return None


def rank_refs(points: list[dict[str, Any] | None]) -> list[dict[str, Any]]:
    refs = []
    for point in points:
        if not point:
            continue
        refs.append(
            {
                "rank": point.get("rank"),
                "selling_point": point.get("selling_point"),
                "confidence": point.get("confidence"),
            }
        )
    return refs


def collect_point_needs(points: list[dict[str, Any] | None], max_items: int = 4) -> list[str]:
    needs = []
    for point in points:
        if not point:
            continue
        needs.extend(point.get("needs_verification") or [])
    return unique(needs)[:max_items]


def shared_verification(needs: list[dict[str, Any]], max_items: int = 2) -> list[str]:
    result = []
    for item in needs[:max_items]:
        if isinstance(item, dict):
            result.append(str(item.get("item") or item.get("impact") or ""))
        else:
            result.append(str(item))
    return [item for item in result if item]


def extract_keyword_terms(keyword_context: dict[str, Any], key: str) -> list[str]:
    terms = []
    for item in keyword_context.get(key, []) if isinstance(keyword_context, dict) else []:
        if isinstance(item, dict):
            value = item.get("term") or item.get("value")
        else:
            value = item
        if isinstance(value, str) and value.strip():
            terms.append(value.strip())
    return unique(terms)


def find_parameter_values_by_sku(planning_context: Any, parameter_names: list[str]) -> dict[str, str]:
    names = [name.lower() for name in parameter_names]
    sources = []
    sources.extend(planning_context.research_brief.core_product_parameters or [])
    sources.extend(planning_context.standard_chinese_parameter_table or [])
    for item in sources:
        name = str(getattr(item, "name", "") or getattr(item, "parameter", "") or "").lower()
        if not any(target in name for target in names):
            continue
        values = getattr(item, "values_by_sku", None) or {}
        if values:
            return {str(key): str(value) for key, value in values.items() if str(value).strip()}
    return {}


def clean_parameter_value(value: str) -> str:
    text = value.strip()
    text = re.sub(r"^长宽高[:：]\s*", "Size: ", text)
    text = re.sub(r"^实测[:：]\s*", "Measured: ", text)
    return text


def valid_evidence_ids(evidence_by_id: dict[str, dict[str, Any]], evidence_ids: list[str]) -> list[str]:
    ids = []
    for item in evidence_ids or []:
        value = str(item)
        if value in evidence_by_id and value not in ids:
            ids.append(value)
    return ids


def parameter_refs_from_evidence(evidence_by_id: dict[str, dict[str, Any]], evidence_ids: list[str]) -> list[dict[str, Any]]:
    refs = []
    for evidence_id in evidence_ids or []:
        evidence = evidence_by_id.get(str(evidence_id), {})
        if not evidence:
            continue
        if str(evidence_id).startswith("spec-fact") or str(evidence_id).startswith("strategy-spec-fact"):
            refs.append(
                {
                    "evidence_id": evidence_id,
                    "source_type": evidence.get("source_type", "spec_fact"),
                    "source": evidence.get("source", ""),
                    "claim": evidence.get("claim") or evidence.get("value") or evidence.get("title") or "",
                }
            )
    return refs


def collect_strategy_ids(points: list[dict[str, Any]]) -> list[str]:
    return unique(flatten([point.get("evidence_ids", []) for point in points if point]))


def flatten(items: list[Any]) -> list[Any]:
    result = []
    for item in items:
        if isinstance(item, list):
            result.extend(item)
        elif item:
            result.append(item)
    return result


def unique(values: list[Any]) -> list[Any]:
    result = []
    seen = set()
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
        if not value or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def min_confidence(points: list[dict[str, Any] | None], default: float = 0.7) -> float:
    values = [float_or_default(point.get("confidence"), default) for point in points if point]
    if not values:
        return default
    return min(values)


def float_or_default(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_copy(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip()) if "\n" not in text else text.strip()
    replacements = {
        "best": "reliable",
        "perfect": "well-suited",
        "guaranteed": "designed",
        "waterproof": "covered",
        "weatherproof": "outdoor-ready",
        "never": "helps reduce",
        "always": "is designed to",
    }
    for bad, replacement in replacements.items():
        text = re.sub(rf"\b{bad}\b", replacement, text, flags=re.IGNORECASE)
    return text


def content_id(module: str, submodule: str, sequence: int) -> str:
    raw = f"{module}-{submodule}-{sequence}".lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return f"content-{slug}"


def group_modules(items: list[dict[str, Any]]) -> dict[str, Any]:
    modules = {
        "title": [],
        "title_reference": [],
        "ebay_title": [],
        "supporting_image_copy": [],
        "premium_aplus": [],
        "bullet_points": [],
        "product_description": [],
    }
    key_map = {
        "Title": "title",
        "Title Reference": "title_reference",
        "eBay Title": "ebay_title",
        "Supporting Image Copy": "supporting_image_copy",
        "Premium A+": "premium_aplus",
        "Bullet Point": "bullet_points",
        "Product Description": "product_description",
    }
    for item in items:
        modules.setdefault(key_map.get(item.get("module"), item.get("module", "other")), []).append(item)
    return modules


def run_minimal_checks(items: list[dict[str, Any]]) -> dict[str, Any]:
    missing_pairs = [
        item.get("id")
        for item in items
        if not item.get("english_copy") or not item.get("chinese_copy")
    ]
    missing_scope = [item.get("id") for item in items if not item.get("sku_scope")]
    length_failures = [
        {
            "id": item.get("id"),
            "module": item.get("module"),
            "submodule": item.get("submodule"),
            "failed_length_checks": failed_length_checks(item),
        }
        for item in items
        if failed_length_checks(item)
    ]
    high_risk_words = {}
    pattern = re.compile(r"\b(best|perfect|guaranteed|lifetime|100%|waterproof|weatherproof|never|always)\b", re.IGNORECASE)
    for item in items:
        matches = sorted(set(pattern.findall(item.get("english_copy", ""))))
        if matches:
            high_risk_words[item.get("id")] = matches
    return {
        "all_items_have_bilingual_copy": not missing_pairs,
        "all_items_have_sku_scope": not missing_scope,
        "all_length_rules_passed": not length_failures,
        "length_failures": length_failures,
        "high_risk_word_scan": high_risk_words,
        "note": "仅执行最低限度生成校验；完整禁用词、参数核对、中英文一致性和自动修正留给 Task 6。",
    }


def build_content_confidence(items: list[dict[str, Any]], strategy_result: Any, needs_verification: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float_or_default(item.get("confidence"), 0.5) for item in items]
    item_confidence = sum(values) / len(values) if values else 0.0
    strategy_confidence = float_or_default((strategy_result.confidence or {}).get("overall"), 0.6)
    missing_penalty = min(0.16, len(needs_verification) * 0.01)
    overall = max(0.1, min(0.92, item_confidence * 0.55 + strategy_confidence * 0.45 - missing_penalty))
    return {
        "overall": round(overall, 2),
        "average_item_confidence": round(item_confidence, 2),
        "strategy_confidence": round(strategy_confidence, 2),
        "needs_verification_count": len(needs_verification),
        "risk_notes": [
            "Content 只消费 StrategyResult，不重新决定定位、购买理由或卖点排序。",
            "含 SKU 独有尺寸的 F7 使用 SKU_SPECIFIC variants，未套用到 ALL。",
            "缺少真实评论或 ASIN 数据的判断仅作为安全表达和 needs_verification，不写成确定事实。",
        ],
    }


def build_legacy_drafts(modules: dict[str, Any]) -> dict[str, Any]:
    title_items = modules.get("title", [])
    description_items = modules.get("product_description", [])
    return {
        "title": first_english(title_items),
        "item_name": first_english([item for item in title_items if item.get("submodule") == "Item Name"]),
        "item_highlight": first_english(modules.get("title_reference", [])),
        "ebay_title": first_english(modules.get("ebay_title", [])),
        "title_reference": modules.get("title_reference", []),
        "supporting_image_copy": modules.get("supporting_image_copy", []),
        "premium_aplus": modules.get("premium_aplus", []),
        "bullet_points": modules.get("bullet_points", []),
        "product_description": first_english([item for item in description_items if item.get("submodule") == "Product Description"]),
        "transition": first_english([item for item in description_items if item.get("submodule") == "Transition"]),
    }


def first_english(items: list[dict[str, Any]]) -> str:
    for item in items:
        if item.get("english_copy"):
            return item["english_copy"]
    return ""
