#!/usr/bin/env python3
"""
core/config.py —— 读 emmy.yaml 配置

给 Emmy 提供「当前群的角色 + 资源绑定」：
    chat_id -> {role, base_app_token, base_table_id, repo, ...}
没有 emmy.yaml、或没配该群 → 返回 None（Emmy 当普通小助理）。

依赖：优先用 pyyaml；没装则用极简兜底解析器（只够解析 emmy.yaml 这种
缩进 dict + 标量 + # 注释 的简单结构，不支持列表/多行/锚点）。
"""
from __future__ import annotations

import os
from typing import Optional

_CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "emmy.yaml"
)


def _scalar(v: str):
    v = v.strip()
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        return v[1:-1]
    return v


def _mini_yaml(text: str) -> dict:
    """极简 YAML：仅支持 emmy.yaml 的嵌套 dict + 标量 + 行内/整行 # 注释。"""
    root: dict = {}
    stack = [(-1, root)]  # (indent, dict)
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()  # 去注释（简单：不处理引号内 #）
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        key, _sep, val = line.strip().partition(":")
        key = key.strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if val.strip() == "":
            d: dict = {}
            parent[key] = d
            stack.append((indent, d))
        else:
            parent[key] = _scalar(val)
    return root


def _parse(text: str) -> dict:
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text) or {}
    except ImportError:
        return _mini_yaml(text)


def load_config(path: Optional[str] = None) -> dict:
    p = path or _CONFIG_FILE
    try:
        with open(p, encoding="utf-8") as f:
            return _parse(f.read())
    except FileNotFoundError:
        return {}


def chat_config(chat_id: str, cfg: Optional[dict] = None) -> Optional[dict]:
    """返回该群的配置 dict（含 role/base_app_token/base_table_id/repo），没配则 None。"""
    cfg = cfg if cfg is not None else load_config()
    chats = (cfg or {}).get("chats") or {}
    return chats.get(chat_id)


# ---------------- 自测（python3 core/config.py）----------------
def _selftest() -> None:
    sample = (
        "chats:\n"
        "  oc_abc:\n"
        "    name: 测试群\n"
        "    role: fix-bug\n"
        "    base_app_token: bascn_x\n"
        "    base_table_id: tbl_x\n"
        "    repo: /Users/echo/project/mass   # 本地路径\n"
        "defaults:\n"
        "  model: claude-sonnet-4-6\n"
    )
    cfg = _parse(sample)
    assert cfg["chats"]["oc_abc"]["role"] == "fix-bug", cfg
    assert cfg["chats"]["oc_abc"]["repo"] == "/Users/echo/project/mass", cfg
    assert cfg["defaults"]["model"] == "claude-sonnet-4-6"
    print("✓ 解析嵌套 + 标量 + 行内注释")

    cc = chat_config("oc_abc", cfg)
    assert cc and cc["base_app_token"] == "bascn_x"
    assert chat_config("oc_unknown", cfg) is None
    print("✓ chat_config 命中 / 未命中")

    assert load_config("/no/such/emmy.yaml") == {}
    print("✓ 无配置文件返回空 dict")

    print("\nconfig 解析自测全部通过 ✅")


if __name__ == "__main__":
    _selftest()
