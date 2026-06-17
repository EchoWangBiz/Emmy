#!/usr/bin/env python3
"""
core/listener.py —— 飞书事件监听

跑 `lark-cli event consume im.message.receive_v1 --as bot` 子进程，
逐行读 NDJSON，按 event_id 去重后，把消息交给回调处理。

im.message.receive_v1 事件结构（本机 `lark-cli event schema` 确认）：
  chat_id / chat_type(p2p|group) / content(预渲染文本) / event_id /
  message_id / sender_id / message_type / timestamp
  —— 没有 mentions 字段；群 @机器人 由 scope(group_at_msg) 决定是否推送，
     所以收到的事件即「该 Emmy 处理」的（p2p 直发 + group @机器人）。
"""
from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from typing import Awaitable, Callable, Optional

EVENT_KEY = "im.message.receive_v1"


class Dedup:
    """基于 event_id 的有界去重——飞书 3s 未处理会重推，避免重复回复。"""

    def __init__(self, maxsize: int = 2000) -> None:
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._maxsize = maxsize

    def is_new(self, event_id: str) -> bool:
        if not event_id:
            return True  # 无 id 无法去重，放行（宁可重复也不丢消息）
        if event_id in self._seen:
            return False
        self._seen[event_id] = None
        if len(self._seen) > self._maxsize:
            self._seen.popitem(last=False)
        return True


def parse_line(line: str) -> Optional[dict]:
    """把一行 NDJSON 解析成 dict；脏行返回 None。"""
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def to_message(event: dict) -> Optional[dict]:
    """提取关心的字段；兼容事件直接在顶层或包在 'event' 键下。无 chat_id 视为无效。"""
    src = event.get("event") if isinstance(event.get("event"), dict) else event
    chat_id = src.get("chat_id")
    if not chat_id:
        return None
    return {
        "event_id": src.get("event_id", ""),
        "chat_id": chat_id,
        "chat_type": src.get("chat_type"),
        "content": src.get("content", ""),
        "message_id": src.get("message_id") or src.get("id"),
        "sender_id": src.get("sender_id"),
        "message_type": src.get("message_type"),
    }


async def listen(
    on_message: Callable[[dict], Awaitable[None]],
    *,
    on_ready: Optional[Callable[[], None]] = None,
) -> int:
    """
    启动 event consume 子进程，逐行处理 stdout，去重后回调 on_message。
    返回子进程退出码（由调用方 run.py 决定是否重启 —— supervisor）。
    """
    dedup = Dedup()
    proc = await asyncio.create_subprocess_exec(
        "lark-cli", "event", "consume", EVENT_KEY, "--as", "bot",
        # ★ 保持 stdin 开：lark-cli 把 stdin 的 EOF 当退出信号（为 AI 子进程调用设计）。
        #   launchd 后台跑时父进程 stdin 是 /dev/null（EOF），不给 PIPE 的话 consume 一启动就退出
        #   → supervisor 死循环重启。给个 PIPE 且永不写/不关，stdin 就一直开着；停止用 proc.terminate()。
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if on_ready:
        on_ready()
    assert proc.stdout is not None
    async for raw in proc.stdout:
        event = parse_line(raw.decode("utf-8", "replace"))
        if event is None:
            continue
        msg = to_message(event)
        if msg is None:
            continue
        if not dedup.is_new(msg["event_id"]):
            continue
        try:
            await on_message(msg)
        except Exception as e:  # 单条处理失败不拖垮监听
            print(f"[listener] on_message error: {e}", flush=True)
    return await proc.wait()


# ---------------- 自测（python3 core/listener.py）----------------
def _selftest() -> None:
    # 1) 解析 + 字段提取（按真实 schema 的扁平结构）
    line = (
        '{"type":"im.message.receive_v1","event_id":"evt_1","chat_id":"oc_a",'
        '"chat_type":"group","content":"@Emmy 帮我写周报","message_id":"om_x",'
        '"sender_id":"ou_s","message_type":"text"}'
    )
    ev = parse_line(line)
    assert ev is not None, "parse_line 应解析出对象"
    msg = to_message(ev)
    assert msg is not None and msg["chat_id"] == "oc_a", msg
    assert msg["content"].startswith("@Emmy") and msg["chat_type"] == "group", msg
    print("✓ 解析 + 字段提取")

    # 2) 兼容包在 event 键下的结构
    ev2 = parse_line('{"event":{"chat_id":"oc_b","content":"hi","event_id":"evt_2"}}')
    assert to_message(ev2)["chat_id"] == "oc_b"
    print("✓ 兼容嵌套 event")

    # 3) event_id 去重
    d = Dedup()
    assert d.is_new("evt_1") is True
    assert d.is_new("evt_1") is False, "同 event_id 第二次应判为重复"
    assert d.is_new("") is True, "空 id 放行"
    print("✓ event_id 去重")

    # 4) 脏数据不崩
    assert parse_line("") is None
    assert parse_line("not json") is None
    assert to_message({"foo": "bar"}) is None  # 无 chat_id
    print("✓ 脏数据健壮")

    print("\nlistener 核心逻辑自测全部通过 ✅")


if __name__ == "__main__":
    _selftest()
