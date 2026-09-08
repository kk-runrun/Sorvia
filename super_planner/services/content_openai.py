# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import re
from typing import Any

from super_planner.core.exceptions import WorkflowSystemError
from super_planner.services.openai_client import build_openai_client, get_openai_settings


COPY_GENERATION_PROVIDER = "openai"
MAX_LENGTH_REWRITE_ROUNDS = 3


class OpenAICopyGenerator:
    def __init__(self) -> None:
        self.settings = get_openai_settings()
        self.client = build_openai_client()
        self.calls: list[dict[str, Any]] = []

    def ensure_available(self) -> None:
        if not self.settings.api_key or self.client is None:
            raise WorkflowSystemError("OpenAI Copy Generation is not configured. Please contact IT.")

    def generate_initial_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.ensure_available()
        output_text, meta = self._call_json(
            purpose="initial_content_generation",
            system_prompt=build_generation_system_prompt(),
            user_prompt=json.dumps(payload, ensure_ascii=False, indent=2),
            temperature=0.35,
            schema=content_generation_schema(),
        )
        parsed = parse_json_object(output_text)
        if not isinstance(parsed, dict):
            raise WorkflowSystemError("OpenAI Copy Generation returned invalid JSON. Please contact IT.")
        parsed.setdefault("_openai_meta", meta)
        return parsed

    def repair_length(self, item: dict[str, Any], failures: list[dict[str, Any]], context: dict[str, Any], round_index: int) -> dict[str, Any]:
        self.ensure_available()
        payload = {
            "task": "Rewrite one ContentResult item to satisfy character rules without changing strategy or facts.",
            "round": round_index,
            "content_item": compact_item_for_repair(item),
            "failed_length_checks": failures,
            "target_character_count": target_character_count(failures),
            "context": context,
            "instructions": [
                "Return only JSON.",
                "Keep the same id.",
                "Revise English first, then translate the final English into semantically matching Chinese.",
                "Do not add, guess, convert, or modify any parameter.",
                "Do not add fake brand, certification, accessory, ASIN, search volume, sales, BSR, review ratio, price, or market share.",
                "Do not solve length by hard truncation. Rephrase naturally.",
                "Aim for the target_character_count exactly when it is supplied.",
                "For Bullet Point, keep Title: Description, Title Case heading, clear subject, no terminal punctuation.",
            ],
        }
        output_text, _meta = self._call_json(
            purpose=f"length_repair_round_{round_index}",
            system_prompt=build_repair_system_prompt(),
            user_prompt=json.dumps(payload, ensure_ascii=False, indent=2),
            temperature=0.2,
            schema=content_repair_schema(),
        )
        parsed = parse_json_object(output_text)
        if not isinstance(parsed, dict):
            return {}
        return parsed

    def meta(self) -> dict[str, Any]:
        return {
            "provider": COPY_GENERATION_PROVIDER,
            "model": self.settings.model,
            "config_source": self.settings.source,
            "workbench_defaults_used": self.settings.workbench_defaults_used,
            "calls": self.calls,
        }

    def _call_json(
        self,
        *,
        purpose: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        schema: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        attempts = []

        if hasattr(self.client, "responses"):
            attempts.append(("responses_json_schema", lambda: self.client.responses.create(
                model=self.settings.model,
                instructions=system_prompt,
                input=user_prompt,
                text={"format": schema},
                temperature=temperature,
            )))
            attempts.append(("responses_json_prompt", lambda: self.client.responses.create(
                model=self.settings.model,
                instructions=system_prompt + "\nReturn one valid JSON object.",
                input=user_prompt,
                temperature=temperature,
            )))

        attempts.append(("chat_json_object", lambda: self.client.chat.completions.create(
            model=self.settings.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            response_format={"type": "json_object"},
        )))
        attempts.append(("chat_json_prompt", lambda: self.client.chat.completions.create(
            model=self.settings.model,
            messages=[
                {"role": "system", "content": system_prompt + "\nReturn one valid JSON object."},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
        )))

        errors = []
        for api_name, call in attempts:
            try:
                response = call()
                output_text = extract_text_from_response(response)
                meta = {"api": api_name, "response_id": getattr(response, "id", "")}
                self.calls.append({"purpose": purpose, **meta})
                return output_text, meta
            except TypeError as exc:
                errors.append(f"{api_name}: {_sanitize_error(exc)}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{api_name}: {_sanitize_error(exc)}")
                continue

        detail = "; ".join(errors[-3:])
        raise WorkflowSystemError(f"OpenAI Copy Generation request failed. Please contact IT. Detail: {detail}")


def build_generation_system_prompt() -> str:
    return """
You are Content Engine V0.1 for an Amazon US planning workflow.
Return only one valid JSON object. Do not return Markdown.

Role boundary:
- Strategy decides what to say. You only decide how to express it.
- Do not change User Purchase Reason, Communication Strategy, TOP1-TOP5 order, F1-F8 duties, A+ duties, or BP order.
- Do not re-read the original spec sheet. Do not do market research.
- Use only the supplied Planning Context, Research Keyword Context, StrategyResult, Evidence, and allowed product facts.
- Product hard parameters may only come from spec_fact / Planning Context. Do not create, convert, round, or guess parameters.
- When the Chinese material is 杉木, write it as fir wood or wood frame. Do not infer cedar unless cedar is explicitly verified as a product fact.
- Use the supplied brand_context.brand for Item Name. If brand_status is default_config, treat it as the configured demo default and do not replace it with "Brand".
- Do not write certification, accessory, warranty, ASIN, review, ranking, search volume, sales, price, market share, or comparison claims unless supplied as verified facts.
- model_inference and needs_verification must stay cautious; do not describe them as verified customer reviews or facts.

Output requirements:
- Generate English copy first, then Chinese copy with the same meaning.
- Keep terminology consistent: wooden greenhouse, walk-in greenhouse, PC panels, sloped roof, shelves, hooks, slat hanging panels.
- Use natural Amazon US listing language, but avoid unsupported performance promises.
- Item Name: Brand + core keyword + core parameter + keyword, 70-75 characters, Title Case.
- Item Highlight: scenario + purchase reason/function + long-tail terms, 120-125 characters, do not repeat the same information already carried by Item Name. Do not mention accessories/certifications unless verified.
- eBay Title: no more than 80 characters.
- Supporting image F1-F8: each has title <=50 and description <=300.
- Bullet Point: Title: Description, heading in Title Case, clear subject, <=255 characters, no terminal punctuation.
- Premium A+: each module has tag <=25, title <=80, description <=300.
- Introduction-2 Question <=120 and Answer <=250.
"""


def build_repair_system_prompt() -> str:
    return """
You are a character-control copy editor for Content Engine V0.1.
Return only one valid JSON object.
Revise only the failed item or failed copy part.
Preserve all strategy, evidence, SKU scope, product facts, and meaning.
Generate English first and Chinese second with matching meaning.
Do not use hard truncation; rewrite naturally.
"""


def content_generation_schema() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": "content_generation_result",
        "schema": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                }
            },
            "required": ["items"],
        },
        "strict": False,
    }


def content_repair_schema() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": "content_repair_result",
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


def compact_item_for_repair(item: dict[str, Any]) -> dict[str, Any]:
    keep = [
        "id",
        "module",
        "submodule",
        "sku_scope",
        "english_copy",
        "chinese_copy",
        "copy_parts",
        "length_rule_id",
        "strategy_reference",
        "selling_point_reference",
        "parameter_reference",
        "needs_verification",
    ]
    return {key: item.get(key) for key in keep if key in item}


def target_character_count(failures: list[dict[str, Any]]) -> int | None:
    if not failures:
        return None
    mins = [int(item.get("character_min") or 0) for item in failures]
    maxes = [item.get("character_max") for item in failures if item.get("character_max") is not None]
    if maxes:
        return int((max(mins) + min(int(item) for item in maxes)) / 2)
    return max(mins) if mins else None


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


def extract_text_from_response(response: Any) -> str:
    output_text = getattr(response, "output_text", "")
    if output_text:
        return output_text
    if hasattr(response, "choices"):
        choice = response.choices[0]
        return choice.message.content or ""
    data = response_to_dict(response)
    parts: list[str] = []
    for item in data.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) or []:
            if isinstance(content, dict) and content.get("type") in {"output_text", "text"}:
                parts.append(str(content.get("text", "")))
    return "\n".join(part for part in parts if part)


def response_to_dict(response: Any) -> dict[str, Any]:
    if hasattr(response, "model_dump"):
        return response.model_dump()
    if hasattr(response, "dict"):
        return response.dict()
    try:
        return json.loads(response.model_dump_json())
    except Exception:
        return {}


def _sanitize_error(exc: Exception) -> str:
    text = str(exc)
    text = re.sub(r"sk-[A-Za-z0-9_\-]+", "sk-***", text)
    text = re.sub(r"Bearer\s+[A-Za-z0-9_\-.]+", "Bearer ***", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()[:500]
