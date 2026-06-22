#!/usr/bin/env python3
"""
run.py —— Emmy 主进程

主链路：
  lark-cli event consume (listener) → 防抖聚合 → ChatDispatcher (按群分发)
    → claude_runner.run (大脑干活) → reply.send (发回原会话)

并发模型：同一个群【串行】（保 session 不被并发写坏），不同群【并行】，全局 Semaphore 限并发上限。

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

# 收到消息先秒回一句（H1 两段式的第一段）——明确「收到 + 在处理 + 稍等」，让用户知道后面还有正式回复，
# 而不是"在的在的"这种闲聊式让人以为没下文了。
ACK_REPLIES = [
    "收到~ 我看看哈，稍等一下下 🦊",
    "好嘞！这就去办，马上回你~",
    "收到啦！让我瞧瞧，稍等~ 🐾",
    "好的呀~ 我去处理了，一会儿回你",
    "嗯嗯收到，这就来处理，稍等哈 ✨",
    "好哒~ 让我看看，马上回你 🐾",
    "包在我身上！处理中，稍等一下~",
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
    repos = cc.get("repos") or ({"默认": cc.get("repo")} if cc.get("repo") else {})
    if repos:
        lines.append("- 项目仓库: %s" % "，".join("%s=%s" % (n, p) for n, p in repos.items()))
    return "\n".join(lines) + "\n\n" + content


# ── 配置门禁：群没配过时，Emmy 先对话式问全配置；框架接住结果写 emmy.yaml ──
# Emmy 大脑没有写文件权限，只负责【收集 + 在回复末尾吐出 <EMMY_CONFIG> 块】，
# 真正落盘由这里的框架代码做（只写 emmy.yaml 的 chats[chat_id]，碰不到别的文件）。
_CONFIG_RE = re.compile(r"<EMMY_CONFIG>\s*(\{.*?\})\s*</EMMY_CONFIG>", re.S)
_ALLOWED_KEYS = ("name", "role", "base_app_token", "base_table_id", "repo", "repos", "initialized")


def _is_initialized(cc: dict) -> bool:
    """该群是否已走完 onboarding（emmy.yaml 标了 initialized）。兼容 True / 'true' 字符串。"""
    v = (cc or {}).get("initialized")
    return v is True or str(v).strip().lower() in ("true", "1", "yes", "done")


def _onboard_prompt(chat_id: str, content: str) -> str:
    """群未初始化时的 onboarding 引导：确认意图 → 自检环境 → 一项项补足 → 标记完成。
    配好后框架会标 initialized，以后这个群不再走这套。"""
    tpl = """[入群初始化] 这个群我还没初始化（chat_id=__CID__）。在配好之前我的首要任务是【带大家把这个群一次性配好】，配好我会记下来、以后就不再问。说话照我活泼简洁的风格，别一次甩一大段——按下面的步骤聊着推进，缺啥补啥（已经清楚的别重复问）：

1) 先确认意图：这个群想让我干啥？目前我会【修 BUG】(role=fix-bug)。不是的话就先问清楚。

2) 是修 BUG 的话，要这两样：
   - BUG 多维表格的【分享链接】（我自己从 .../base/<app_token>?table=<table_id> 里取 token，不用谁手填）
   - 代码项目的本地【绝对路径】——可能不止一个仓（前端 / 后端），按【模块→路径】分别问清（如 前端=/Users/xxx/llm-platform-web、后端=/Users/xxx/llm-platform）；只有一个仓也行

3) 拿到表链接后，自检 + 自动补全表格（用 emmy-lark，token 用从链接解析出来的）：
   - 先 `emmy-lark base +field-list --base-token <t> --table-id <tbl>` 看现有字段
   - 缺这些就【自动建】(纯新增、低危)：问题编号、提问人、问题摘要、复现/期望/实际、状态、修复分支/PR、AI备注、待确认问题、提问人答复
     文本字段：`emmy-lark base +field-create --base-token <t> --table-id <tbl> --json '{"name":"AI备注","type":"text"}'`
     状态字段(select)：`--json '{"name":"状态","type":"select","options":[{"name":"待处理"},{"name":"待修复"},{"name":"修复中"},{"name":"待人工确认"},{"name":"待发布"},{"name":"待验收"},{"name":"已验收"},{"name":"不修"}]}'`
   - ⚠️ 若【状态】字段已存在但选项不全，我改不了已有字段的选项——这种就明确请群主去多维表格把状态选项补成那 8 个，补好再继续
   - 必须确保齐的：状态(含 8 选项)、修复分支/PR、AI备注、待确认问题

4) 置顶检查：`emmy-lark im pins list --chat-id __CID__` 看群里 pin 了没；没有就发一条表入口消息再 pin 上，方便大家随时点开：
   发 → `emmy-lark im +messages-send --as bot --chat-id __CID__ --msg-type text --content '{"text":"📊 BUG 表在这儿：<表链接>"}'`（记下返回的 message_id）
   pin → `emmy-lark im pins create --chat-id __CID__ --message-id <上一步的 message_id>`

4.5) 扫一眼群里的自动化（详见 base-automation 能力）：`emmy-lark base +workflow-list --base-token <t>` 看有没有、什么状态，简短报给群主（发现空壳/禁用的提一句）。要不要按规范建/改，先问群主、别擅自动。

5) 全部 OK 后（意图确认 + 表字段/选项齐 + 置顶好 + 仓库路径拿到），在你【那条回复的最末尾】附上这个块（对方看不到，框架会接住写进配置、并标记本群已初始化、以后不再问）：
<EMMY_CONFIG>{"name":"群备注","role":"fix-bug","base_app_token":"...","base_table_id":"...","repos":{"前端":"/绝对/路径","后端":"/绝对/路径"},"initialized":true}</EMMY_CONFIG>
（只有一个仓就 repos 里写一个；模块名尽量用表里「所属模块」会出现的值，worker 据此按模块路由）
**还没全部搞定就绝对不要吐这个块**（尤其状态选项没补全、repo 没拿到时）。中间每一步都照常用人话跟大家说进展。

对方刚说：__CONTENT__"""
    cc = config.chat_config(chat_id) or {}
    repos = cc.get("repos") or ({"默认": cc.get("repo")} if cc.get("repo") else {})
    if cc.get("base_app_token") or repos:
        # 之前配过但没走完初始化：把已知的告诉 Emmy，别重新问，只补缺的 + 自检表/自动化
        content += ("\n\n【这个群之前配过一部分，已知：base_app_token=%s, base_table_id=%s, repos=%s】"
                    "——已知的直接用、别重新问；只补缺的，重点是自检补全表 schema + 扫自动化，齐了就吐带 initialized 的块。"
                    % (cc.get("base_app_token") or "(无)", cc.get("base_table_id") or "(无)", repos or "(无)"))
    return tpl.replace("__CID__", chat_id).replace("__CONTENT__", content)


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


WORKER_LOG_DIR = os.path.expanduser("~/.emmy/logs")


async def _reap_worker(chat_id: str, proc, logf) -> None:
    """监督 worker：等它退出 → 关日志、从登记表删除（让该群能接新派工）、退出码异常则告警。
    没有这个，proc.returncode 永远是 None，该群会被永久判定为「还在忙」、再也派不了工。"""
    try:
        rc = await proc.wait()
    except Exception:  # noqa: BLE001
        rc = -1
    try:
        logf.close()
    except Exception:  # noqa: BLE001
        pass
    if _fix_workers.get(chat_id) and _fix_workers[chat_id][0] is proc:  # 只回收自己这次的
        _fix_workers.pop(chat_id, None)
    tag = "✓ 跑完" if rc in (0, None) else f"⚠️ 异常退出(rc={rc})"
    print(f"[run] worker({chat_id}) {tag}", flush=True)


async def _dispatch_fix_worker(chat_id: str) -> bool:
    """收到派工信号 → 后台起 worker（该群已有 worker 在跑就不重起）。退出由 _reap_worker 回收。
    输出写到 ~/.emmy/logs/worker-<chat>.log，方便 tail 观察。"""
    cur = _fix_workers.get(chat_id)
    if cur is not None and cur[0].returncode is None:
        return False                 # 还在跑，不重起
    os.makedirs(WORKER_LOG_DIR, exist_ok=True)
    log_path = os.path.join(WORKER_LOG_DIR, "worker-%s.log" % chat_id)
    logf = open(log_path, "a", buffering=1)  # 行缓冲，tail 能实时看到
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-u", os.path.join(PROJECT_DIR, "core", "worker.py"), chat_id,
        cwd=PROJECT_DIR, stdout=logf, stderr=logf)  # -u：worker 无缓冲，日志实时滚（tail 看得到进度）
    _fix_workers[chat_id] = (proc, logf)
    asyncio.create_task(_reap_worker(chat_id, proc, logf))  # 监督回收，否则该群会卡死派不了工
    print(f"[run] 🛠️ 已为 {chat_id} 起代码侧 worker（pid={proc.pid}），日志: {log_path}", flush=True)
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

    # 注：群聊的「队列播报」第一段由 ChatDispatcher.submit 在入队时已发（私聊不播报），这里直接干活。

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

    # 群聊：入群初始化闸（没初始化过 → onboarding 自检引导；初始化完成 → 注入群上下文正常干活）
    inited = _is_initialized(cc)
    prompt = _with_chat_context(chat_id, content) if inited else _onboard_prompt(chat_id, content)
    res = await claude_runner.run(
        prompt, chat_id, resume=resume, system_prompt=system_prompt, cwd=PROJECT_DIR)
    if res["is_error"]:
        text = _diagnose(res)
    else:
        text = res["text"] or "（没有返回内容）"
        if not inited:  # onboarding 模式：接住配置块并落盘（含 initialized 标记）
            text = _maybe_save_config(chat_id, text)
        elif _DISPATCH_RE.search(text):  # 已初始化群：接住派工信号 → 后台起 worker 修代码
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


# 全局并发上限：同时最多几个群在跑 claude（单机资源有限，别让活跃群太多把机器拖垮）
MAX_CONCURRENT_CHATS = 4


def _queue_ack(ahead: int, active: int) -> str:
    """收到指令时先回的「队列播报」（替代原来的随机俏皮 ack）：排队/繁忙就报实情，闲就活泼。"""
    if ahead > 0:
        return random.choice(["好嘞~", "收到！", "在的~"]) + " 你前面还排着 %d 条，处理完立马到你 🐾" % ahead
    if active >= MAX_CONCURRENT_CHATS:
        return "收到啦~ 这会儿 %d 个群都在找我，排到你了，马上开工！🦊" % active
    return random.choice(ACK_REPLIES)   # 闲：保持小 Emmy 活泼俏皮的劲儿


class ChatDispatcher:
    """按 chat_id 分独立串行队列调度：
      - 同一个群【串行】（一条处理完再下一条）—— 保住该群 claude session 不被并发写坏；
      - 不同群【并行】—— 群 A 不再阻塞群 B；
      - 全局 Semaphore 限并发 —— 同时在跑的群数有上限，超出的排队等。"""

    def __init__(self, handle_fn, system_prompt: str, system_prompt_p2p: str,
                 max_concurrent: int = MAX_CONCURRENT_CHATS) -> None:
        self._handle = handle_fn
        self._sp = system_prompt
        self._sp_p2p = system_prompt_p2p
        self._sem = asyncio.Semaphore(max_concurrent)
        self._queues: dict = {}    # chat_id -> asyncio.Queue
        self._tasks: dict = {}     # chat_id -> asyncio.Task（每个群一个串行消费者）
        self._active: set = set()  # 正在跑 handle 的 chat_id（算"几个群在忙"）

    async def submit(self, msg: dict) -> None:
        cid = msg["chat_id"]
        q = self._queues.get(cid)
        ahead = q.qsize() if q is not None else 0   # 入队前，该群前面还等着几条
        if q is None:
            q = asyncio.Queue()
            self._queues[cid] = q
            self._tasks[cid] = asyncio.create_task(self._run_chat(cid, q))
        # 群聊 + 有实质内容：刚收到就先「播报队列情况」（私聊/空消息不播报）
        has_content = bool((msg.get("content") or "").strip()) or bool(msg.get("file_message_ids"))
        if msg.get("chat_type") != "p2p" and has_content:
            await reply.send(cid, _queue_ack(ahead, len(self._active)),
                             idempotency_key=(msg.get("event_id") or "") + ":ack")
        await q.put(msg)

    async def _run_chat(self, cid: str, q: "asyncio.Queue") -> None:
        while True:
            msg = await q.get()
            try:
                async with self._sem:   # 占一个全局并发名额（满了就在这等）
                    self._active.add(cid)
                    try:
                        await self._handle(msg, self._sp, self._sp_p2p)
                    finally:
                        self._active.discard(cid)
            except Exception as e:      # 单条失败不拖垮该群、更不拖垮别的群
                print(f"[run] handle error ({cid}): {e}", flush=True)
                try:  # 别让用户「没后续」——出错也回一句，至少有反馈
                    await reply.send(cid, "哎呀我这边卡了一下下，稍后再喊我一次试试？🙏",
                                     idempotency_key=(msg.get("event_id") or "") + ":err")
                except Exception:
                    pass
            finally:
                q.task_done()


async def main() -> None:
    system_prompt = load_system_prompt()                            # 群聊：人设 + 全部能力
    system_prompt_p2p = load_system_prompt(include_abilities=False)  # 私聊：仅人设，纯 CC 对话

    # 监听 → 防抖聚合 → 按群分发（同群串行、跨群并行、全局限并发）
    dispatcher = ChatDispatcher(handle, system_prompt, system_prompt_p2p)
    debouncer = Debouncer(AGGREGATE_DELAY, dispatcher.submit)

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
