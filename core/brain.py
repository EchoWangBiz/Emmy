#!/usr/bin/env python3
"""
core/brain.py —— 大脑适配器选择层

对 run.py 暴露统一接口，启动时可选择 claude / codex。
"""
from __future__ import annotations

import os
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import claude_runner, codex_runner, config

SUPPORTED = ("claude", "codex")
_provider = "claude"
_model = ""


def normalize_provider(name: Optional[str]) -> str:
    n = (name or "").strip().lower()
    aliases = {"claude-code": "claude", "cc": "claude", "codex-cli": "codex"}
    n = aliases.get(n, n)
    if not n:
        return "claude"
    if n not in SUPPORTED:
        raise ValueError("不支持的大脑: %s（可选: %s）" % (name, ", ".join(SUPPORTED)))
    return n


def defaults() -> dict:
    return (config.load_config().get("defaults") or {})


def resolve_provider(cli_value: Optional[str] = None) -> str:
    d = defaults()
    return normalize_provider(cli_value or os.environ.get("EMMY_BRAIN") or d.get("brain") or "claude")


def resolve_model(provider: str, cli_value: Optional[str] = None) -> str:
    d = defaults()
    env = os.environ.get("EMMY_MODEL")
    if cli_value:
        return cli_value
    if env:
        return env
    if provider == "claude":
        return d.get("claude_model") or d.get("model") or ""
    if provider == "codex":
        return d.get("codex_model") or (d.get("model") if d.get("brain") == "codex" else "") or ""
    return ""


def configure(provider: Optional[str] = None, model: Optional[str] = None) -> tuple:
    global _provider, _model
    _provider = resolve_provider(provider)
    _model = resolve_model(_provider, model)
    return _provider, _model


def current() -> tuple:
    return _provider, _model


async def run(*args, **kwargs) -> dict:
    provider, model = current()
    if model:
        kwargs["model"] = model
    if provider == "codex":
        return await codex_runner.run(*args, **kwargs)
    return await claude_runner.run(*args, **kwargs)


async def healthcheck() -> bool:
    provider, _model = current()
    if provider == "codex":
        return await codex_runner.healthcheck()
    return await claude_runner.healthcheck()


def _selftest() -> None:
    assert normalize_provider("") == "claude"
    assert normalize_provider("cc") == "claude"
    assert normalize_provider("codex-cli") == "codex"
    try:
        normalize_provider("x")
    except ValueError:
        pass
    else:
        raise AssertionError("未知 provider 应报错")
    print("✓ brain provider 归一化")

    print("\nbrain 适配器自测通过 ✅")


if __name__ == "__main__":
    _selftest()
