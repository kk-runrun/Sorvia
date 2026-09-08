# -*- coding: utf-8 -*-
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import re
from typing import Dict, Iterable, List, Sequence

from super_planner.schemas.project import (
    ParsedSpecFileResult,
    ParsedSpecResult,
    PlanningContext,
    PlanningProject,
    ResearchBrief,
    ResearchCoreParameter,
    SkuGroupCommonParameters,
    SkuInfo,
    SpecFile,
    StandardParameterRow,
)
from super_planner.schemas.workflow import ConfirmationQuestion


METADATA_ROWS = {"序号", "sku", "SKU", "中文名"}
CORE_PARAM_KEYWORDS = (
    "承重",
    "载重",
    "容量",
    "容积",
    "功率",
    "精度",
    "速度",
    "转速",
    "兼容",
    "材质",
    "尺寸",
    "重量",
    "电压",
    "电流",
    "频率",
    "压力",
    "流量",
    "扭矩",
    "续航",
    "适用",
    "年龄",
)
RESEARCH_EXCLUDED_KEYWORDS = (
    "包装",
    "包材",
    "立项规格",
    "录入系统",
    "外箱",
    "内箱",
    "箱规",
    "毛重",
    "装箱",
)
CONFLICT_PARAM_KEYWORDS = (
    "承重",
    "载重",
    "容量",
    "容积",
    "功率",
    "精度",
    "速度",
    "转速",
    "尺寸",
    "重量",
    "电压",
    "压力",
    "流量",
    "扭矩",
)
CONFLICT_OVERVIEW_TERMS = {
    "承重": ("承重", "载重", "load", "weight capacity", "capacity"),
    "载重": ("载重", "承重", "load", "weight capacity", "capacity"),
    "容量": ("容量", "容积", "capacity", "volume"),
    "容积": ("容积", "容量", "capacity", "volume"),
    "功率": ("功率", "power", "wattage"),
    "精度": ("精度", "accuracy", "precision"),
    "速度": ("速度", "speed"),
    "转速": ("转速", "rpm", "rotation speed"),
    "尺寸": ("尺寸", "size", "dimension", "dimensions"),
    "重量": ("重量", "weight"),
    "电压": ("电压", "voltage"),
    "压力": ("压力", "pressure"),
    "流量": ("流量", "flow", "flow rate"),
    "扭矩": ("扭矩", "torque"),
}
UNIT_ALIASES = {
    "lbs": "lb",
    "pounds": "lb",
    "pound": "lb",
    "lb": "lb",
    "kgs": "kg",
    "kg": "kg",
    "g": "g",
    "w": "w",
    "kw": "kw",
    "v": "v",
    "a": "a",
    "rpm": "rpm",
    "r/min": "rpm",
    "mm": "mm",
    "cm": "cm",
    "m": "m",
    "in": "in",
    "inch": "in",
    "inches": "in",
    "ft": "ft",
    "l": "l",
    "ml": "ml",
    "gal": "gal",
    "%": "%",
}
NUMBER_UNIT_RE = re.compile(
    r"(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>lbs?|pounds?|kgs?|kg|g|kw|w|v|a|rpm|r/min|mm|cm|m|inches|inch|in|ft|l|ml|gal|%)",
    re.IGNORECASE,
)


def _clean(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    return text


def _model_to_dict(model) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    return model.dict()


def _row_label(df, row_index: int) -> str:
    if row_index >= len(df) or df.shape[1] < 1:
        return ""
    return _clean(df.iat[row_index, 0])


def _find_row_index(df, candidates: Iterable[str], fallback: int) -> int:
    normalized_candidates = {item.lower() for item in candidates}
    for row_index in range(len(df)):
        label = _row_label(df, row_index)
        if label.lower() in normalized_candidates:
            return row_index
    return fallback


def build_parsed_spec_file_result(result, spec_file: SpecFile) -> ParsedSpecFileResult:
    df = result.chinese_table.fillna("").astype(str)
    order_row = _find_row_index(df, {"序号"}, 0)
    sku_row = _find_row_index(df, {"SKU", "sku"}, 1)
    name_row = _find_row_index(df, {"中文名"}, 2)

    raw_sku_values = [_clean(df.iat[sku_row, col]) if sku_row < len(df) else "" for col in range(1, df.shape[1])]
    counts = Counter(value or f"SKU{index + 1}" for index, value in enumerate(raw_sku_values))

    sku_list: List[SkuInfo] = []
    for offset, col in enumerate(range(1, df.shape[1]), start=1):
        sku = _clean(df.iat[sku_row, col]) if sku_row < len(df) else ""
        if not sku:
            sku = f"SKU{offset}"
        sequence_text = _clean(df.iat[order_row, col]) if order_row < len(df) else ""
        try:
            sequence = int(float(sequence_text))
        except ValueError:
            sequence = offset
        sku_id = sku if counts[sku] == 1 else f"{sku}__{sequence}"
        name = _clean(df.iat[name_row, col]) if name_row < len(df) else ""
        sku_list.append(
            SkuInfo(
                sku_id=sku_id,
                sku=sku,
                name=name,
                sequence=sequence,
                source_file_id=spec_file.id,
                source_file_name=spec_file.filename,
                source_column_index=col,
            )
        )

    sku_list.sort(key=lambda item: (item.sequence, item.source_column_index))
    standard_rows: List[StandardParameterRow] = []
    for row_index in range(len(df)):
        parameter = _row_label(df, row_index)
        if not parameter:
            continue
        values_by_sku: Dict[str, str] = {}
        values: List[str] = []
        for sku in sku_list:
            value = _clean(df.iat[row_index, sku.source_column_index]) if sku.source_column_index < df.shape[1] else ""
            values.append(value)
            values_by_sku[sku.sku_id] = value
        standard_rows.append(StandardParameterRow(parameter=parameter, values=values, values_by_sku=values_by_sku))

    common_parameters = _extract_product_common_parameters(standard_rows, sku_list)
    group_parameters = _extract_sku_group_common_parameters(standard_rows, sku_list, common_parameters)
    differential_parameters = _extract_sku_differential_parameters(standard_rows, sku_list, common_parameters)

    messages = []
    if len(sku_list) != int(result.context.get("sku_count", len(sku_list)) or 0):
        messages.append("中文参数表 SKU 数量与 pipeline context 不完全一致，已以中文参数表为准。")

    return ParsedSpecFileResult(
        spec_file_id=spec_file.id,
        filename=spec_file.filename,
        upload_order=spec_file.upload_order,
        status="completed",
        category=_clean(result.category),
        sku_count=len(sku_list),
        output_path=str(result.output_path),
        exception_count=int(len(result.exception_table)),
        sku_list=sku_list,
        standard_chinese_parameter_table=standard_rows,
        product_common_parameters=common_parameters,
        sku_group_common_parameters=group_parameters,
        sku_differential_parameters=differential_parameters,
        messages=messages,
    )


def merge_parsed_spec_results(project: PlanningProject, parsed_file: ParsedSpecFileResult) -> ParsedSpecResult:
    spec_file_ids = [item.id for item in sorted(project.spec_files, key=lambda item: item.upload_order or 0)]
    fields = {
        "source": "workbench_spec_sheet_arranger",
        "parsed_file_id": parsed_file.spec_file_id,
        "parsed_filename": parsed_file.filename,
        "uploaded_file_count": len(project.spec_files),
        "v0_1_multi_file_policy": "V0.1 只解析上传顺序中的第一份规格书；多规格书合并后续加强。",
    }
    if len(project.spec_files) > 1:
        fields["unparsed_file_ids"] = [item.id for item in project.spec_files if item.id != parsed_file.spec_file_id]

    return ParsedSpecResult(
        status="completed",
        spec_file_ids=spec_file_ids,
        sku_count=parsed_file.sku_count,
        category=parsed_file.category,
        summary=f"规格书解析完成，识别 {parsed_file.sku_count} 个 SKU。",
        parsed_files=[parsed_file],
        output_paths=[parsed_file.output_path] if parsed_file.output_path else [],
        sku_list=parsed_file.sku_list,
        standard_chinese_parameter_table=parsed_file.standard_chinese_parameter_table,
        product_common_parameters=parsed_file.product_common_parameters,
        sku_group_common_parameters=parsed_file.sku_group_common_parameters,
        sku_differential_parameters=parsed_file.sku_differential_parameters,
        exception_count=parsed_file.exception_count,
        fields=fields,
    )


def build_planning_context(
    project: PlanningProject,
    parsed_spec: ParsedSpecResult,
    answers: Dict[str, str] | None = None,
) -> PlanningContext:
    answers = answers or {}
    assumptions = []
    if len(project.spec_files) > 1:
        assumptions.append("V0.1 已保留多规格书上传顺序，但仅解析第一份规格书。")
    if parsed_spec.exception_count:
        assumptions.append(f"规格书整理产生 {parsed_spec.exception_count} 条异常记录，可在输出文件的异常记录 Sheet 查看。")

    research_brief = build_research_brief(project.user_input.keywords, parsed_spec.standard_chinese_parameter_table, parsed_spec.sku_list)
    user_input = {
        "keywords_raw": project.user_input.keywords,
        "overview": project.user_input.overview,
        "project_name": project.project_name,
    }
    spec_result = {
        "source_files": [_model_to_dict(item) for item in project.spec_files],
        "category": parsed_spec.category,
        "sku_count": parsed_spec.sku_count,
        "output_paths": parsed_spec.output_paths,
        "parser": "D:\\智能翻译工作台1.2\\spec_sheet_arranger",
    }

    return PlanningContext(
        status="completed",
        summary=f"规格书解析完成，识别 {parsed_spec.sku_count} 个 SKU，已建立 Planning Context。",
        user_input=user_input,
        spec_result=spec_result,
        sku_list=parsed_spec.sku_list,
        standard_chinese_parameter_table=parsed_spec.standard_chinese_parameter_table,
        product_common_parameters=parsed_spec.product_common_parameters,
        sku_group_common_parameters=parsed_spec.sku_group_common_parameters,
        sku_differential_parameters=parsed_spec.sku_differential_parameters,
        research_brief=research_brief,
        facts={
            "keywords": project.user_input.keywords,
            "overview": project.user_input.overview,
            "category": parsed_spec.category,
            "sku_count": parsed_spec.sku_count,
            "product_common_parameters": parsed_spec.product_common_parameters,
            "sku_group_common_parameters": [_model_to_dict(item) for item in parsed_spec.sku_group_common_parameters],
            "sku_differential_parameters": parsed_spec.sku_differential_parameters,
        },
        assumptions=assumptions,
        human_notes={key: value for key, value in answers.items() if str(value).strip()},
    )


def build_manual_planning_context(project: PlanningProject, answers: Dict[str, str]) -> PlanningContext:
    research_brief = build_research_brief(project.user_input.keywords, [], [])
    return PlanningContext(
        status="manual_without_spec",
        summary="用户确认暂不使用规格书，Planning Context 仅包含用户输入和人工说明。",
        user_input={
            "keywords_raw": project.user_input.keywords,
            "overview": project.user_input.overview,
            "project_name": project.project_name,
        },
        spec_result={"source_files": [], "category": "", "sku_count": 0, "output_paths": [], "parser": ""},
        research_brief=research_brief,
        facts={
            "keywords": project.user_input.keywords,
            "overview": project.user_input.overview,
            "manual_notes": answers,
        },
        assumptions=["未上传规格书，后续涉及硬参数时不得自行补充或猜测。"],
        human_notes={key: value for key, value in answers.items() if str(value).strip()},
    )


def build_research_brief(
    raw_keywords: str,
    rows: Sequence[StandardParameterRow],
    sku_list: Sequence[SkuInfo],
) -> ResearchBrief:
    english_keyword = extract_english_keyword(raw_keywords)
    core_parameters: List[ResearchCoreParameter] = []

    for row in rows:
        if row.parameter in METADATA_ROWS:
            continue
        if not _is_research_core_parameter(row.parameter):
            continue
        non_empty = {sku.sku_id: row.values_by_sku.get(sku.sku_id, "") for sku in sku_list if row.values_by_sku.get(sku.sku_id, "")}
        if not non_empty:
            continue
        unique_values = sorted(set(non_empty.values()))
        if len(unique_values) == 1 and len(non_empty) == len(sku_list):
            core_parameters.append(
                ResearchCoreParameter(name=row.parameter, value=unique_values[0], scope="product_common", sku_ids=list(non_empty))
            )
        else:
            core_parameters.append(
                ResearchCoreParameter(
                    name=row.parameter,
                    scope="sku_variable",
                    sku_ids=list(non_empty),
                    values_by_sku=non_empty,
                )
            )
        if len(core_parameters) >= 12:
            break

    return ResearchBrief(
        raw_keywords=raw_keywords,
        english_core_keyword=english_keyword,
        keyword_source="user_input" if english_keyword else "",
        needs_english_keyword_generation=not bool(english_keyword),
        core_product_parameters=core_parameters,
        excluded_parameter_notes=["Research Brief 默认排除包装尺寸、包材、箱规、毛重等偏物流参数。"],
    )


def extract_english_keyword(raw_keywords: str) -> str:
    chunks = re.findall(r"[A-Za-z][A-Za-z0-9&+\- ]*[A-Za-z0-9]", raw_keywords or "")
    cleaned = [re.sub(r"\s+", " ", item).strip(" /,-") for item in chunks]
    cleaned = [item for item in cleaned if re.search(r"[A-Za-z]", item)]
    if not cleaned:
        return ""
    return max(cleaned, key=len)


def detect_overview_conflicts(
    overview: str,
    rows: Sequence[StandardParameterRow],
    sku_list: Sequence[SkuInfo],
    resolved_answers: Dict[str, str],
) -> List[ConfirmationQuestion]:
    if not overview.strip() or not rows or not sku_list:
        return []

    questions: List[ConfirmationQuestion] = []
    for row in rows:
        if row.parameter in METADATA_ROWS or not _is_conflict_candidate(row.parameter):
            continue
        matched_keywords = [keyword for keyword in CONFLICT_PARAM_KEYWORDS if keyword in row.parameter]
        if not matched_keywords:
            continue
        overview_terms = []
        for keyword in matched_keywords:
            overview_terms.extend(CONFLICT_OVERVIEW_TERMS.get(keyword, (keyword,)))
        overview_claims = _extract_claims_near_keywords(overview, overview_terms)
        if not overview_claims:
            continue

        spec_values = {sku.sku_id: row.values_by_sku.get(sku.sku_id, "") for sku in sku_list if row.values_by_sku.get(sku.sku_id, "")}
        if not spec_values:
            continue
        spec_pairs_by_sku = {sku_id: _extract_number_unit_pairs(value) for sku_id, value in spec_values.items()}

        for claim_text, claim_number, claim_unit in overview_claims:
            comparable_skus = {
                sku_id: pairs
                for sku_id, pairs in spec_pairs_by_sku.items()
                if any(unit == claim_unit for _, unit, _ in pairs)
            }
            if not comparable_skus:
                continue
            matching_skus = [
                sku_id
                for sku_id, pairs in comparable_skus.items()
                if any(unit == claim_unit and _numbers_close(number, claim_number) for number, unit, _ in pairs)
            ]
            if len(matching_skus) == len(comparable_skus):
                continue

            background_values = []
            for sku in sku_list:
                value = spec_values.get(sku.sku_id, "")
                if value:
                    label = sku.sku or sku.sku_id
                    background_values.append(f"{label}: {value}")
            digest = hashlib.md5(
                f"{row.parameter}|{claim_text}|{'|'.join(background_values)}".encode("utf-8")
            ).hexdigest()[:10]
            question_id = f"conflict_{digest}"
            if resolved_answers.get(question_id, "").strip():
                continue
            questions.append(
                ConfirmationQuestion(
                    id=question_id,
                    prompt="请确认正确参数，或说明该参数适用的 SKU 范围。",
                    background=(
                        f"检测到产品概述中包含「{claim_text}」，但规格书参数「{row.parameter}」记录为："
                        f"{'；'.join(background_values[:8])}。规格书硬参数不会被概述自动覆盖。"
                    ),
                )
            )
            break

    return questions


def _extract_product_common_parameters(
    rows: Sequence[StandardParameterRow],
    sku_list: Sequence[SkuInfo],
) -> Dict[str, str]:
    common: Dict[str, str] = {}
    if not sku_list:
        return common
    sku_ids = [item.sku_id for item in sku_list]
    for row in rows:
        if row.parameter in METADATA_ROWS:
            continue
        values = [row.values_by_sku.get(sku_id, "") for sku_id in sku_ids]
        non_empty = [value for value in values if value]
        if len(non_empty) == len(sku_ids) and len(set(non_empty)) == 1:
            common[row.parameter] = non_empty[0]
    return common


def _extract_sku_group_common_parameters(
    rows: Sequence[StandardParameterRow],
    sku_list: Sequence[SkuInfo],
    common_parameters: Dict[str, str],
) -> List[SkuGroupCommonParameters]:
    if len(sku_list) < 3:
        return []

    grouped: Dict[tuple[str, ...], Dict[str, str]] = defaultdict(dict)
    sku_by_id = {sku.sku_id: sku for sku in sku_list}
    for row in rows:
        if row.parameter in METADATA_ROWS or row.parameter in common_parameters:
            continue
        value_to_skus: Dict[str, List[str]] = defaultdict(list)
        for sku in sku_list:
            value = row.values_by_sku.get(sku.sku_id, "")
            if value:
                value_to_skus[value].append(sku.sku_id)
        for value, sku_ids in value_to_skus.items():
            if 1 < len(sku_ids) < len(sku_list):
                grouped[tuple(sku_ids)][row.parameter] = value

    groups: List[SkuGroupCommonParameters] = []
    sorted_items = sorted(
        grouped.items(),
        key=lambda item: (sku_by_id[item[0][0]].sequence if item[0] else 9999, -len(item[0]), tuple(item[0])),
    )
    for index, (sku_ids, parameters) in enumerate(sorted_items, start=1):
        groups.append(
            SkuGroupCommonParameters(
                group_id=f"sku_group_{index}",
                sku_ids=list(sku_ids),
                sku_indexes=[sku_by_id[sku_id].sequence for sku_id in sku_ids if sku_id in sku_by_id],
                parameters=parameters,
            )
        )
    return groups


def _extract_sku_differential_parameters(
    rows: Sequence[StandardParameterRow],
    sku_list: Sequence[SkuInfo],
    common_parameters: Dict[str, str],
) -> Dict[str, Dict[str, str]]:
    diffs: Dict[str, Dict[str, str]] = {sku.sku_id: {} for sku in sku_list}
    for row in rows:
        if row.parameter in METADATA_ROWS or row.parameter in common_parameters:
            continue
        values = [row.values_by_sku.get(sku.sku_id, "") for sku in sku_list]
        non_empty_counts = Counter(value for value in values if value)
        if len(non_empty_counts) <= 1:
            continue
        for sku, value in zip(sku_list, values):
            if value and non_empty_counts[value] == 1:
                diffs[sku.sku_id][row.parameter] = value
    return {sku_id: params for sku_id, params in diffs.items() if params}


def _is_research_core_parameter(parameter: str) -> bool:
    if any(keyword in parameter for keyword in RESEARCH_EXCLUDED_KEYWORDS):
        return False
    return any(keyword in parameter for keyword in CORE_PARAM_KEYWORDS)


def _is_conflict_candidate(parameter: str) -> bool:
    if any(keyword in parameter for keyword in RESEARCH_EXCLUDED_KEYWORDS):
        return False
    return any(keyword in parameter for keyword in CONFLICT_PARAM_KEYWORDS)


def _canonical_unit(unit: str) -> str:
    return UNIT_ALIASES.get(str(unit).strip().lower(), str(unit).strip().lower())


def _extract_number_unit_pairs(text: str) -> List[tuple[float, str, str]]:
    pairs = []
    for match in NUMBER_UNIT_RE.finditer(text or ""):
        unit = _canonical_unit(match.group("unit"))
        if not unit:
            continue
        pairs.append((float(match.group("number")), unit, match.group(0)))
    return pairs


def _extract_claims_near_keywords(text: str, keywords: Sequence[str]) -> List[tuple[str, float, str]]:
    claims: List[tuple[str, float, str]] = []
    for keyword in keywords:
        for match in re.finditer(re.escape(keyword), text, flags=re.IGNORECASE):
            window = text[match.start() : min(len(text), match.end() + 48)]
            for number, unit, raw in _extract_number_unit_pairs(window):
                claims.append((f"{keyword} {raw}", number, unit))
    return claims


def _numbers_close(left: float, right: float) -> bool:
    tolerance = max(0.01, abs(right) * 0.015)
    return abs(left - right) <= tolerance
