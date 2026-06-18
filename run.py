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
import json
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import listener, claude_runner, reply, config  # noqa: E402

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

# 私聊场景前缀：明确「私聊 = 正常的编程对话助手」，群里那套 BUG 工单流程别主动触发
P2P_PREFIX = (
    "[私聊模式] 现在是和你单独聊天。你就是个聪明又靠谱的编程对话助手：问啥答啥、"
    "帮看代码、出主意、查问题都行。群里那套「BUG 工单 / 改多维表格 / 状态流转 / 群通知」"
    "流程在私聊里【不要主动触发】，除非对方明确要求。\n\n"
)


def load_system_prompt(include_abilities: bool = True) -> str:
    """人设（+ 可选能力模块 prompts/abilities/*.md）拼成 system prompt。
    可插拔能力层：加新能力 = 往 abilities/ 放个 .md，不用改代码。
    include_abilities=False 用于私聊——只带人设、不带 BUG 工单等群能力，回归纯 CC 对话。"""
    parts = []
    try:
        with open(SYSTEM_PROMPT_FILE, encoding="utf-8") as f:
            parts.append(f.read())
    except FileNotFoundError:
        pass  # 还没写人设也能跑
    if include_abilities and os.path.isdir(ABILITIES_DIR):
        for name in sorted(os.listdir(ABILITIES_DIR)):
            if name.endswith(".md"):
                with open(os.path.join(ABILITIES_DIR, name), encoding="utf-8") as f:
                    parts.append(f.read())
    return "\n\n---\n\n".join(parts)


def _with_chat_context(chat_id: str, content: str) -> str:
    """注入当前群的配置上下文（角色/表/repo），Emmy 据此认群、认活。没配该群则原样返回。"""
    cc = config.chat_config(chat_id)
    if not cc:
        return content
    lines = ["[当前群上下文]"]
    if cc.get("role"):
        lines.append("- 群角色: %s" % cc["role"])
    if cc.get("base_app_token"):
        lines.append("- BUG表 base-token: %s  table-id: %s"
                     % (cc["base_app_token"], cc.get("base_table_id", "")))
    if cc.get("repo"):
        lines.append("- 项目 repo: %s" % cc["repo"])
    return "\n".join(lines) + "\n\n" + content


# ── 配置门禁：群没配过时，Emmy 先对话式问全配置；框架接住结果写 emmy.yaml ──
# Emmy 大脑没有写文件权限，只负责【收集 + 在回复末尾吐出 <EMMY_CONFIG> 块】，
# 真正落盘由这里的框架代码做（只写 emmy.yaml 的 chats[chat_id]，碰不到别的文件）。
_CONFIG_RE = re.compile(r"<EMMY_CONFIG>\s*(\{.*?\})\s*</EMMY_CONFIG>", re.S)
_ALLOWED_KEYS = ("name", "role", "base_app_token", "base_table_id", "repo")


def _onboard_prompt(chat_id: str, content: str) -> str:
    """群未配置时给 Emmy 的引导：对话式问全配置，齐了再吐 <EMMY_CONFIG> 块。"""
    return (
        "[配置门禁] 这个群我还没配置过（chat_id=%s）。配好之前我的首要任务是"
        "【引导大家一次性把配置说清楚】，先别急着干别的活。\n"
        "用我自己活泼的口吻、一次性（别一条条挤牙膏）问全这几样：\n"
        "1) 这个群想让我干啥？目前我会的是【修 BUG】(role=fix-bug)。\n"
        "2) 如果是修 BUG，还要两样：\n"
        "   - BUG 多维表格的【分享链接】发我。链接形如\n"
        "     https://xxx.feishu.cn/base/<app_token>?table=<table_id>&view=...\n"
        "     我自己从链接里取 app_token 和 table_id，不用谁手填。\n"
        "   - 代码项目在电脑上的【绝对路径】（已经 clone 好的那个，例如 /Users/xxx/project/mass）。\n"
        "信息没给齐就继续追问，【绝不瞎编/猜测/填占位符】。\n"
        "等齐全了，在【那一条回复的最末尾】附上这个块（对方看不到它，我的框架会接住写进配置）：\n"
        "<EMMY_CONFIG>{\"name\":\"群备注\",\"role\":\"fix-bug\",\"base_app_token\":\"...\","
        "\"base_table_id\":\"...\",\"repo\":\"/绝对/路径\"}</EMMY_CONFIG>\n"
        "信息还没齐就【绝对不要】输出这个块。\n\n"
        "对方刚说：%s" % (chat_id, content)
    )


def _maybe_save_config(chat_id: str, text: str) -> str:
    """门禁模式下：若 Emmy 回复里带 <EMMY_CONFIG> 块，解析并落盘，再从给用户的回复里抹掉它。"""
    m = _CONFIG_RE.search(text)
    if not m:
        return text
    cleaned = (text[:m.start()] + text[m.end():]).strip()
    try:
        cfg = json.loads(m.group(1))
    except Exception as e:  # noqa: BLE001
        print(f"[run] ⚠️ 门禁配置块解析失败: {e}", flush=True)
        return cleaned or text  # 至少别把原始 JSON 块发给用户
    allowed = {k: cfg[k] for k in _ALLOWED_KEYS if cfg.get(k)}
    if not allowed.get("role"):
        return cleaned or text
    try:
        path = config.set_chat_config(chat_id, allowed)
        print(f"[run] ✓ 已写入群配置 {chat_id} -> {path}: {allowed}", flush=True)
        return (cleaned + "\n\n（配置我记好啦~ 以后这个群直接喊我干活就行 🦊）").strip()
    except Exception as e:  # noqa: BLE001
        print(f"[run] ⚠️ 写配置失败: {e}", flush=True)
        return cleaned or text


def _diagnose(res: dict) -> str:
    """claude 出错时在终端打印详细诊断（含原始输出，方便排查），返回给用户的简短文案。"""
    print(f"[run] ⚠️ claude 出错: {res.get('error')} (returncode={res.get('returncode')})", flush=True)
    if res.get("raw_stdout") is not None:
        print("[run] ---- claude raw stdout ----\n" + (res.get("raw_stdout") or "(空)"), flush=True)
        print("[run] ---- claude stderr ----\n" + (res.get("raw_stderr") or "(空)"), flush=True)
    return "（出错了：%s）" % (res.get("error") or res.get("text") or "未知")


async def handle(msg: dict, system_prompt: str, system_prompt_p2p: str) -> None:
    chat_id = msg["chat_id"]
    content = (msg.get("content") or "").strip()
    if not content:
        # 一窗口全是图片/文件/贴纸这类非文本（飞书预渲染 content 为空）→ 别静默，温和提示一句
        await reply.send(chat_id, "我现在只看得懂文字哦~ 图片/文件先用文字跟我说说要干嘛呀 🦊",
                         idempotency_key=msg.get("event_id"))
        return
    resume = chat_id in _seen_chats
    _seen_chats.add(chat_id)

    # 私聊（p2p）：正常跟 Claude Code 对话——精简 system prompt（不带群里的 BUG 能力）+ 私聊定位，
    # 不门禁、不注入群配置、不秒回 ack，问啥答啥
    if msg.get("chat_type") == "p2p":
        res = await claude_runner.run(
            P2P_PREFIX + content, chat_id, resume=resume,
            system_prompt=system_prompt_p2p, cwd=PROJECT_DIR)
        text = _diagnose(res) if res["is_error"] else (res["text"] or "（没有返回内容）")
        await reply.send(chat_id, text, idempotency_key=msg.get("event_id"))
        return

    # 群聊：H1 两段式——先随机秒回一句（避免以为机器人挂了，也更有 Emmy 的活泼劲儿）
    await reply.send(chat_id, random.choice(ACK_REPLIES),
                     idempotency_key=(msg.get("event_id") or "") + ":ack")

    # 配置门禁：群没配过 → 走引导收集；配好了 → 注入群上下文正常干活
    cc = config.chat_config(chat_id)
    prompt = _with_chat_context(chat_id, content) if cc else _onboard_prompt(chat_id, content)

    res = await claude_runner.run(
        prompt, chat_id, resume=resume, system_prompt=system_prompt, cwd=PROJECT_DIR)
    if res["is_error"]:
        text = _diagnose(res)
    else:
        text = res["text"] or "（没有返回内容）"
        if not cc:  # 仅门禁模式才接住配置块并落盘
            text = _maybe_save_config(chat_id, text)
    await reply.send(chat_id, text, idempotency_key=msg.get("event_id"))


# ── 消息聚合：飞书连发多条（如一次拖几个文件、或分几段说）会到达成多个独立事件；
#    用 per-chat 的防抖窗口攒一攒，合并成一条再处理 → 一次对话、一次回复。
#    注：一窗口内全是非文本（图片/文件，content 为空）的批次合并后内容为空，由 handle 回一句温和提示。──
AGGREGATE_DELAY = 1.2       # 秒：窗口内同一 chat 的新消息都并进来，最后一条到齐后再触发
AGGREGATE_MAX_WAIT = 8.0    # 秒：硬上限——从该批首条算起最多攒这么久就强制触发，避免持续连发被无限延后


def _merge_msgs(msgs: list) -> dict:
    """同一 chat 的多条消息合并成一条：内容按行拼接，回复挂最后一条，幂等键用第一条。"""
    base = dict(msgs[-1])
    base["content"] = "\n".join(
        c for c in ((m.get("content") or "").strip() for m in msgs) if c)
    base["event_id"] = msgs[0].get("event_id", "")
    return base


class Debouncer:
    """同一 chat 短时间内的多条消息聚合成一条再交给下游。
    防抖：后到的消息取消并重置该 chat 的窗口；不同 chat 互不影响。
    硬上限：从该批首条到达算起超过 max_wait 就强制触发，避免持续高频连发被无限延后。"""

    def __init__(self, delay: float, flush, max_wait: float = AGGREGATE_MAX_WAIT) -> None:
        self._delay = delay
        self._max_wait = max_wait
        self._flush = flush          # async (merged_msg) -> None
        self._buf: dict = {}         # chat_id -> [msg]
        self._timers: dict = {}      # chat_id -> asyncio.Task
        self._first_ts: dict = {}    # chat_id -> 该批首条到达的单调时钟

    def feed(self, msg: dict) -> None:
        cid = msg["chat_id"]
        buf = self._buf.setdefault(cid, [])
        if not buf:
            self._first_ts[cid] = time.monotonic()
        buf.append(msg)
        old = self._timers.pop(cid, None)
        if old and not old.done():
            old.cancel()
        # 距首条已超硬上限 → 立即触发（wait=0）；否则重置防抖窗口
        wait = 0.0 if time.monotonic() - self._first_ts[cid] >= self._max_wait else self._delay
        self._timers[cid] = asyncio.create_task(self._fire(cid, wait))

    async def _fire(self, cid: str, wait: float) -> None:
        try:
            if wait:
                await asyncio.sleep(wait)
        except asyncio.CancelledError:
            return  # 窗口内又来了新消息，本次作废，由新定时器接力
        msgs = self._buf.pop(cid, [])
        self._timers.pop(cid, None)
        self._first_ts.pop(cid, None)
        if not msgs:
            return
        try:
            await self._flush(_merge_msgs(msgs))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 —— fire-and-forget task 自己兜底，别变成 "Task exception never retrieved"
            print(f"[debouncer] flush 失败，丢弃 {len(msgs)} 条: {e}", flush=True)


async def worker(queue: "asyncio.Queue", system_prompt: str, system_prompt_p2p: str) -> None:
    while True:
        msg = await queue.get()
        try:
            await handle(msg, system_prompt, system_prompt_p2p)
        except Exception as e:  # 单条失败不拖垮主进程
            print(f"[run] handle error: {e}", flush=True)
        finally:
            queue.task_done()


async def main() -> None:
    system_prompt = load_system_prompt()                            # 群聊：人设 + 全部能力
    system_prompt_p2p = load_system_prompt(include_abilities=False)  # 私聊：仅人设，纯 CC 对话
    queue: "asyncio.Queue" = asyncio.Queue()
    asyncio.create_task(worker(queue, system_prompt, system_prompt_p2p))  # 单消费者

    # 监听 → 防抖聚合 → 入队（连发的多条先并成一条，再交给单消费者）
    debouncer = Debouncer(AGGREGATE_DELAY, queue.put)

    async def on_message(msg: dict) -> None:
        debouncer.feed(msg)

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
