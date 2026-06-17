#!/usr/bin/env python3
"""
core/reply.py —— 把 Emmy 的回复发回飞书会话

封装 `lark-cli im +messages-send --as bot --chat-id <oc_xxx> --text <内容>`。
用 --idempotency-key 防重复发（和 listener 的 event_id 去重形成双保险）。
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

PIPE = asyncio.subprocess.PIPE


def build_send_cmd(chat_id: str, text: str, *, idempotency_key: Optional[str] = None) -> List[str]:
    """构造发消息命令（纯函数，便于单测）。"""
    cmd = [
        "lark-cli", "im", "+messages-send",
        "--as", "bot",
        "--chat-id", chat_id,
        "--text", text,
        "--format", "json",
    ]
    if idempotency_key:
        cmd += ["--idempotency-key", idempotency_key]
    return cmd


async def send(chat_id: str, text: str, *, idempotency_key: Optional[str] = None, timeout: int = 30) -> bool:
    """发一条文本到指定会话；成功返回 True。空文本直接跳过。"""
    if not text or not text.strip():
        return False
    cmd = build_send_cmd(chat_id, text, idempotency_key=idempotency_key)
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
    try:
        await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return False
    return proc.returncode == 0


# ---------------- 自测（python3 core/reply.py）----------------
def _selftest() -> None:
    cmd = build_send_cmd("oc_a", "你好", idempotency_key="evt_1")
    assert cmd[:3] == ["lark-cli", "im", "+messages-send"]
    assert "--as" in cmd and "bot" in cmd
    assert "--chat-id" in cmd and "oc_a" in cmd
    assert "--text" in cmd and "你好" in cmd
    assert "--idempotency-key" in cmd and "evt_1" in cmd
    # 不带幂等键
    cmd2 = build_send_cmd("oc_b", "hi")
    assert "--idempotency-key" not in cmd2
    print("reply 命令构造自测通过 ✅")


if __name__ == "__main__":
    _selftest()
