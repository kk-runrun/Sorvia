# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from super_planner.core.naming import build_project_name
from super_planner.schemas.workflow import ConfirmationRequest, WorkflowState, create_workflow_state, utc_now


class UserInput(BaseModel):
    keywords: str
    overview: str = ""


class SpecFile(BaseModel):
    id: str
    filename: str
    content_type: str = ""
    size_bytes: int = 0
    storage_path: str
    upload_order: int = 0
    uploaded_at: datetime = Field(default_factory=utc_now)


class SkuInfo(BaseModel):
    sku_id: str
    sku: str
    name: str = ""
    sequence: int
    source_file_id: str = ""
    source_file_name: str = ""
    source_column_index: int = 0


class StandardParameterRow(BaseModel):
    parameter: str
    values: List[str] = Field(default_factory=list)
    values_by_sku: Dict[str, str] = Field(default_factory=dict)


class SkuGroupCommonParameters(BaseModel):
    group_id: str
    sku_ids: List[str] = Field(default_factory=list)
    sku_indexes: List[int] = Field(default_factory=list)
    parameters: Dict[str, str] = Field(default_factory=dict)


class ParsedSpecFileResult(BaseModel):
    spec_file_id: str
    filename: str
    upload_order: int = 0
    status: str = "not_started"
    category: str = ""
    sku_count: int = 0
    output_path: str = ""
    exception_count: int = 0
    sku_list: List[SkuInfo] = Field(default_factory=list)
    standard_chinese_parameter_table: List[StandardParameterRow] = Field(default_factory=list)
    product_common_parameters: Dict[str, str] = Field(default_factory=dict)
    sku_group_common_parameters: List[SkuGroupCommonParameters] = Field(default_factory=list)
    sku_differential_parameters: Dict[str, Dict[str, str]] = Field(default_factory=dict)
    messages: List[str] = Field(default_factory=list)


class ParsedSpecResult(BaseModel):
    status: str = "not_started"
    spec_file_ids: List[str] = Field(default_factory=list)
    sku_count: int = 0
    category: str = ""
    summary: str = ""
    parsed_files: List[ParsedSpecFileResult] = Field(default_factory=list)
    output_paths: List[str] = Field(default_factory=list)
    sku_list: List[SkuInfo] = Field(default_factory=list)
    standard_chinese_parameter_table: List[StandardParameterRow] = Field(default_factory=list)
    product_common_parameters: Dict[str, str] = Field(default_factory=dict)
    sku_group_common_parameters: List[SkuGroupCommonParameters] = Field(default_factory=list)
    sku_differential_parameters: Dict[str, Dict[str, str]] = Field(default_factory=dict)
    exception_count: int = 0
    fields: Dict[str, Any] = Field(default_factory=dict)


class ResearchCoreParameter(BaseModel):
    name: str
    value: str = ""
    scope: str = "product_common"
    sku_ids: List[str] = Field(default_factory=list)
    values_by_sku: Dict[str, str] = Field(default_factory=dict)
    source: str = "parsed_spec_result"


class ResearchBrief(BaseModel):
    raw_keywords: str = ""
    english_core_keyword: str = ""
    keyword_source: str = ""
    needs_english_keyword_generation: bool = False
    core_product_parameters: List[ResearchCoreParameter] = Field(default_factory=list)
    excluded_parameter_notes: List[str] = Field(default_factory=list)


class PlanningContext(BaseModel):
    status: str = "not_started"
    summary: str = ""
    user_input: Dict[str, Any] = Field(default_factory=dict)
    spec_result: Dict[str, Any] = Field(default_factory=dict)
    sku_list: List[SkuInfo] = Field(default_factory=list)
    standard_chinese_parameter_table: List[StandardParameterRow] = Field(default_factory=list)
    product_common_parameters: Dict[str, str] = Field(default_factory=dict)
    sku_group_common_parameters: List[SkuGroupCommonParameters] = Field(default_factory=list)
    sku_differential_parameters: Dict[str, Dict[str, str]] = Field(default_factory=dict)
    research_brief: ResearchBrief = Field(default_factory=ResearchBrief)
    source_policy: str = "原始规格书 → 规格书拆解 → 标准中文参数表；后续涉及具体参数时优先使用结构化规格书结果。"
    facts: Dict[str, Any] = Field(default_factory=dict)
    assumptions: List[str] = Field(default_factory=list)
    human_notes: Dict[str, str] = Field(default_factory=dict)


class ResearchResult(BaseModel):
    status: str = "not_started"
    summary: str = ""
    research_provider: str = ""
    provider_status: str = ""
    target_market: str = "Amazon US"
    web_search_enabled: bool = False
    web_search_used: bool = False
    generated_at: Optional[datetime] = None
    market_context: Dict[str, Any] = Field(default_factory=dict)
    keyword_context: Dict[str, Any] = Field(default_factory=dict)
    audience_demand_context: Dict[str, Any] = Field(default_factory=dict)
    competitor_context: Dict[str, Any] = Field(default_factory=dict)
    voc_context: Dict[str, Any] = Field(default_factory=dict)
    market_tier: Dict[str, Any] = Field(default_factory=dict)
    opportunity_signals: List[Dict[str, Any]] = Field(default_factory=list)
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    data_quality: Dict[str, Any] = Field(default_factory=dict)
    sources: List[str] = Field(default_factory=list)
    raw_payload: Dict[str, Any] = Field(default_factory=dict)


class StrategyResult(BaseModel):
    status: str = "not_started"
    summary: str = ""
    strategy_version: str = ""
    generated_at: Optional[datetime] = None
    product_category: Dict[str, Any] = Field(default_factory=dict)
    strategy_category: Dict[str, Any] = Field(default_factory=dict)
    user_mindset: Dict[str, Any] = Field(default_factory=dict)
    target_audience: List[Dict[str, Any]] = Field(default_factory=list)
    main_uses: List[Dict[str, Any]] = Field(default_factory=list)
    main_scenario: Dict[str, Any] = Field(default_factory=dict)
    secondary_scenario: List[Dict[str, Any]] = Field(default_factory=list)
    user_needs_summary: List[Dict[str, Any]] = Field(default_factory=list)
    focus_points: List[Dict[str, Any]] = Field(default_factory=list)
    pain_points: List[Dict[str, Any]] = Field(default_factory=list)
    purchase_barriers: List[Dict[str, Any]] = Field(default_factory=list)
    competitive_landscape: Dict[str, Any] = Field(default_factory=dict)
    competitive_opportunity: List[Dict[str, Any]] = Field(default_factory=list)
    competitive_strategy: Dict[str, Any] = Field(default_factory=dict)
    user_purchase_reason: Dict[str, Any] = Field(default_factory=dict)
    communication_strategy: Dict[str, Any] = Field(default_factory=dict)
    selling_point_ranking: List[Dict[str, Any]] = Field(default_factory=list)
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    confidence: Dict[str, Any] = Field(default_factory=dict)
    needs_verification: List[Dict[str, Any]] = Field(default_factory=list)
    pillars: List[str] = Field(default_factory=list)
    raw_payload: Dict[str, Any] = Field(default_factory=dict)


class ContentResult(BaseModel):
    status: str = "not_started"
    summary: str = ""
    content_version: str = ""
    generated_at: Optional[datetime] = None
    generation_provider: str = ""
    generation_model: str = ""
    generation_api: str = ""
    format_rules_version: str = ""
    content_mapping: Dict[str, Any] = Field(default_factory=dict)
    content_items: List[Dict[str, Any]] = Field(default_factory=list)
    modules: Dict[str, Any] = Field(default_factory=dict)
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    confidence: Dict[str, Any] = Field(default_factory=dict)
    needs_verification: List[Dict[str, Any]] = Field(default_factory=list)
    length_rewrite_summary: Dict[str, Any] = Field(default_factory=dict)
    minimal_checks: Dict[str, Any] = Field(default_factory=dict)
    drafts: Dict[str, Any] = Field(default_factory=dict)
    raw_payload: Dict[str, Any] = Field(default_factory=dict)


class QAResult(BaseModel):
    status: str = "not_started"
    summary: str = ""
    qa_version: str = ""
    generated_at: Optional[datetime] = None
    checked_items: int = 0
    passed_items: int = 0
    corrected_items: int = 0
    unresolved_issues: List[Dict[str, Any]] = Field(default_factory=list)
    correction_history: List[Dict[str, Any]] = Field(default_factory=list)
    parameter_issues: List[Dict[str, Any]] = Field(default_factory=list)
    forbidden_word_issues: List[Dict[str, Any]] = Field(default_factory=list)
    translation_issues: List[Dict[str, Any]] = Field(default_factory=list)
    format_issues: List[Dict[str, Any]] = Field(default_factory=list)
    strategy_issues: List[Dict[str, Any]] = Field(default_factory=list)
    overall_risk: str = "low"
    data_quality: Dict[str, Any] = Field(default_factory=dict)
    raw_payload: Dict[str, Any] = Field(default_factory=dict)
    issues: List[Dict[str, Any]] = Field(default_factory=list)


class ExportResult(BaseModel):
    status: str = "not_started"
    summary: str = ""
    export_version: str = ""
    generated_at: Optional[datetime] = None
    artifact_path: str = ""
    artifact_filename: str = ""
    artifact_type: str = ""
    template_path: str = ""
    template_used: bool = False
    sheet_names: List[str] = Field(default_factory=list)
    download_url: str = ""
    qa_unresolved_count: int = 0
    raw_payload: Dict[str, Any] = Field(default_factory=dict)


class PlanningProject(BaseModel):
    project_id: str
    project_name: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    user_input: UserInput
    spec_files: List[SpecFile] = Field(default_factory=list)
    parsed_spec_result: ParsedSpecResult = Field(default_factory=ParsedSpecResult)
    planning_context: PlanningContext = Field(default_factory=PlanningContext)
    research_result: ResearchResult = Field(default_factory=ResearchResult)
    strategy_result: StrategyResult = Field(default_factory=StrategyResult)
    content_result: ContentResult = Field(default_factory=ContentResult)
    qa_result: QAResult = Field(default_factory=QAResult)
    export_result: ExportResult = Field(default_factory=ExportResult)
    workflow_state: WorkflowState
    confirmation_history: List[ConfirmationRequest] = Field(default_factory=list)


def create_project(project_id: str, user_input: UserInput, spec_files: List[SpecFile]) -> PlanningProject:
    return PlanningProject(
        project_id=project_id,
        project_name=build_project_name(user_input.keywords),
        user_input=user_input,
        spec_files=spec_files,
        workflow_state=create_workflow_state(project_id),
    )
