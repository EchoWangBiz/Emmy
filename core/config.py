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


# ---------------- 受控写入（配置门禁用）----------------
# 说明：Emmy 大脑【没有写文件权限】，群配置由框架（run.py）在收到 Emmy 收集好的
# 结构化结果后，调用这里写入——只动 emmy.yaml 的 chats[chat_id]，碰不到别的文件。

def _quote(v) -> str:
    """需要时给标量加双引号（含空格/冒号/引号、或首尾空格才加），保证 mini parser 也能读回。"""
    s = str(v)
    if s == "" or s != s.strip() or any(c in s for c in ': "'):
        return '"%s"' % s.replace('"', '\\"')
    return s


def _dump_yaml(cfg: dict) -> str:
    """把 emmy.yaml 这种「chats 嵌套 dict + defaults」结构序列化回 YAML（与 _mini_yaml 配对）。"""
    out = []
    chats = cfg.get("chats") or {}
    out.append("chats:")
    for cid, c in chats.items():
        out.append("  %s:" % cid)
        for k, v in (c or {}).items():
            if isinstance(v, dict):   # 二层嵌套，如 repos: {前端: /p, 后端: /p}
                out.append("    %s:" % k)
                for kk, vv in v.items():
                    out.append("      %s: %s" % (kk, _quote(vv)))
            else:
                out.append("    %s: %s" % (k, _quote(v)))
    defaults = cfg.get("defaults")
    if defaults:
        out.append("defaults:")
        for k, v in defaults.items():
            out.append("  %s: %s" % (k, _quote(v)))
    return "\n".join(out) + "\n"


def set_chat_config(chat_id: str, chat_cfg: dict, path: Optional[str] = None) -> str:
    """把某群的配置写入 emmy.yaml（合并：只更新该群、空值不覆盖）。原子写。返回文件路径。"""
    p = path or _CONFIG_FILE
    full = load_config(p)
    if not isinstance(full, dict):
        full = {}
    full.setdefault("chats", {})
    merged = dict(full["chats"].get(chat_id) or {})
    merged.update({k: v for k, v in (chat_cfg or {}).items() if v not in (None, "")})
    full["chats"][chat_id] = merged
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# Emmy 配置（部分由飞书 @对话自动写入，可手改；本文件不入库）\n")
        f.write(_dump_yaml(full))
    os.replace(tmp, p)
    return p


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

    # 写读往返（临时文件，不碰真 emmy.yaml）：门禁写入 → 重新解析能读回
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), "emmy_cfg_selftest.yaml")
    try:
        set_chat_config("oc_new", {
            "name": "MASS 内测群", "role": "fix-bug",
            "base_app_token": "bascn_demo", "base_table_id": "tbl_demo",
            "repo": "/Users/echo/project/mass",
        }, path=tmp)
        back = load_config(tmp)
        cc2 = chat_config("oc_new", back)
        assert cc2 and cc2["role"] == "fix-bug" and cc2["repo"] == "/Users/echo/project/mass", cc2
        assert cc2["name"] == "MASS 内测群", cc2  # 含空格的值加引号后仍能读回
        # 二次写：合并、不丢已有字段、空值不覆盖
        set_chat_config("oc_new", {"base_table_id": "tbl_changed", "repo": ""}, path=tmp)
        cc3 = chat_config("oc_new", load_config(tmp))
        assert cc3["base_table_id"] == "tbl_changed", cc3       # 改了的生效
        assert cc3["repo"] == "/Users/echo/project/mass", cc3   # 空值没覆盖
        assert cc3["role"] == "fix-bug", cc3                    # 没传的字段还在
        print("✓ set_chat_config 写读往返 + 合并/空值不覆盖")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    print("\nconfig 解析自测全部通过 ✅")


if __name__ == "__main__":
    _selftest()
