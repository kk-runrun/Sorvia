# -*- coding: utf-8 -*-
from __future__ import annotations

import uuid
from typing import Dict, Optional

from super_planner.core.exceptions import WorkflowBusinessInterruption
from super_planner.schemas.project import ParsedSpecResult, PlanningProject
from super_planner.schemas.workflow import ConfirmationQuestion, ConfirmationRequest
from super_planner.services.planning_context_builder import (
    build_manual_planning_context,
    build_parsed_spec_file_result,
    build_planning_context,
    detect_overview_conflicts,
    merge_parsed_spec_results,
)
from super_planner.services.spec_sheet_adapter import WorkbenchSpecSheetAdapter


NO_SPEC_ANSWER_ID = "no_spec_continue"
NO_SKU_ANSWER_ID = "manual_sku_assignment"


def parse_product_context(project: PlanningProject, answers: Optional[Dict[str, str]] = None) -> None:
    answers = answers or {}

    if not project.spec_files:
        if not answers.get(NO_SPEC_ANSWER_ID, "").strip():
            raise WorkflowBusinessInterruption(
                ConfirmationRequest(
                    id=uuid.uuid4().hex,
                    step_id="product_context",
                    title="缺少规格书",
                    reason="Product Context 阶段需要规格书作为硬参数事实来源，当前未上传规格书。",
                    questions=[
                        ConfirmationQuestion(
                            id=NO_SPEC_ANSWER_ID,
                            prompt="请确认是否允许本次任务暂不使用规格书继续，并说明原因。",
                            background="如果继续，Planning Context 只会包含用户输入和你的人工说明；后续涉及具体参数时不得由模型猜测。",
                        )
                    ],
                )
            )

        project.parsed_spec_result = ParsedSpecResult(
            status="manual_without_spec",
            spec_file_ids=[],
            sku_count=0,
            summary="用户确认暂不使用规格书，未生成标准中文参数表。",
            fields={"source": "manual_confirmation"},
        )
        project.planning_context = build_manual_planning_context(project, answers)
        return

    if project.parsed_spec_result.status == "completed" and project.parsed_spec_result.standard_chinese_parameter_table:
        parsed_spec = project.parsed_spec_result
    else:
        ordered_files = sorted(project.spec_files, key=lambda item: item.upload_order or 0)
        primary_file = ordered_files[0]
        pipeline_result = WorkbenchSpecSheetAdapter().parse(project.project_id, primary_file)
        parsed_file = build_parsed_spec_file_result(pipeline_result, primary_file)
        parsed_spec = merge_parsed_spec_results(project, parsed_file)
        project.parsed_spec_result = parsed_spec

    if parsed_spec.sku_count <= 0:
        if not answers.get(NO_SKU_ANSWER_ID, "").strip():
            raise WorkflowBusinessInterruption(
                ConfirmationRequest(
                    id=uuid.uuid4().hex,
                    step_id="product_context",
                    title="无法识别 SKU",
                    reason="规格书已读取，但没有识别到可用于整理参数的 SKU 页或 SKU 序号。",
                    questions=[
                        ConfirmationQuestion(
                            id=NO_SKU_ANSWER_ID,
                            prompt="请说明该规格书是否为单 SKU，并提供 SKU 编码、中文名或参数归属说明。",
                            background="系统不会猜测参数属于哪个 SKU。若文件结构不符合当前规格书整理规则，请联系 IT 补充适配。",
                        )
                    ],
                )
            )

    project.planning_context = build_planning_context(project, parsed_spec, answers)
    conflict_questions = detect_overview_conflicts(
        overview=project.user_input.overview,
        rows=parsed_spec.standard_chinese_parameter_table,
        sku_list=parsed_spec.sku_list,
        resolved_answers=answers,
    )
    if conflict_questions:
        raise WorkflowBusinessInterruption(
            ConfirmationRequest(
                id=uuid.uuid4().hex,
                step_id="product_context",
                title="检测到产品信息冲突",
                reason="产品概述中的硬参数与规格书拆解结果不一致，不能静默覆盖规格书参数。",
                questions=conflict_questions,
            )
        )
