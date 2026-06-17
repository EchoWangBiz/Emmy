#!/usr/bin/env python3
"""
run.py —— Emmy 主进程

主链路：
  lark-cli event consume (listener) → asyncio.Queue (单消费者串行)
    → claude_runner.run (大脑用 lark-cli 干活) → reply.send (发回原会话)

第一版：单消费者串行（单人单机并发极低，串行天然防同 session 并发写 jsonl）。

⚠️ 端到端跑通需要两个前提：
  ① 本机 claude 已登录（claude / claude setup-token）—— 否则 claude_runner 报未登录
  ② 飞书 app 已配好能收 @消息（机器人能力 + 订阅 im.message.receive_v1 + 长连接 + 发布 + 拉群）
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import listener, claude_runner, reply  # noqa: E402

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SYSTEM_PROMPT_FILE = os.path.join(PROJECT_DIR, "prompts", "emmy_system.md")

# 记录哪些 chat 已开过 session（用于 --resume 续聊）
_seen_chats: set = set()


def load_system_prompt() -> str:
    try:
        with open(SYSTEM_PROMPT_FILE, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""  # 还没写人设也能跑


async def handle(msg: dict, system_prompt: str) -> None:
    chat_id = msg["chat_id"]
    content = (msg.get("content") or "").strip()
    if not content:
        return
    resume = chat_id in _seen_chats
    _seen_chats.add(chat_id)

    # H1 两段式响应：先秒回"处理中"，避免用户以为机器人挂了
    await reply.send(chat_id, "🦊 收到，处理中…",
                     idempotency_key=(msg.get("event_id") or "") + ":ack")

    res = await claude_runner.run(
        content, chat_id, resume=resume, system_prompt=system_prompt, cwd=PROJECT_DIR)
    if res["is_error"]:
        text = "（出错了：%s）" % (res.get("error") or res.get("text") or "未知")
    else:
        text = res["text"] or "（没有返回内容）"
    await reply.send(chat_id, text, idempotency_key=msg.get("event_id"))


async def worker(queue: "asyncio.Queue", system_prompt: str) -> None:
    while True:
        msg = await queue.get()
        try:
            await handle(msg, system_prompt)
        except Exception as e:  # 单条失败不拖垮主进程
            print(f"[run] handle error: {e}", flush=True)
        finally:
            queue.task_done()


async def main() -> None:
    system_prompt = load_system_prompt()
    queue: "asyncio.Queue" = asyncio.Queue()
    asyncio.create_task(worker(queue, system_prompt))  # 单消费者

    async def on_message(msg: dict) -> None:
        await queue.put(msg)

    print("🦊 Emmy 启动，开始监听飞书 @消息…", flush=True)
    # H6 supervisor：event consume 断开/异常就重启
    while True:
        try:
            code = await listener.listen(
                on_message, on_ready=lambda: print("[listener] 长连接就绪", flush=True))
            print(f"[listener] event consume 退出（code={code}），3 秒后重启…", flush=True)
        except Exception as e:
            print(f"[listener] 异常：{e}，3 秒后重启…", flush=True)
        await asyncio.sleep(3)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nEmmy 已停止")
