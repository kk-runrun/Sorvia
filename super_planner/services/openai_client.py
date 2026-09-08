# -*- coding: utf-8 -*-
from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from super_planner.core.config import WORKBENCH_ROOT


@dataclass
class OpenAIClientSettings:
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    source: str = ""
    workbench_defaults_used: bool = False


def _first_env(names: list[str]) -> tuple[str, str]:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value, name
    return "", ""


def _load_workbench_openai_defaults(workbench_root: Path = WORKBENCH_ROOT) -> Dict[str, str]:
    app_path = workbench_root / "app.py"
    if not app_path.exists():
        return {}
    try:
        tree = ast.parse(app_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}

    wanted = {"DEFAULT_API_KEY", "DEFAULT_BASE_URL", "DEFAULT_MODEL_NAME"}
    values: Dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in wanted:
                values[target.id] = node.value.value.strip()
    return values


def get_openai_settings() -> OpenAIClientSettings:
    api_key, api_key_source = _first_env(
        [
            "SUPER_PLANNER_OPENAI_API_KEY",
            "TRANSHUB_OPENAI_API_KEY",
            "QC_AI_API_KEY",
            "OPENAI_API_KEY",
        ]
    )
    base_url, base_url_source = _first_env(
        [
            "SUPER_PLANNER_OPENAI_BASE_URL",
            "TRANSHUB_OPENAI_BASE_URL",
            "QC_AI_BASE_URL",
            "OPENAI_BASE_URL",
        ]
    )
    model, model_source = _first_env(
        [
            "SUPER_PLANNER_RESEARCH_MODEL",
            "SUPER_PLANNER_MODEL",
            "TRANSHUB_MODEL_NAME",
            "QC_AI_MODEL",
            "OPENAI_MODEL",
        ]
    )

    workbench_defaults_used = False
    workbench_defaults = {}
    if not api_key or not base_url or not model:
        workbench_defaults = _load_workbench_openai_defaults()

    if not api_key and workbench_defaults.get("DEFAULT_API_KEY"):
        api_key = workbench_defaults["DEFAULT_API_KEY"]
        api_key_source = "workbench:DEFAULT_API_KEY"
        workbench_defaults_used = True
    if not base_url and workbench_defaults.get("DEFAULT_BASE_URL"):
        base_url = workbench_defaults["DEFAULT_BASE_URL"]
        base_url_source = "workbench:DEFAULT_BASE_URL"
        workbench_defaults_used = True
    if not model:
        if base_url and workbench_defaults.get("DEFAULT_BASE_URL") == base_url and workbench_defaults.get("DEFAULT_MODEL_NAME"):
            model = workbench_defaults["DEFAULT_MODEL_NAME"]
            model_source = "workbench:DEFAULT_MODEL_NAME"
            workbench_defaults_used = True
        else:
            model = "gpt-5"
            model_source = "default:gpt-5"

    sources = [item for item in (api_key_source, base_url_source, model_source) if item]
    return OpenAIClientSettings(
        api_key=api_key,
        base_url=base_url,
        model=model,
        source=", ".join(sources),
        workbench_defaults_used=workbench_defaults_used,
    )


def build_openai_client():
    settings = get_openai_settings()
    if not settings.api_key:
        return None
    try:
        from openai import OpenAI
    except ModuleNotFoundError:
        return None
    kwargs = {"api_key": settings.api_key}
    if settings.base_url:
        kwargs["base_url"] = settings.base_url
    kwargs["timeout"] = 120.0
    return OpenAI(**kwargs)
