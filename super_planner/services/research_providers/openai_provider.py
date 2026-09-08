# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from super_planner.core.exceptions import WorkflowSystemError
from super_planner.schemas.project import PlanningProject, ResearchCoreParameter, ResearchResult
from super_planner.services.openai_client import build_openai_client, get_openai_settings
from super_planner.services.research_providers.base import ProgressCallback, ProviderHealth, emit_progress


REQUIRED_RESULT_KEYS = (
    "market_context",
    "keyword_context",
    "audience_demand_context",
    "competitor_context",
    "voc_context",
    "market_tier",
    "opportunity_signals",
    "evidence",
    "data_quality",
)

MARKET_NUMBER_PATTERN = re.compile(
    r"(search volume|monthly sales|sales volume|market share|bsr|rank|ranking|review percentage|"
    r"搜索量|销量|月销|销售额|市场份额|份额|排名|评论占比|好评率|差评率|\$\s*\d|\d+(?:\.\d+)?\s*%)",
    re.IGNORECASE,
)


class OpenAIResearchProvider:
    name = "openai"

    def __init__(self) -> None:
        self.settings = get_openai_settings()

    def health_check(self) -> ProviderHealth:
        configured = bool(self.settings.api_key)
        return ProviderHealth(
            name=self.name,
            configured=configured,
            available=configured,
            reason="" if configured else "OpenAI API key is not configured.",
            metadata={
                "model": self.settings.model,
                "config_source": self.settings.source,
                "workbench_defaults_used": self.settings.workbench_defaults_used,
                "supports_web_search_attempt": True,
            },
        )

    def run(self, project: PlanningProject, progress: ProgressCallback | None = None) -> ResearchResult:
        health = self.health_check()
        if not health.available:
            raise WorkflowSystemError("OpenAI Research Provider is not configured. Please contact IT.")

        client = build_openai_client()
        if client is None:
            raise WorkflowSystemError("OpenAI Python client is unavailable or API key is missing. Please contact IT.")

        research_input = build_research_input(project)
        seed_evidence = build_seed_evidence(research_input)
        system_prompt = build_system_prompt()
        user_prompt = build_user_prompt(research_input, seed_evidence, web_search_allowed=True)

        response_meta: dict[str, Any] = {}
        web_search_enabled = False
        web_search_used = False
        web_search_error = ""

        emit_progress(progress, "正在分析市场及类目")
        try:
            output_text, response_meta = self._generate_with_responses(
                client=client,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                use_web_search=True,
            )
            web_search_enabled = True
            web_search_used = bool(response_meta.get("web_search_used"))
        except Exception as exc:  # noqa: BLE001
            web_search_error = _sanitize_error(exc)
            emit_progress(progress, "正在使用 OpenAI 模型整理市场研究")
            try:
                output_text, response_meta = self._generate_without_web_search(
                    client=client,
                    system_prompt=system_prompt,
                    user_prompt=build_user_prompt(research_input, seed_evidence, web_search_allowed=False),
                )
            except Exception as fallback_exc:  # noqa: BLE001
                detail = _sanitize_error(fallback_exc)
                raise WorkflowSystemError(
                    f"OpenAI Research Provider request failed. Please contact IT. Detail: {detail}"
                ) from fallback_exc

        emit_progress(progress, "正在研究关键词")
        result_data = parse_json_object(output_text)
        if not isinstance(result_data, dict):
            raise WorkflowSystemError("OpenAI Research returned invalid JSON. Please contact IT.")

        emit_progress(progress, "正在筛选代表性竞品")
        normalized = normalize_research_payload(result_data)
        emit_progress(progress, "正在分析用户需求/VOC")

        web_evidence = build_openai_web_evidence(response_meta)
        evidence = merge_evidence(seed_evidence, normalized.get("evidence", []), web_evidence)
        invalid_evidence_refs = sanitize_evidence_references(normalized, evidence)
        source_urls = [str(item.get("url")) for item in evidence if item.get("url")]
        data_quality = normalize_data_quality(
            normalized.get("data_quality", {}),
            provider_health=health.to_dict(),
            web_search_enabled=web_search_enabled,
            web_search_used=web_search_used,
            web_search_error=web_search_error,
            evidence=evidence,
            payload=normalized,
            invalid_evidence_refs=invalid_evidence_refs,
        )

        emit_progress(progress, "正在整理研究结果")
        return ResearchResult(
            status="completed",
            summary=build_summary(research_input, data_quality),
            research_provider=self.name,
            provider_status="completed",
            target_market="Amazon US",
            web_search_enabled=web_search_enabled,
            web_search_used=web_search_used,
            generated_at=datetime.utcnow(),
            market_context=normalized["market_context"],
            keyword_context=normalized["keyword_context"],
            audience_demand_context=normalized["audience_demand_context"],
            competitor_context=normalized["competitor_context"],
            voc_context=normalized["voc_context"],
            market_tier=normalized["market_tier"],
            opportunity_signals=normalized["opportunity_signals"],
            evidence=evidence,
            data_quality=data_quality,
            sources=source_urls,
            raw_payload={
                "provider": self.name,
                "model": self.settings.model,
                "config_source": self.settings.source,
                "workbench_defaults_used": self.settings.workbench_defaults_used,
                "input_snapshot": research_input,
                "response_id": response_meta.get("response_id", ""),
                "response_api": response_meta.get("api", ""),
                "web_search_error": web_search_error,
                "workflow_control": normalized.get("workflow_control", {}),
            },
        )

    def _generate_with_responses(
        self,
        *,
        client: Any,
        system_prompt: str,
        user_prompt: str,
        use_web_search: bool,
    ) -> tuple[str, dict[str, Any]]:
        if not hasattr(client, "responses"):
            raise RuntimeError("OpenAI Responses API is not available in the installed SDK.")

        tools = []
        if use_web_search:
            tools = [{"type": "web_search", "search_context_size": "medium"}]

        try:
            response = client.responses.create(
                model=self.settings.model,
                instructions=system_prompt,
                input=user_prompt,
                tools=tools,
                temperature=0.2,
            )
        except TypeError:
            response = client.responses.create(
                model=self.settings.model,
                instructions=system_prompt,
                input=user_prompt,
                tools=tools,
            )
        except Exception:
            if not use_web_search:
                raise
            response = client.responses.create(
                model=self.settings.model,
                instructions=system_prompt,
                input=user_prompt,
                tools=[{"type": "web_search_preview", "search_context_size": "medium"}],
            )

        output_text = getattr(response, "output_text", "") or extract_text_from_response(response)
        meta = extract_response_metadata(response)
        meta["api"] = "responses"
        return output_text, meta

    def _generate_without_web_search(
        self,
        *,
        client: Any,
        system_prompt: str,
        user_prompt: str,
    ) -> tuple[str, dict[str, Any]]:
        try:
            output_text, meta = self._generate_with_responses(
                client=client,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                use_web_search=False,
            )
            meta["web_search_used"] = False
            return output_text, meta
        except Exception:
            pass

        payload = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        try:
            response = client.chat.completions.create(**payload)
        except Exception:
            payload.pop("response_format", None)
            response = client.chat.completions.create(**payload)

        content = response.choices[0].message.content
        return content, {"api": "chat.completions", "response_id": getattr(response, "id", ""), "web_search_used": False}


def build_research_input(project: PlanningProject) -> dict[str, Any]:
    planning_context = project.planning_context
    brief = planning_context.research_brief
    keyword = (brief.english_core_keyword or brief.raw_keywords or project.user_input.keywords).strip()
    overview = (
        str(planning_context.user_input.get("overview") or "").strip()
        or str(planning_context.facts.get("overview") or "").strip()
        or project.user_input.overview
    )
    parameters = [serialize_core_parameter(item) for item in brief.core_product_parameters]
    return {
        "target_market": "Amazon US",
        "english_core_keyword": keyword,
        "raw_keywords": brief.raw_keywords or project.user_input.keywords,
        "keyword_source": brief.keyword_source,
        "needs_english_keyword_generation": brief.needs_english_keyword_generation,
        "product_overview": overview,
        "core_product_parameters": parameters,
        "excluded_parameter_notes": brief.excluded_parameter_notes,
        "human_notes": planning_context.human_notes or {},
        "project_id": project.project_id,
    }


def serialize_core_parameter(parameter: ResearchCoreParameter) -> dict[str, Any]:
    data = parameter.model_dump(mode="json") if hasattr(parameter, "model_dump") else parameter.dict()
    if data.get("value"):
        data["value"] = _trim(str(data["value"]), 500)
    if data.get("values_by_sku"):
        data["values_by_sku"] = {key: _trim(str(value), 320) for key, value in data["values_by_sku"].items()}
    return data


def build_seed_evidence(research_input: dict[str, Any]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    if research_input.get("english_core_keyword"):
        evidence.append(
            {
                "id": "user-input-keyword",
                "source_type": "user_input",
                "title": "User supplied product keyword",
                "summary": research_input["english_core_keyword"],
                "confidence": 1.0,
            }
        )
    if research_input.get("product_overview"):
        evidence.append(
            {
                "id": "user-input-overview",
                "source_type": "user_input",
                "title": "User supplied product overview",
                "summary": _trim(research_input["product_overview"], 600),
                "confidence": 1.0,
            }
        )
    for index, parameter in enumerate(research_input.get("core_product_parameters", []), start=1):
        value = parameter.get("value") or parameter.get("values_by_sku") or ""
        evidence.append(
            {
                "id": f"spec-fact-{index}",
                "source_type": "spec_fact",
                "title": f"Research core parameter: {parameter.get('name', '')}",
                "summary": _trim(json.dumps(value, ensure_ascii=False), 700),
                "raw": parameter,
                "confidence": 1.0,
            }
        )
    return evidence


def build_system_prompt() -> str:
    return """
You are Research Engine V0.1 for an Amazon US planning workflow.
Return only one valid JSON object. Do not return Markdown.

Boundaries:
- This is Research only. Do not output final product positioning, one-sentence purchase reason, final competitive strategy, TOP selling point ranking, or Listing copy.
- Use the supplied Planning Context only. Do not reinterpret the original spec sheet.
- Product hard parameters from spec_fact evidence are authoritative product facts.
- If Web Search evidence is available, use it for current market, competitor, listing, and review observations.
- If Web Search evidence is not available or a claim is not externally verified, mark it as model_inference.
- Never invent precise search volume, sales volume, BSR, ranking, market share, review percentage, exact price, ASIN, or competitor identity.
- If a precise number is not supported by web_evidence or sorftime_data, set the field to null or write "missing_verified_data".
- If true Amazon competitors cannot be verified, output competitor product types or candidates as "needs_verification" and leave ASIN null.
- VOC must not say "reviews show" unless there is review/listing web_evidence. Without evidence, use "potential demand/concern hypothesis".
- For every important conclusion, include evidence_ids and source_type: spec_fact, user_input, web_evidence, model_inference, or sorftime_data.
- evidence_ids must reference ids in this JSON evidence array or the supplied preloaded_evidence only. Do not use internal citation ids such as "turn0search0".
- Use Chinese for explanations; keep necessary English marketplace terms.

Research frame to use:
- Market/category structure, mainstream product forms, specification/price tier signals.
- Keyword dimensions: core terms, attribute terms, function terms, spec terms, use-case terms, audience terms.
- Audience & demand: user roles, jobs to be done, buying triggers, evaluation criteria, trust barriers, proof requirements, conversion drivers.
- Competitor context: candidate pool, representative competitors, inclusion/exclusion rationale, route/price/spec coverage when verified.
- VOC context: review-backed findings when evidence exists; otherwise demand/concern hypotheses.
- Market tier: stage/tier judgement with confidence and basis.
- Opportunity signals: facts or hypotheses worth passing to Strategy 4.0 for judgement, not final strategy.

Required JSON shape:
{
  "workflow_control": {
    "requires_user_confirmation": false,
    "reason": "",
    "category_options": []
  },
  "market_context": {
    "category_hypothesis": {"value": "", "source_type": "", "evidence_ids": [], "confidence": 0.0},
    "market_structure": [],
    "mainstream_product_forms": [],
    "price_spec_tiers": [],
    "notes": []
  },
  "keyword_context": {
    "core_terms": [],
    "attribute_terms": [],
    "function_terms": [],
    "spec_terms": [],
    "use_case_terms": [],
    "audience_terms": [],
    "missing_search_volume": true,
    "notes": []
  },
  "audience_demand_context": {
    "audiences": [],
    "jobs_to_be_done": [],
    "buying_triggers": [],
    "evaluation_criteria": [],
    "trust_barriers": [],
    "proof_requirements": [],
    "conversion_drivers": []
  },
  "competitor_context": {
    "candidate_pool": [],
    "representative_competitors": [],
    "selection_logic": [],
    "excluded_candidates": [],
    "limitations": []
  },
  "voc_context": {
    "review_based_findings": [],
    "inferred_needs_or_concerns": [],
    "limitations": []
  },
  "market_tier": {
    "tier_judgement": "",
    "basis": [],
    "confidence": 0.0,
    "risk_notes": []
  },
  "opportunity_signals": [],
  "evidence": [],
  "data_quality": {
    "verified": [],
    "inferred": [],
    "missing_data": [],
    "data_completeness": 0.0,
    "confidence": 0.0,
    "risk_notes": []
  }
}
"""


def build_user_prompt(
    research_input: dict[str, Any],
    seed_evidence: list[dict[str, Any]],
    *,
    web_search_allowed: bool,
) -> str:
    return json.dumps(
        {
            "task": "Generate ResearchResult content for Research Engine V0.1.",
            "web_search_allowed": web_search_allowed,
            "fixed_market": "Amazon US",
            "research_input": research_input,
            "preloaded_evidence": seed_evidence,
            "instructions": [
                "Use Web Search when allowed to find current Amazon US category/listing/competitor/review evidence.",
                "When web evidence is used, add evidence objects with source_type=web_evidence, title, url, summary, and confidence.",
                "Do not fabricate any Sorftime data. Sorftime source_type is reserved only for future provider output.",
                "Keep all important conclusions traceable through evidence_ids.",
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


def parse_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None


def normalize_research_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    for key in REQUIRED_RESULT_KEYS:
        if key not in normalized or normalized[key] is None:
            normalized[key] = [] if key in {"opportunity_signals", "evidence"} else {}
    normalized.setdefault("workflow_control", {"requires_user_confirmation": False, "reason": "", "category_options": []})
    if not isinstance(normalized["opportunity_signals"], list):
        normalized["opportunity_signals"] = []
    if not isinstance(normalized["evidence"], list):
        normalized["evidence"] = []
    return normalized


def normalize_data_quality(
    data_quality: dict[str, Any],
    *,
    provider_health: dict[str, Any],
    web_search_enabled: bool,
    web_search_used: bool,
    web_search_error: str,
    evidence: list[dict[str, Any]],
    payload: dict[str, Any],
    invalid_evidence_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    quality = dict(data_quality or {})
    quality.setdefault("verified", [])
    quality.setdefault("inferred", [])
    quality.setdefault("missing_data", [])
    quality.setdefault("risk_notes", [])

    quality["research_provider"] = "openai"
    quality["provider_health"] = provider_health
    quality["web_search_enabled"] = web_search_enabled
    quality["web_search_used"] = web_search_used
    quality["generated_at"] = datetime.utcnow().isoformat()
    quality["evidence_counts"] = {
        "spec_fact": count_evidence(evidence, "spec_fact"),
        "user_input": count_evidence(evidence, "user_input"),
        "web_evidence": count_evidence(evidence, "web_evidence"),
        "model_inference": count_evidence(evidence, "model_inference"),
        "sorftime_data": count_evidence(evidence, "sorftime_data"),
    }

    if web_search_error:
        quality["web_search_error"] = web_search_error
        append_missing(
            quality,
            "OpenAI Web Search",
            "外部网页证据不足；当前结果只能使用模型推断、用户输入和规格书事实。",
            "Responses API web_search failed or was unsupported by the current model/gateway.",
        )
    if not web_search_used:
        append_missing(
            quality,
            "Verified Amazon market data",
            "不能输出搜索量、销量、BSR、市场份额、精确价格或评论比例。",
            "No completed web search evidence was detected.",
        )
    if count_evidence(evidence, "sorftime_data") == 0:
        append_missing(
            quality,
            "Sorftime data",
            "缺少 Sorftime 类目、关键词、竞品、销量和评论数据库结果。",
            "Sorftime Provider is reserved but not configured.",
        )

    risky = find_unverified_market_numbers(payload, evidence)
    if risky:
        quality["risk_notes"].append(
            {
                "type": "guardrail",
                "message": "Detected possible numeric market claims; verify that each one is backed by web_evidence or sorftime_data.",
                "examples": risky[:6],
            }
        )
    if invalid_evidence_refs:
        quality["risk_notes"].append(
            {
                "type": "evidence_reference_cleanup",
                "message": "Removed evidence_ids that did not match saved Evidence entries.",
                "removed_refs": invalid_evidence_refs[:12],
            }
        )

    try:
        quality["data_completeness"] = max(0.0, min(float(quality.get("data_completeness", 0.0)), 1.0))
    except (TypeError, ValueError):
        quality["data_completeness"] = 0.0
    try:
        quality["confidence"] = max(0.0, min(float(quality.get("confidence", 0.0)), 1.0))
    except (TypeError, ValueError):
        quality["confidence"] = 0.0
    return quality


def append_missing(quality: dict[str, Any], item: str, impact: str, reason: str) -> None:
    existing = quality.setdefault("missing_data", [])
    if any(isinstance(entry, dict) and entry.get("item") == item for entry in existing):
        return
    existing.append({"item": item, "impact": impact, "reason": reason})


def merge_evidence(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    for group in groups:
        for item in group or []:
            if not isinstance(item, dict):
                continue
            item = dict(item)
            url = str(item.get("url") or "").strip()
            item_id = str(item.get("id") or "").strip()
            if url and url in seen_urls:
                continue
            if not item_id:
                item_id = f"evidence-{len(merged) + 1}"
                item["id"] = item_id
            if item_id in seen_ids:
                item["id"] = f"{item_id}-{len(merged) + 1}"
            seen_ids.add(item["id"])
            if url:
                seen_urls.add(url)
            item.setdefault("source_type", "model_inference")
            merged.append(item)
    return merged


def sanitize_evidence_references(payload: Any, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    valid_ids = {str(item.get("id")) for item in evidence if item.get("id")}
    removed: list[dict[str, Any]] = []

    def walk(value: Any, path: str = "$") -> None:
        if isinstance(value, dict):
            ids = value.get("evidence_ids")
            if isinstance(ids, list):
                valid = [item for item in ids if str(item) in valid_ids]
                invalid = [str(item) for item in ids if str(item) not in valid_ids]
                if invalid:
                    value["evidence_ids"] = valid
                    removed.append({"path": path, "ids": invalid})
            for key, nested in value.items():
                walk(nested, f"{path}.{key}")
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                walk(nested, f"{path}[{index}]")

    walk(payload)
    return removed


def build_openai_web_evidence(meta: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for index, source in enumerate(meta.get("web_sources", []), start=1):
        url = str(source.get("url") or "").strip()
        if not url:
            continue
        items.append(
            {
                "id": f"openai-web-{index}",
                "source_type": "web_evidence",
                "title": source.get("title") or source.get("url") or "OpenAI Web Search source",
                "url": url,
                "summary": source.get("summary", ""),
                "confidence": 0.75,
            }
        )
    return items


def extract_response_metadata(response: Any) -> dict[str, Any]:
    data = response_to_dict(response)
    output = data.get("output", []) if isinstance(data, dict) else []
    web_sources: list[dict[str, Any]] = []
    web_search_used = False

    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "web_search_call":
            web_search_used = True
            action = item.get("action") or {}
            for source in action.get("sources") or []:
                if isinstance(source, dict) and source.get("url"):
                    web_sources.append({"url": source.get("url"), "title": source.get("title", "")})
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            for annotation in content.get("annotations") or []:
                if not isinstance(annotation, dict):
                    continue
                url = annotation.get("url") or annotation.get("uri")
                if url:
                    web_sources.append(
                        {
                            "url": url,
                            "title": annotation.get("title") or annotation.get("text") or url,
                            "summary": annotation.get("text", ""),
                        }
                    )

    return {
        "response_id": data.get("id", "") if isinstance(data, dict) else getattr(response, "id", ""),
        "web_search_used": web_search_used,
        "web_sources": dedupe_sources(web_sources),
    }


def response_to_dict(response: Any) -> dict[str, Any]:
    if hasattr(response, "model_dump"):
        return response.model_dump()
    if hasattr(response, "dict"):
        return response.dict()
    try:
        return json.loads(response.model_dump_json())
    except Exception:
        return {}


def extract_text_from_response(response: Any) -> str:
    data = response_to_dict(response)
    parts: list[str] = []
    for item in data.get("output", []):
        for content in item.get("content", []) if isinstance(item, dict) else []:
            if isinstance(content, dict) and content.get("type") in {"output_text", "text"}:
                parts.append(str(content.get("text", "")))
    return "\n".join(part for part in parts if part)


def dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in sources:
        url = str(source.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(source)
    return deduped


def count_evidence(evidence: list[dict[str, Any]], source_type: str) -> int:
    return sum(1 for item in evidence if item.get("source_type") == source_type)


def find_unverified_market_numbers(payload: Any, evidence: list[dict[str, Any]]) -> list[str]:
    evidence_by_id = {str(item.get("id")): item for item in evidence if item.get("id")}
    risky: list[str] = []

    def has_verified_source(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        if item.get("source_type") in {"web_evidence", "sorftime_data"}:
            return True
        for evidence_id in item.get("evidence_ids") or []:
            source = evidence_by_id.get(str(evidence_id), {})
            if source.get("source_type") in {"web_evidence", "sorftime_data"}:
                return True
        return False

    def walk(value: Any, parent: Any = None) -> None:
        if len(risky) >= 12:
            return
        if isinstance(value, dict):
            for nested in value.values():
                walk(nested, value)
            return
        if isinstance(value, list):
            for nested in value:
                walk(nested, parent)
            return
        if isinstance(value, str) and MARKET_NUMBER_PATTERN.search(value) and not has_verified_source(parent):
            risky.append(_trim(value, 180))

    walk(payload)
    return risky


def build_summary(research_input: dict[str, Any], data_quality: dict[str, Any]) -> str:
    missing_count = len(data_quality.get("missing_data") or [])
    suffix = "，缺失数据已记录到 Data Quality" if missing_count else ""
    return (
        f"市场研究完成：{research_input.get('english_core_keyword', '')} / Amazon US。"
        f"已整理 Market、Keyword、Audience、Competitor、VOC、Market Tier 和 Opportunity Signals{suffix}。"
    )


def _sanitize_error(exc: Exception) -> str:
    text = str(exc)
    text = re.sub(r"sk-[A-Za-z0-9_\-]+", "sk-***", text)
    text = re.sub(r"Bearer\s+[A-Za-z0-9_\-.]+", "Bearer ***", text, flags=re.IGNORECASE)
    return _trim(text, 500)


def _trim(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
