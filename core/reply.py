#!/usr/bin/env python3
"""
core/reply.py —— 把 Emmy 的回复发回飞书会话

封装 `lark-cli im +messages-send --as bot --chat-id <oc_xxx> --text <内容>`。
用 --idempotency-key 防重复发（和 listener 的 event_id 去重形成双保险）。
"""
from __future__ import annotations

import asyncio
import json
from typing import List, Optional

PIPE = asyncio.subprocess.PIPE


def _lark_ok(stdout: bytes) -> bool:
    """从 lark-cli --format json 输出判断是否真成功（ok!=False 且 code in (0,None)）。
    解析不出 JSON 就不当失败（靠 returncode 兜底）。"""
    try:
        d = json.loads(stdout.decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError):
        return True
    if not isinstance(d, dict):
        return True
    return d.get("ok") is not False and d.get("code") in (0, None)


def build_send_cmd(chat_id: str, text: str, *, idempotency_key: Optional[str] = None,
                   at_user_id: Optional[str] = None) -> List[str]:
    """构造发消息命令（纯函数，便于单测）。at_user_id：群聊多人时 @ 回的那个人，区分归属。"""
    if at_user_id:  # 带 @：用 content 富文本（--text 不解析 <at>）
        content = json.dumps({"text": '<at user_id="%s"></at> %s' % (at_user_id, text)}, ensure_ascii=False)
        cmd = ["lark-cli", "im", "+messages-send", "--as", "bot", "--chat-id", chat_id,
               "--msg-type", "text", "--content", content, "--format", "json"]
    else:
        cmd = ["lark-cli", "im", "+messages-send", "--as", "bot", "--chat-id", chat_id,
               "--text", text, "--format", "json"]
    if idempotency_key:
        cmd += ["--idempotency-key", idempotency_key]
    return cmd


async def send(chat_id: str, text: str, *, idempotency_key: Optional[str] = None,
               timeout: int = 30, retries: int = 2, at_user_id: Optional[str] = None) -> bool:
    """发一条文本到指定会话；成功返回 True。空文本直接跳过。
    带有限重试（指数退避）——网络抖动/token 短暂失效时别让用户「没下文」；
    幂等键保证重试不重复发（调用方基本都带 event_id）。at_user_id：群聊 @ 回的人。"""
    if not text or not text.strip():
        return False
    cmd = build_send_cmd(chat_id, text, idempotency_key=idempotency_key, at_user_id=at_user_id)
    last = ""
    for attempt in range(retries + 1):
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            last = "timeout"
        else:
            if proc.returncode == 0 and _lark_ok(out):
                return True
            last = (err.decode("utf-8", "replace")[:200] if err else "rc=%s" % proc.returncode)
        if attempt < retries:
            await asyncio.sleep(1.0 * (attempt + 1))
    print(f"[reply] 发送失败 chat={chat_id}: {last}", flush=True)
    return False


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
    # 带 @：走 content 富文本（--text 不解析 <at>），@ 的 open_id 进 <at>
    cmd3 = build_send_cmd("oc_c", "修好了", at_user_id="ou_x")
    assert "--text" not in cmd3 and "--content" in cmd3
    payload = json.loads(cmd3[cmd3.index("--content") + 1])
    assert payload["text"] == '<at user_id="ou_x"></at> 修好了'
    # _lark_ok：ok=false / code!=0 判失败，其余放行
    assert _lark_ok(b'{"ok":true,"data":{}}') is True
    assert _lark_ok(b'{"ok":false,"msg":"x"}') is False
    assert _lark_ok(b'{"code":99,"msg":"bad"}') is False
    assert _lark_ok(b'{"code":0}') is True
    assert _lark_ok(b'not json') is True   # 解析不出不当失败
    print("reply 命令构造 + _lark_ok 自测通过 ✅")


if __name__ == "__main__":
    _selftest()
