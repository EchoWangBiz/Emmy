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
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import listener, claude_runner, reply  # noqa: E402

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SYSTEM_PROMPT_FILE = os.path.join(PROJECT_DIR, "prompts", "emmy_system.md")
ABILITIES_DIR = os.path.join(PROJECT_DIR, "prompts", "abilities")

# 收到消息先秒回一句（H1 两段式）——随机挑一句，更像活泼爱俏皮的小 Emmy
ACK_REPLIES = [
    "好嘞！🦊",
    "好的呀~",
    "Okkk~",
    "收到收到！",
    "嗯嗯，这就来~",
    "马上办！✨",
    "好哒~ 🐾",
    "包在我身上！",
    "在的在的~",
]

# 记录哪些 chat 已开过 session（用于 --resume 续聊）
_seen_chats: set = set()


def load_system_prompt() -> str:
    """人设 + 所有能力模块（prompts/abilities/*.md）拼成 system prompt。
    可插拔能力层：加新能力 = 往 abilities/ 放个 .md，不用改代码。"""
    parts = []
    try:
        with open(SYSTEM_PROMPT_FILE, encoding="utf-8") as f:
            parts.append(f.read())
    except FileNotFoundError:
        pass  # 还没写人设也能跑
    if os.path.isdir(ABILITIES_DIR):
        for name in sorted(os.listdir(ABILITIES_DIR)):
            if name.endswith(".md"):
                with open(os.path.join(ABILITIES_DIR, name), encoding="utf-8") as f:
                    parts.append(f.read())
    return "\n\n---\n\n".join(parts)


async def handle(msg: dict, system_prompt: str) -> None:
    chat_id = msg["chat_id"]
    content = (msg.get("content") or "").strip()
    if not content:
        return
    resume = chat_id in _seen_chats
    _seen_chats.add(chat_id)

    # H1 两段式响应：先随机秒回一句，避免用户以为机器人挂了（也更有 Emmy 的活泼劲儿）
    await reply.send(chat_id, random.choice(ACK_REPLIES),
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
