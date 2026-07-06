#!/usr/bin/env python3
"""
core/codex_runner.py —— 调用本机 Codex CLI（可选大脑适配器）

通过 `codex exec --json` 非交互运行。stdout 是 JSONL 事件流，最终回复从
agent_message item 中取；thread_id 会按（chat_id + sender_id + system_prompt）
记录到 ~/.emmy/codex_sessions.json，下次同会话用 `codex exec resume` 续聊。

注意：Codex CLI 当前没有 Claude Code 那种 allowedTools 精确白名单。本适配器用
read-only sandbox + prompt 约束 + PATH 优先指向 bin/emmy-lark，适合作为可选试验
大脑；聊天侧的强门禁仍以 emmy-lark/lark_gate 为核心。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import uuid
from typing import List, Optional

PIPE = asyncio.subprocess.PIPE

_NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
_SESSION_FILE = os.path.expanduser("~/.emmy/codex_sessions.json")
_CODEX_APP_BIN = "/Applications/Codex.app/Contents/Resources/codex"

CODEX_SYSTEM_PREFIX = (
    "你是 Emmy 的大脑。你收到的最终回复会被框架自动发回当前飞书聊天。\n"
    "飞书操作只能通过项目 PATH 里的 `emmy-lark` 命令完成；不要直接调用 lark-cli，"
    "不要用 python/node/sh 等子进程绕过门禁。"
)


def session_key_for(chat_id: str, system_prompt: str = "", sender_id: str = "") -> str:
    """同一（会话 + 发言人 + 人设）→ 同一稳定 key，用于查 Codex thread_id。"""
    h = hashlib.sha1((system_prompt or "").encode("utf-8")).hexdigest()[:12]
    return str(uuid.uuid5(_NS, "codex:%s:%s:%s" % (chat_id, sender_id, h)))


def _load_sessions(path: str = _SESSION_FILE) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_sessions(sessions: dict, path: str = _SESSION_FILE) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sessions, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _compose_prompt(prompt: str, system_prompt: str = "") -> str:
    parts = [CODEX_SYSTEM_PREFIX]
    if system_prompt:
        parts.append("[系统人设与能力]\n" + system_prompt)
    parts.append("[用户消息]\n" + prompt)
    return "\n\n---\n\n".join(parts)


def codex_bin(env: Optional[dict] = None) -> str:
    """定位 Codex CLI。优先 PATH，macOS 上 fallback 到 Codex.app 内置二进制。"""
    path = (env or os.environ).get("PATH")
    found = shutil.which("codex", path=path)
    if found:
        return found
    if os.path.exists(_CODEX_APP_BIN):
        return _CODEX_APP_BIN
    return "codex"


def build_cmd(
    prompt: str,
    *,
    thread_id: Optional[str] = None,
    cwd: Optional[str] = None,
    model: str = "",
    sandbox: str = "read-only",
    codex_path: str = "codex",
) -> List[str]:
    """构造 codex exec 命令（纯函数，便于单测）。"""
    cmd = [codex_path, "exec", "--sandbox", sandbox]
    if cwd:
        cmd += ["--cd", cwd]
    if model and not thread_id:
        cmd += ["--model", model]
    if thread_id:
        cmd += ["resume", "--json"]
        if model:
            cmd += ["--model", model]
        cmd += [thread_id, prompt]
    else:
        cmd += ["--json", prompt]
    return cmd


def parse_jsonl(stdout: str) -> dict:
    """解析 codex exec --json 的 JSONL 输出，返回统一结果。"""
    thread_id = None
    last_text = ""
    error = ""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "thread.started":
            thread_id = ev.get("thread_id") or thread_id
        if ev.get("type") == "error":
            error = ev.get("message") or ev.get("error") or "codex error"
        item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
        if item.get("type") == "agent_message" and item.get("text") is not None:
            last_text = item.get("text") or last_text
        if ev.get("type") == "turn.failed":
            error = ev.get("error") or error or "codex turn failed"
    return {
        "ok": not bool(error),
        "is_error": bool(error),
        "text": last_text,
        "error": error,
        "session_id": thread_id,
        "cost_usd": 0.0,
    }


async def _invoke(
    prompt: str,
    *,
    thread_id: Optional[str],
    cwd: Optional[str],
    env: Optional[dict],
    timeout: int,
    model: str,
) -> dict:
    proc_env = dict(env or os.environ)
    if cwd:
        proc_env["PATH"] = os.path.join(cwd, "bin") + os.pathsep + proc_env.get("PATH", "")
    cmd = build_cmd(prompt, thread_id=thread_id, cwd=cwd, model=model,
                    codex_path=codex_bin(proc_env))
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, env=proc_env, stdout=PIPE, stderr=PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return {"ok": False, "is_error": True, "text": "（处理超时，请稍后再试）",
                "error": "timeout", "session_id": thread_id, "cost_usd": 0.0, "raw_stderr": ""}
    res = parse_jsonl(out.decode("utf-8", "replace"))
    res["raw_stderr"] = (err.decode("utf-8", "replace")[:1500] if err else "")
    res["returncode"] = proc.returncode
    if proc.returncode != 0 and not res["is_error"]:
        res["ok"] = False
        res["is_error"] = True
        res["error"] = res["raw_stderr"] or "codex exited with rc=%s" % proc.returncode
    if res["is_error"]:
        res["raw_stdout"] = out.decode("utf-8", "replace")[:1500]
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
    sender_id: str = "",
    model: str = "",
) -> dict:
    """调 Codex。resume=True 且找到本地 thread_id 时续聊；否则新开线程。"""
    key = session_key_for(chat_id, system_prompt, sender_id)
    sessions = _load_sessions()
    thread_id = sessions.get(key) if resume else None
    res = await _invoke(_compose_prompt(prompt, system_prompt), thread_id=thread_id,
                        cwd=cwd, env=env, timeout=timeout, model=model)
    if not res["is_error"] and res.get("session_id"):
        sessions[key] = res["session_id"]
        _save_sessions(sessions)
    elif res["is_error"] and thread_id and ("not found" in (res.get("error") or "").lower()
                                            or "no session" in (res.get("error") or "").lower()):
        sessions.pop(key, None)
        _save_sessions(sessions)
        res = await _invoke(_compose_prompt(prompt, system_prompt), thread_id=None,
                            cwd=cwd, env=env, timeout=timeout, model=model)
        if not res["is_error"] and res.get("session_id"):
            sessions[key] = res["session_id"]
            _save_sessions(sessions)
    return res


async def healthcheck() -> bool:
    path = codex_bin()
    proc = await asyncio.create_subprocess_exec(
        path, "exec", "--json", "--sandbox", "read-only", "--skip-git-repo-check",
        "Return exactly ok.", stdout=PIPE, stderr=PIPE)
    out, _ = await proc.communicate()
    return proc.returncode == 0 and not parse_jsonl(out.decode("utf-8", "replace"))["is_error"]


def _selftest() -> None:
    key = session_key_for("oc_x", "人设", "ou_a")
    assert key == session_key_for("oc_x", "人设", "ou_a")
    assert key != session_key_for("oc_x", "人设", "ou_b")
    uuid.UUID(key)
    print("✓ codex session key 稳定派生")

    cmd = build_cmd("hi", cwd="/tmp/repo", model="gpt-5.4")
    assert cmd[:4] == ["codex", "exec", "--sandbox", "read-only"]
    assert "--cd" in cmd and "/tmp/repo" in cmd and "--model" in cmd and "--json" in cmd
    assert "resume" not in cmd
    cmd2 = build_cmd("again", thread_id="tid", cwd="/tmp/repo")
    assert "resume" in cmd2 and "tid" in cmd2 and "again" in cmd2
    cmd3 = build_cmd("hi", codex_path="/x/codex")
    assert cmd3[0] == "/x/codex"
    assert codex_bin({"PATH": "/no/such/path"}) in ("codex", _CODEX_APP_BIN)
    print("✓ codex build_cmd 新建/续聊")

    out = "\n".join([
        '{"type":"thread.started","thread_id":"t1"}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
        '{"type":"turn.completed"}',
    ])
    parsed = parse_jsonl(out)
    assert parsed["ok"] and parsed["text"] == "done" and parsed["session_id"] == "t1"
    err = parse_jsonl('{"type":"error","message":"bad"}')
    assert err["is_error"] and err["error"] == "bad"
    print("✓ codex JSONL 解析")

    prompt = _compose_prompt("用户说 hi", "系统")
    assert "emmy-lark" in prompt and "用户说 hi" in prompt and "系统" in prompt
    print("✓ codex prompt 拼装")

    print("\ncodex_runner 核心逻辑自测全部通过 ✅")


if __name__ == "__main__":
    _selftest()
