# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib
import socket
from pathlib import Path


def _ensure_runtime_deps() -> None:
    checks = [
        ("fastapi", "fastapi"),
        ("uvicorn", "uvicorn"),
        ("multipart", "python-multipart"),
        ("openai", "openai"),
    ]
    missing = []
    for module_name, package_name in checks:
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError:
            missing.append(package_name)

    if missing:
        packages = ", ".join(sorted(set(missing)))
        raise RuntimeError(f"缺少运行依赖：{packages}。请先执行：python -m pip install -r requirements.txt")


_ensure_runtime_deps()

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from super_planner.api.routes import router
from super_planner.core.config import FRONTEND_DIR


app = FastAPI(title="超级策划", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router, prefix="/api")
app.mount("/src", StaticFiles(directory=FRONTEND_DIR / "src"), name="src")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/{path_name:path}", include_in_schema=False)
async def spa_fallback(path_name: str) -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


def _find_free_port(start: int = 8601, end: int = 8699) -> int:
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"未找到可用端口：{start}-{end}")


if __name__ == "__main__":
    port = _find_free_port()
    url = f"http://127.0.0.1:{port}"
    print(f"超级策划本地 Demo 已启动：{url}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
