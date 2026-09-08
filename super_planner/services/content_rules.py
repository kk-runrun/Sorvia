# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


FORMAT_RULES_VERSION = "content-format-rules-v0.1"


@dataclass(frozen=True)
class LengthRule:
    rule_id: str
    min_chars: int
    max_chars: int | None


LENGTH_RULES: dict[str, LengthRule] = {
    "item_name": LengthRule("item_name", 70, 75),
    "item_highlight": LengthRule("item_highlight", 120, 125),
    "ebay_title": LengthRule("ebay_title", 0, 80),
    "introduction_2_question": LengthRule("introduction_2_question", 0, 120),
    "introduction_2_answer": LengthRule("introduction_2_answer", 0, 250),
    "supporting_image_title": LengthRule("supporting_image_title", 0, 50),
    "supporting_image_description": LengthRule("supporting_image_description", 0, 300),
    "bullet_point": LengthRule("bullet_point", 0, 255),
    "premium_aplus_title": LengthRule("premium_aplus_title", 0, 80),
    "premium_aplus_description": LengthRule("premium_aplus_description", 0, 300),
    "premium_aplus_tag": LengthRule("premium_aplus_tag", 0, 25),
    "unbounded": LengthRule("unbounded", 0, None),
}


TITLE_CASE_RULES = {
    "item_name",
    "ebay_title",
    "supporting_image_title",
    "premium_aplus_title",
    "premium_aplus_tag",
}

SENTENCE_CASE_RULES = {
    "supporting_image_description",
    "premium_aplus_description",
    "introduction_2_question",
    "introduction_2_answer",
    "product_description",
    "unbounded",
}

SMALL_WORDS = {
    "a",
    "an",
    "the",
    "and",
    "but",
    "or",
    "nor",
    "on",
    "at",
    "to",
    "from",
    "by",
    "with",
    "in",
    "of",
    "as",
    "for",
    "into",
    "onto",
    "over",
}

RESTORE_TERMS = {
    "pc": "PC",
    "pe": "PE",
    "pvc": "PVC",
    "uv": "UV",
    "led": "LED",
    "usb": "USB",
    "diy": "DIY",
    "sku": "SKU",
    "a+": "A+",
    "us": "US",
    "amazon": "Amazon",
    "ebay": "eBay",
    "vevor": "VEVOR",
    "in": "in",
    "ft": "ft",
    "mm": "mm",
    "cm": "cm",
    "m": "m",
    "kg": "kg",
    "g": "g",
    "lb": "lbs",
    "lbs": "lbs",
    "oz": "oz",
    "v": "V",
    "w": "W",
    "hz": "Hz",
}

SAFE_MARKETING_REPLACEMENTS = {
    "best": "practical",
    "perfect": "well-suited",
    "guaranteed": "designed",
    "guarantee": "support",
    "waterproof": "covered",
    "weatherproof": "outdoor-ready",
    "never": "helps reduce",
    "always": "is designed to",
}


def character_count(text: str) -> int:
    return len(text or "")


def rule_for(rule_id: str | None) -> LengthRule:
    return LENGTH_RULES.get(rule_id or "unbounded", LENGTH_RULES["unbounded"])


def length_status(text: str, rule_id: str | None) -> str:
    rule = rule_for(rule_id)
    count = character_count(text)
    if count < rule.min_chars:
        return "too_short"
    if rule.max_chars is not None and count > rule.max_chars:
        return "too_long"
    return "ok"


def length_distance(text: str, rule_id: str | None) -> int:
    rule = rule_for(rule_id)
    count = character_count(text)
    if count < rule.min_chars:
        return rule.min_chars - count
    if rule.max_chars is not None and count > rule.max_chars:
        return count - rule.max_chars
    return 0


def attach_length_metadata(item: dict[str, Any]) -> dict[str, Any]:
    item = dict(item)
    checks: dict[str, Any] = {}

    if isinstance(item.get("copy_parts"), dict):
        for name, part in item["copy_parts"].items():
            if not isinstance(part, dict):
                continue
            rule_id = part.get("length_rule_id") or item.get("length_rule_id") or "unbounded"
            english = format_for_rule(str(part.get("english_copy") or ""), rule_id)
            chinese = normalize_chinese_copy(str(part.get("chinese_copy") or ""))
            part["english_copy"] = english
            part["chinese_copy"] = chinese
            checks[name] = build_length_check(english, rule_id)
        item["copy_parts"] = item["copy_parts"]
        english_copy = combine_parts(item)
        chinese_copy = combine_parts(item, language="chinese")
        primary = item.get("primary_part")
        primary_check = checks.get(primary or "") or next(iter(checks.values()), build_length_check(english_copy, "unbounded"))
    else:
        rule_id = item.get("length_rule_id") or "unbounded"
        english_copy = format_for_rule(str(item.get("english_copy") or ""), rule_id)
        chinese_copy = normalize_chinese_copy(str(item.get("chinese_copy") or ""))
        primary_check = build_length_check(english_copy, rule_id)
        checks["english_copy"] = primary_check

    item["english_copy"] = english_copy
    item["chinese_copy"] = chinese_copy
    item["character_count"] = primary_check["character_count"]
    item["character_min"] = primary_check["character_min"]
    item["character_max"] = primary_check["character_max"]
    item["length_status"] = primary_check["length_status"]
    item["length_checks"] = checks
    return item


def build_length_check(text: str, rule_id: str | None) -> dict[str, Any]:
    rule = rule_for(rule_id)
    return {
        "rule_id": rule.rule_id,
        "character_count": character_count(text),
        "character_min": rule.min_chars,
        "character_max": rule.max_chars,
        "length_status": length_status(text, rule.rule_id),
    }


def item_length_ok(item: dict[str, Any]) -> bool:
    if isinstance(item.get("length_checks"), dict):
        return all(check.get("length_status") == "ok" for check in item["length_checks"].values())
    return item.get("length_status") == "ok"


def failed_length_checks(item: dict[str, Any]) -> list[dict[str, Any]]:
    failures = []
    for field, check in (item.get("length_checks") or {}).items():
        if check.get("length_status") != "ok":
            failures.append({"field": field, **check})
    return failures


def best_length_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    if not candidates:
        return {}

    def score(item: dict[str, Any]) -> tuple[int, int]:
        checks = item.get("length_checks") or {}
        failed = sum(1 for check in checks.values() if check.get("length_status") != "ok")
        distance = 0
        if isinstance(item.get("copy_parts"), dict):
            for name, part in item["copy_parts"].items():
                check = checks.get(name, {})
                distance += length_distance(str(part.get("english_copy") or ""), check.get("rule_id"))
        else:
            distance += length_distance(str(item.get("english_copy") or ""), item.get("length_rule_id"))
        return failed, distance

    return sorted(candidates, key=score)[0]


def combine_parts(item: dict[str, Any], *, language: str = "english") -> str:
    parts = item.get("copy_parts") or {}
    if not isinstance(parts, dict):
        return str(item.get(f"{language}_copy") or "").strip()
    order = item.get("copy_part_order") or list(parts.keys())
    key = f"{language}_copy"
    values = [str(parts[name].get(key) or "").strip() for name in order if isinstance(parts.get(name), dict)]
    return "\n".join(value for value in values if value)


def format_for_rule(text: str, rule_id: str | None) -> str:
    text = normalize_english_copy(text)
    rule_id = rule_id or "unbounded"
    if rule_id == "bullet_point":
        return format_bullet_point(text)
    if rule_id in TITLE_CASE_RULES:
        return smart_title_case(text).rstrip(".!?;: ")
    if rule_id == "item_highlight":
        return smart_title_case(text).rstrip(".!?;: ")
    if rule_id in SENTENCE_CASE_RULES:
        return sentence_case(text)
    return text


def format_bullet_point(text: str) -> str:
    text = normalize_english_copy(text).rstrip(".!?; ")
    if ":" not in text:
        return smart_title_case(text).rstrip(".!?; ")
    label, body = text.split(":", 1)
    label = smart_title_case(label).rstrip(".!?;: ")
    body = sentence_case(body.strip()).rstrip(".!?; ")
    return f"{label}: {body}"


def normalize_english_copy(text: str) -> str:
    text = str(text or "").replace("：", ":").replace("；", ",").replace("×", "x")
    text = text.replace("✕", "x").replace("✖", "x").replace("✗", "x")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\s*/\s*", " / ", text)
    text = re.sub(r"(?<=\d)\s*[xX*]\s*(?=\d)", " x ", text)
    text = re.sub(r"(\d+(?:\.\d+)?)\s*(?:''|’’|′′|\"|”|″)", r"\1 in", text)
    text = re.sub(r"(\d+(?:\.\d+)?)\s*(?:'|’|′)(?!['’′])", r"\1 ft", text)
    text = re.sub(
        r"\b(\d+(?:\.\d+)?)\s*(inches|inch)\b",
        r"\1 in",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(\d+(?:\.\d+)?)\s*(feet|foot)\b",
        r"\1 ft",
        text,
        flags=re.IGNORECASE,
    )
    units = r"(?:in|ft|mm|cm|m|kg|g|lb|lbs|oz|ml|l|gal|v|w|hz|khz|mhz|rpm)"
    text = re.sub(rf"(\d)({units})\b", r"\1 \2", text, flags=re.IGNORECASE)
    text = re.sub(r"(\d)\s+%", r"\1%", text)
    for bad, replacement in SAFE_MARKETING_REPLACEMENTS.items():
        text = re.sub(rf"\b{re.escape(bad)}\b", replacement, text, flags=re.IGNORECASE)
    text = restore_terms(text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def normalize_chinese_copy(text: str) -> str:
    text = str(text or "").replace("×", "x")
    text = re.sub(r"(?<=\d)\s*[xX*]\s*(?=\d)", " x ", text)
    text = re.sub(r"\s*/\s*", " / ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def smart_title_case(text: str) -> str:
    text = normalize_english_copy(text)
    if not text:
        return ""
    words = text.split(" ")
    output = []
    last_index = len(words) - 1
    for index, word in enumerate(words):
        output.append(title_case_token(word, force=index in {0, last_index}))
    return restore_terms(" ".join(output))


def title_case_token(token: str, *, force: bool = False) -> str:
    if not token:
        return token
    if token.lower() == "walk-in":
        return "Walk-In"
    if token.lower() == "multi-sku":
        return "Multi-SKU"
    if token in {"PC", "PE", "PVC", "UV", "LED", "USB", "DIY", "SKU", "A+", "US"}:
        return token
    if re.fullmatch(r"\d+(?:\.\d+)?", token):
        return token
    if re.search(r"\d", token) and re.search(r"[A-Za-z]", token):
        return restore_terms(token)
    prefix = re.match(r"^[^A-Za-z0-9]+", token)
    suffix = re.search(r"[^A-Za-z0-9+]+$", token)
    lead = prefix.group(0) if prefix else ""
    tail = suffix.group(0) if suffix else ""
    core = token[len(lead) : len(token) - len(tail) if tail else len(token)]
    if "-" in core:
        parts = core.split("-")
        return lead + "-".join(title_case_core(part, force=force or i == 0) for i, part in enumerate(parts)) + tail
    return lead + title_case_core(core, force=force) + tail


def title_case_core(core: str, *, force: bool = False) -> str:
    lower = core.lower()
    if not force and lower in SMALL_WORDS:
        return lower
    if lower in RESTORE_TERMS:
        return RESTORE_TERMS[lower]
    return lower[:1].upper() + lower[1:]


def sentence_case(text: str) -> str:
    text = normalize_english_copy(text)
    if not text:
        return ""
    paragraphs = []
    for paragraph in text.split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            paragraphs.append("")
            continue
        lowered = paragraph.lower()
        lowered = restore_terms(lowered)
        lowered = re.sub(
            r"(^|[.!?]\s+)([a-z])",
            lambda match: match.group(1) + match.group(2).upper(),
            lowered,
        )
        paragraphs.append(lowered)
    result = "\n".join(paragraphs).strip()
    return restore_terms(result)


def restore_terms(text: str) -> str:
    for raw, replacement in sorted(RESTORE_TERMS.items(), key=lambda item: len(item[0]), reverse=True):
        if raw in {"in", "m", "g"}:
            pattern = rf"(?<=\d )({re.escape(raw)})(?=\b)"
        else:
            pattern = rf"(?<![A-Za-z]){re.escape(raw)}(?![A-Za-z])"
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text
