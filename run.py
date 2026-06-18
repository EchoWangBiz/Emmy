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
from core import listener, claude_runner, reply, config, attachments  # noqa: E402

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

# 收到既没文字又没可读文件时的温和兜底（图片/贴纸/读不了的文件，别静默）
EMPTY_TIP = "我现在只看得懂文字和文本类文件哦~ 图片之类的先用文字跟我说说要干嘛呀 🦊"

# 可能携带可下载文件资源的消息类型（飞书「文字+拖文件」常是 post 富文本，不只 file）
_RESOURCE_TYPES = ("file", "post", "media")

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


# ── 自动派工：已配置的修 BUG 群里，Emmy 确认并标「待修复」后会在回复末尾吐 <DISPATCH_FIX/> 信号；
#    框架接住 → 后台起 worker 改代码、提 PR、回写状态、@提问人。Emmy 自己【绝不碰代码】。──
_DISPATCH_RE = re.compile(r"<DISPATCH_FIX\s*/?>(?:\s*</DISPATCH_FIX>)?")
_fix_workers: dict = {}   # chat_id -> asyncio.subprocess.Process（防重起）


async def _dispatch_fix_worker(chat_id: str) -> bool:
    """收到派工信号 → 后台起 worker 修代码（该群已有 worker 在跑就不重起；worker 会扫所有待修复）。"""
    p = _fix_workers.get(chat_id)
    if p is not None and p.returncode is None:
        return False
    proc = await asyncio.create_subprocess_exec(
        sys.executable, os.path.join(PROJECT_DIR, "core", "worker.py"), chat_id,
        cwd=PROJECT_DIR,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    _fix_workers[chat_id] = proc
    print(f"[run] 🛠️ 已为 {chat_id} 起代码侧 worker（pid={proc.pid}）", flush=True)
    return True


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
    file_ids = msg.get("file_message_ids") or []
    is_p2p = msg.get("chat_type") == "p2p"
    cc = None if is_p2p else config.chat_config(chat_id)

    # 纯空消息（既没文字又没文件）→ 温和提示，不 ack、不调 claude
    if not content and not file_ids:
        await reply.send(chat_id, EMPTY_TIP, idempotency_key=msg.get("event_id"))
        return

    # 群聊：H1 两段式——要干活了先随机秒回一句（私聊不 ack，就像正常跟 CC 对话）
    if not is_p2p:
        await reply.send(chat_id, random.choice(ACK_REPLIES),
                         idempotency_key=(msg.get("event_id") or "") + ":ack")

    # 文件内容注入：仅【私聊】或【已配置群】才读；门禁未配置阶段不注入（免得大段文件污染配置收集）
    if file_ids and (is_p2p or cc is not None):
        file_text = await attachments.gather(file_ids)
        if file_text:
            content = (content + "\n\n" + file_text).strip()

    # 读完文件仍没有任何可用内容（图片/读不了的文件且无文字）→ 温和提示
    if not content:
        await reply.send(chat_id, EMPTY_TIP, idempotency_key=msg.get("event_id"))
        return

    resume = chat_id in _seen_chats
    _seen_chats.add(chat_id)

    # 私聊（p2p）：正常跟 Claude Code 对话——精简 system prompt（不带群里的 BUG 能力）+ 私聊定位
    if is_p2p:
        res = await claude_runner.run(
            P2P_PREFIX + content, chat_id, resume=resume,
            system_prompt=system_prompt_p2p, cwd=PROJECT_DIR)
        text = _diagnose(res) if res["is_error"] else (res["text"] or "（没有返回内容）")
        await reply.send(chat_id, text, idempotency_key=msg.get("event_id"))
        return

    # 群聊：配置门禁（没配过 → 引导收集；配好了 → 注入群上下文正常干活）
    prompt = _with_chat_context(chat_id, content) if cc else _onboard_prompt(chat_id, content)
    res = await claude_runner.run(
        prompt, chat_id, resume=resume, system_prompt=system_prompt, cwd=PROJECT_DIR)
    if res["is_error"]:
        text = _diagnose(res)
    else:
        text = res["text"] or "（没有返回内容）"
        if not cc:  # 门禁模式：接住配置块并落盘
            text = _maybe_save_config(chat_id, text)
        elif _DISPATCH_RE.search(text):  # 已配置群：接住派工信号 → 后台起 worker 修代码
            text = _DISPATCH_RE.sub("", text).strip()
            started = await _dispatch_fix_worker(chat_id)
            text += ("\n\n🛠️ 代码侧开工啦，修好我来群里通知大家~" if started
                     else "\n\n🛠️ 代码侧已经在忙这个群的活了，这条排上了，修好通知你~")
    await reply.send(chat_id, text, idempotency_key=msg.get("event_id"))


# ── 消息聚合：飞书连发多条（如一次拖几个文件、或分几段说）会到达成多个独立事件；
#    用 per-chat 的防抖窗口攒一攒，合并成一条再处理 → 一次对话、一次回复。
#    注：一窗口内全是非文本（图片/文件，content 为空）的批次合并后内容为空，由 handle 回一句温和提示。──
AGGREGATE_DELAY = 1.2       # 秒：窗口内同一 chat 的新消息都并进来，最后一条到齐后再触发
AGGREGATE_MAX_WAIT = 8.0    # 秒：硬上限——从该批首条算起最多攒这么久就强制触发，避免持续连发被无限延后


def _merge_msgs(msgs: list) -> dict:
    """同一 chat 的多条消息合并成一条：内容按行拼接，回复挂最后一条，幂等键用第一条。
    同时收集文件类消息的 message_id —— 框架据此下载并读出文本内容注入给 Emmy。"""
    base = dict(msgs[-1])
    base["content"] = "\n".join(
        c for c in ((m.get("content") or "").strip() for m in msgs) if c)
    base["event_id"] = msgs[0].get("event_id", "")
    base["file_message_ids"] = [
        m["message_id"] for m in msgs
        if m.get("message_type") in _RESOURCE_TYPES and m.get("message_id")]
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
