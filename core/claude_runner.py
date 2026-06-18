#!/usr/bin/env python3
"""
core/claude_runner.py —— 调用本机 Claude Code（大脑）

以 headless 方式跑 `claude -p`，把飞书来的指令交给 Claude Code 的 agent loop，
让它用 Bash 调 lark-cli 干活，再解析 JSON 结果返回。

设计要点：
  - 会话续聊：同一 feishu chat_id 派生同一确定性 UUID（--session-id / --resume）。
  - 权限：--permission-mode dontAsk + --allowedTools 'Bash(lark-cli:*)'（最小授权，
    高危命令在 --disallowedTools 里显式挡掉；后续 H3 再加受控包装层 + 危险操作确认）。
  - 认证：用订阅 token / 登录态（非 --bare）；healthcheck() 做 heartbeat 预检。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from typing import List, Optional

PIPE = asyncio.subprocess.PIPE

# 固定命名空间：把 feishu chat_id 派生成稳定的 session UUID（跨重启续聊）
_NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")

# 安全护栏：Emmy 只能走包装命令 emmy-lark（高危飞书操作由 core/lark_gate 硬拦）。
# ⚠️ claude 的 allowed/disallowed 只匹配【顶层 Bash 命令字符串】、非 OS 级强制——
#    解释器(python/node/sh)起子进程、或用绝对路径，理论上能绕过。这里 deny 常见逃逸路径
#    把门槛抬高（deny 优先级最高），真正的强隔离需 OS sandbox（见 emmy-dangerous-cmd-gate 备忘）。
_DISALLOWED = [
    "Bash(rm:*)", "Bash(sudo:*)", "Bash(curl:*)", "Bash(ssh:*)", "Bash(git:*)",
    "Bash(lark-cli:*)",                                          # 直连真 lark-cli（绕过包装）
    # 解释器 / shell —— 防起子进程绕过 emmy-lark 调飞书
    "Bash(python:*)", "Bash(python3:*)", "Bash(node:*)", "Bash(deno:*)", "Bash(bun:*)",
    "Bash(ruby:*)", "Bash(perl:*)", "Bash(php:*)",
    "Bash(sh:*)", "Bash(bash:*)", "Bash(zsh:*)", "Bash(fish:*)",
    "Bash(env:*)", "Bash(eval:*)", "Bash(exec:*)", "Bash(xargs:*)", "Bash(nohup:*)",
    "Bash(/*)",                                                  # 绝对路径直调（如 /opt/.../lark-cli）
]


def session_id_for(chat_id: str, system_prompt: str = "") -> str:
    """同一飞书会话 + 同一人设 → 同一确定性 UUID。
    把人设 hash 拌进去：人设/能力一改，session 就换 → Emmy 自动用新人设全新开始，
    不会被旧 session 续聊焊住旧 system prompt（改完重启即生效，不用手删 session）。"""
    h = hashlib.sha1((system_prompt or "").encode("utf-8")).hexdigest()[:12]
    return str(uuid.uuid5(_NS, "%s:%s" % (chat_id, h)))


def build_cmd(
    prompt: str,
    session_id: str,
    *,
    resume: bool,
    system_prompt: str = "",
    model: str = "claude-sonnet-4-6",
    allowed_tools: str = "Bash(emmy-lark:*)",
) -> List[str]:
    """构造 claude -p 命令（纯函数，便于单测）。"""
    cmd = ["claude", "-p", prompt, "--output-format", "json"]
    cmd += (["--resume", session_id] if resume else ["--session-id", session_id])
    if system_prompt:
        cmd += ["--append-system-prompt", system_prompt]
    cmd += ["--permission-mode", "default", "--allowedTools", allowed_tools]
    cmd += ["--disallowedTools", *_DISALLOWED]
    cmd += ["--model", model, "--strict-mcp-config"]
    return cmd


def parse_result(stdout: str) -> dict:
    """解析 claude -p --output-format json 的输出，归一成统一结构。"""
    try:
        d = json.loads(stdout)
    except json.JSONDecodeError:
        return {"ok": False, "is_error": True, "text": "",
                "error": "无法解析 claude 输出", "session_id": None, "cost_usd": 0.0}
    is_error = bool(d.get("is_error"))
    return {
        "ok": not is_error,
        "is_error": is_error,
        "text": d.get("result", ""),
        "session_id": d.get("session_id"),
        "cost_usd": d.get("total_cost_usd", 0.0),
    }


async def _invoke(
    prompt: str, sid: str, *, resume: bool,
    system_prompt: str, cwd: Optional[str], env: Optional[dict], timeout: int,
) -> dict:
    """实际跑一次 claude；总是带回 raw_stderr（自愈判断 + 诊断都要用）。"""
    cmd = build_cmd(prompt, sid, resume=resume, system_prompt=system_prompt)
    # 把项目 bin/ 注入 PATH，让 claude 的 Bash 能找到包装命令 emmy-lark（原 PATH 保留，claude/lark-cli 仍可寻）
    proc_env = dict(env or os.environ)
    if cwd:
        proc_env["PATH"] = os.path.join(cwd, "bin") + os.pathsep + proc_env.get("PATH", "")
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, env=proc_env, stdout=PIPE, stderr=PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return {"ok": False, "is_error": True, "text": "（处理超时，请稍后再试）",
                "error": "timeout", "session_id": sid, "cost_usd": 0.0, "raw_stderr": ""}
    res = parse_result(out.decode("utf-8", "replace"))
    res["raw_stderr"] = (err.decode("utf-8", "replace")[:1500] if err else "")
    if res["is_error"] and res.get("error") == "无法解析 claude 输出":
        res["raw_stdout"] = out.decode("utf-8", "replace")[:1500]
        res["returncode"] = proc.returncode
    return res


async def run(
    prompt: str,
    chat_id: str,
    *,
    resume: bool,
    system_prompt: str = "",
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    timeout: int = 180,
) -> dict:
    """调 claude（带 session 自愈）。

    session_id 按 chat_id 确定性派生；但"是否已建过 session"的判断是内存的，
    重启后会丢 → 可能对已存在 session 误用 --session-id（报 "already in use"），
    或对不存在 session 误用 --resume。这里检测到不一致就自动切换模式重试一次。
    """
    sid = session_id_for(chat_id, system_prompt)
    res = await _invoke(prompt, sid, resume=resume,
                        system_prompt=system_prompt, cwd=cwd, env=env, timeout=timeout)
    stderr = res.get("raw_stderr") or ""
    if res["is_error"] and "already in use" in stderr and not resume:
        # 想新建但 session 已存在 → 改 --resume 续接
        res = await _invoke(prompt, sid, resume=True,
                            system_prompt=system_prompt, cwd=cwd, env=env, timeout=timeout)
    elif res["is_error"] and "No conversation found" in stderr and resume:
        # 想续接但 session 不存在 → 改 --session-id 新建
        res = await _invoke(prompt, sid, resume=False,
                            system_prompt=system_prompt, cwd=cwd, env=env, timeout=timeout)
    return res


async def healthcheck() -> bool:
    """heartbeat：claude -p 'ok' 探认证是否就绪（H4 无人值守认证守护用）。"""
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", "ok", "--output-format", "json", stdout=PIPE, stderr=PIPE)
    out, _ = await proc.communicate()
    return parse_result(out.decode("utf-8", "replace"))["ok"]


# ---------------- 自测（python3 core/claude_runner.py）----------------
def _selftest() -> None:
    # 1) session_id 确定性：同 chat_id 稳定、异 chat_id 不同
    a, b, c = session_id_for("oc_x"), session_id_for("oc_x"), session_id_for("oc_y")
    assert a == b and a != c
    uuid.UUID(a)  # 是合法 UUID
    # 人设变 → session 变（同 chat、不同人设派生不同 session）；同人设稳定
    assert session_id_for("oc_x", "人设A") != session_id_for("oc_x", "人设B")
    assert session_id_for("oc_x", "人设A") == session_id_for("oc_x", "人设A")
    print("✓ session_id 确定性派生（绑 chat_id + 人设 hash）")

    # 2) build_cmd —— 新建会话
    cmd = build_cmd("帮我写周报", "sid1", resume=False, system_prompt="你是 Emmy")
    assert cmd[:4] == ["claude", "-p", "帮我写周报", "--output-format"]
    assert "--session-id" in cmd and "sid1" in cmd and "--resume" not in cmd
    assert "default" in cmd and "Bash(emmy-lark:*)" in cmd   # 合法 permission-mode + 只允许包装命令
    assert "你是 Emmy" in cmd
    # 高危 / 直连 lark-cli / 解释器 / 绝对路径 都进 deny
    assert all(x in cmd for x in ("Bash(rm:*)", "Bash(lark-cli:*)", "Bash(python3:*)", "Bash(sh:*)", "Bash(/*)"))
    print("✓ build_cmd 新建会话 + 权限护栏（emmy-lark 包装 + 解释器/绝对路径逃逸 deny）")

    # 3) build_cmd —— 续聊
    cmd2 = build_cmd("继续", "sid1", resume=True)
    assert "--resume" in cmd2 and "--session-id" not in cmd2
    assert "--append-system-prompt" not in cmd2  # 空 system_prompt 不加
    print("✓ build_cmd 续聊")

    # 4) parse_result —— 成功 / 失败 / 脏数据
    ok = parse_result('{"is_error":false,"result":"已发送","session_id":"s","total_cost_usd":0.012}')
    assert ok["ok"] and ok["text"] == "已发送" and ok["cost_usd"] == 0.012
    err = parse_result('{"is_error":true,"result":"Not logged in · Please run /login"}')
    assert (not err["ok"]) and err["is_error"] and "Not logged in" in err["text"]
    bad = parse_result("garbage not json")
    assert bad["is_error"] and bad["text"] == ""
    print("✓ parse_result 成功/失败/脏数据")

    print("\nclaude_runner 核心逻辑自测全部通过 ✅")


if __name__ == "__main__":
    _selftest()
