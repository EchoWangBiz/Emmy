#!/usr/bin/env python3
"""
run.py —— Emmy 主进程

主链路：
  lark-cli event consume (listener) → 防抖聚合 → ChatDispatcher (按群分发)
    → brain.run (大脑适配器干活) → reply.send (发回原会话)

并发模型：同一个群【串行】（保 session 不被并发写坏），不同群【并行】，全局 Semaphore 限并发上限。

⚠️ 端到端跑通需要两个前提：
  ① 本机所选大脑已登录（默认 claude；也可 --brain codex）
  ② 飞书 app 已配好能收 @消息（机器人能力 + 订阅 im.message.receive_v1 + 长连接 + 发布 + 拉群）
"""
from __future__ import annotations

import asyncio
import argparse
import fcntl
import json
import os
import random
import re
import sys
import time
from urllib.parse import parse_qs, urlparse
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import listener, brain, reply, config, attachments  # noqa: E402

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


def _with_chat_context(chat_id: str, content: str, sender_id: str = "") -> str:
    """注入当前群的配置上下文（角色/表/repo/发言人），Emmy 据此认群、认活、认人。
    发言人 open_id：event 不带姓名，只给 open_id——Emmy 据此用 `im chat.members get
    --member-id-type open_id` 反查姓名（别问对方"你是谁"，这信息框架已经给了）。
    没配该群则只在有 sender_id 时补一行，其余原样返回。"""
    cc = config.chat_config(chat_id)
    lines = []
    if cc:
        lines.append("[当前群上下文]")
        if cc.get("role"):
            lines.append("- 群角色: %s" % cc["role"])
        if cc.get("base_app_token"):
            lines.append("- BUG表 base-token: %s  table-id: %s"
                         % (cc["base_app_token"], cc.get("base_table_id", "")))
        repos = cc.get("repos") or ({"默认": cc.get("repo")} if cc.get("repo") else {})
        if repos:
            lines.append("- 项目仓库: %s" % "，".join("%s=%s" % (n, p) for n, p in repos.items()))
    if sender_id:
        lines.append("- 这条消息的发言人 open_id: %s（要知道是谁本人报的活，用群成员列表反查姓名，别直接问对方是谁）" % sender_id)
    if not lines:
        return content
    return "\n".join(lines) + "\n\n" + content


# ── 配置门禁：群没配过时，Emmy 先对话式问全配置；框架接住结果写 emmy.yaml ──
# Emmy 大脑没有写文件权限，只负责【收集 + 在回复末尾吐出 <EMMY_CONFIG> 块】，
# 真正落盘由这里的框架代码做（只写 emmy.yaml 的 chats[chat_id]，碰不到别的文件）。
_CONFIG_RE = re.compile(r"<EMMY_CONFIG>\s*(\{.*?\})\s*</EMMY_CONFIG>", re.S)
_ALLOWED_KEYS = ("name", "role", "base_app_token", "base_table_id", "repo", "repos",
                 "jenkins_jobs", "initialized")
_URL_RE = re.compile(r"https?://[^\s<>'\"`]+")


def _base_binding_from_url(url: str) -> Optional[dict]:
    """从普通 Base URL 里直接解析 app_token/table_id；Wiki URL 交给 async resolver。"""
    try:
        u = urlparse(url)
    except ValueError:
        return None
    parts = [p for p in u.path.split("/") if p]
    qs = parse_qs(u.query)
    table_id = (qs.get("table") or [""])[0]
    if len(parts) >= 2 and parts[0] == "base" and table_id:
        return {"base_app_token": parts[1], "base_table_id": table_id,
                "source": "base-url"}
    return None


async def _resolve_wiki_base_url(url: str) -> Optional[dict]:
    """用户给 Wiki 里的 Base 链接时，解析真实 bitable obj_token + table query。只读，不改飞书。"""
    try:
        u = urlparse(url)
    except ValueError:
        return None
    parts = [p for p in u.path.split("/") if p]
    table_id = (parse_qs(u.query).get("table") or [""])[0]
    if not (len(parts) >= 2 and parts[0] == "wiki" and table_id):
        return None
    proc = await asyncio.create_subprocess_exec(
        "lark-cli", "wiki", "+node-get", "--node-token", url, "--format", "json",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        print("[run] wiki base 链接解析失败: %s" % err.decode("utf-8", "replace")[:200], flush=True)
        return None
    try:
        d = json.loads(out.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None
    data = (d or {}).get("data") or {}
    if data.get("obj_type") != "bitable" or not data.get("obj_token"):
        return None
    return {"base_app_token": data["obj_token"], "base_table_id": table_id,
            "source": "wiki-url", "wiki_node_token": data.get("node_token", "")}


async def _annotate_base_links(content: str) -> str:
    """把消息里可识别的 Base/Wiki-Base 链接解析结果作为框架可信上下文注入给大脑。"""
    if not content:
        return content
    found = []
    for url in _URL_RE.findall(content):
        direct = _base_binding_from_url(url)
        if direct:
            found.append(direct)
            continue
        via_wiki = await _resolve_wiki_base_url(url)
        if via_wiki:
            found.append(via_wiki)
    if not found:
        return content
    lines = ["【框架已解析到 BUG 多维表格链接，直接使用这些值，不要再说链接格式不对】"]
    for item in found:
        lines.append("- base_app_token=%s, base_table_id=%s（来源: %s%s）" % (
            item["base_app_token"], item["base_table_id"], item["source"],
            ", wiki_node_token=%s" % item["wiki_node_token"] if item.get("wiki_node_token") else ""))
    return content + "\n\n" + "\n".join(lines)


def _is_initialized(cc: dict) -> bool:
    """该群是否已走完 onboarding（emmy.yaml 标了 initialized）。兼容 True / 'true' 字符串。"""
    v = (cc or {}).get("initialized")
    return v is True or str(v).strip().lower() in ("true", "1", "yes", "done")


def _onboard_prompt(chat_id: str, content: str) -> str:
    """群未初始化时的 onboarding 引导：确认意图 → 自检环境 → 一项项补足 → 标记完成。
    配好后框架会标 initialized，以后这个群不再走这套。"""
    tpl = """[入群初始化] 这个群我还没初始化（chat_id=__CID__）。在配好之前我的首要任务是【带大家把这个群一次性配好】，配好我会记下来、以后就不再问。说话照我活泼简洁的风格，别一次甩一大段——按下面的步骤聊着推进，缺啥补啥（已经清楚的别重复问）：

1) 先确认意图：这个群想让我干啥？目前我会【修 BUG】(role=fix-bug)。不是的话就先问清楚。

2) 是修 BUG 的话，要这几样：
   - BUG 多维表格：**有现成的**就给【分享链接】。普通 `.../base/<app_token>?table=<table_id>` 可直接取 token；如果是 `.../wiki/<wiki_node_token>?table=<table_id>`，先用 `emmy-lark wiki +node-get --node-token "<完整链接>"`，返回里 `data.obj_type=bitable` 时 `data.obj_token` 就是 `base_app_token`，URL 里的 `table` 就是 `base_table_id`。框架若已在消息末尾注入「已解析到 BUG 多维表格链接」，直接用那两个值，别再说格式不对。**没有表就我自己建**、不用你动手（见第 3 步）
   - 代码项目的本地【绝对路径】——可能不止一个仓（前端 / 后端），按【模块→路径】分别问清（如 前端=/Users/xxx/llm-platform-web、后端=/Users/xxx/llm-platform）；只有一个仓也行
   - 发布配置：**不要问 Jenkins job 名**。现在发布 job 由各项目自己的 publish skill 管，默认就是项目自己的 dev 发布流程；你只需要提醒群主：要自动发布，项目仓里放好 `.claude/skills/publish/SKILL.md` 或 `.agents/skills/publish/SKILL.md`，并在这台机器上装好/登录好 `jkit`。如果群主说 job 名固定是「项目名_dev」，也不用写进 emmy.yaml。

3) 准备好 BUG 表（用 emmy-lark；已有表用链接里解析的 token，没有就先自建）：
   - **没有现成表 → 我自己建一张**（你确认过要我自建，别再让群主手动建）：
     `emmy-lark base +base-create --name "<群名> BUG表" --table-name "BUG"` —— 记下返回的 `app_token`(=base-token) 和首表 `table_id`。**别加 `--as bot`**（默认身份建，群成员才打得开）；返回里若有 permission_grant/权限提示就照实转告群主。
   - 自检 + 自动补全字段（已有表也走这步）：先 `emmy-lark base +field-list --base-token <t> --table-id <tbl>` 看现有字段，缺的就【自动建】(纯新增、低危)：
     - 文本字段（提问人 / 问题摘要 / 复现/期望/实际 / 负责人 / 修复分支/PR / AI备注 / 待确认问题 / 提问人答复）：`--json '{"name":"AI备注","type":"text"}'`
     - 状态(select)：`--json '{"name":"状态","type":"select","options":[{"name":"待处理"},{"name":"待修复"},{"name":"修复中"},{"name":"待人工确认"},{"name":"待发布"},{"name":"待验收"},{"name":"已验收"},{"name":"不修"}]}'`
     - 附件/截图：`--json '{"name":"附件/截图","type":"attachment"}'`（worker 从这列读截图，必须建）
     - 提交时间：建成**自动「创建时间」** `--json '{"name":"提交时间","type":"created_time"}'`（自动填、不用谁手填）；若该类型报错就退用 `"type":"datetime"`
     - 问题编号：`--json '{"name":"问题编号","type":"auto_number"}'`（系统自动编号；建不了就退用文本，登记时也别自己填编号）
     - 所属模块（可选）：多仓群想给 BUG 标个分类可建 `select`；**但不是必须**——worker 现在多仓会【挂全部仓跨仓修】、不靠它路由，不建也不会卡。
   - ⚠️ 若【状态】已存在但选项不全，我改不了已有字段选项 → 请群主去补成那 8 个
   - 必须齐：状态(8 选项)、提问人、附件/截图、修复分支/PR、AI备注、待确认问题
   - ⚠️ 初始化验收只以 `base +field-list` 为准：已经有 `base_app_token/table_id` 时不要再跑 `base +table-list`。`table-list` 需要额外 `base:table:read`，缺它不代表这张 BUG 表不能用，也绝对不要因此说“表结构读不了”。

4) 表入口检查（非必须、可跳过）：先看群顶部是不是已经能找到这张 BUG 表的入口——不管是【消息 Pin】(`emmy-lark im pins list --chat-id __CID__`)、还是群顶部那排【文档标签页 / 云文档置顶】。只要已经有任一种入口能点开这张表，就别再重复发 / pin，跳过这步即可；确实一个入口都没有时，再发一条表入口消息并 pin 上：
   发 → `emmy-lark im +messages-send --as bot --chat-id __CID__ --msg-type text --content '{"text":"📊 BUG 表在这儿：<表链接>"}'`（记下返回的 message_id）
   pin → `emmy-lark im pins create --chat-id __CID__ --message-id <上一步的 message_id>`
   - 不要为了初始化去跑 `im chat.members get`。群成员读取只用于后续 @ 提问人；读不到就纯文本写名字，不卡初始化。

4.5) 自动发布(可选)：发布到 DEV 由【项目自己的 publish skill】负责，Emmy 不收集 Jenkins job 名、不把 job 名写进配置。请群主确认两件事即可：这台机器装好并登录 `jkit`；每个项目仓里有 `.claude/skills/publish/SKILL.md`（或 `.agents/skills/publish/SKILL.md`），里面写清本项目 dev 发布流程（比如 job 固定为「项目名_dev」）。没配也行——那就只到「待发布」、发布人工来。

5) 全部 OK 后（意图确认 + `field-list` 证明表字段/选项齐 + 群里能找到表入口[已置顶或已有文档标签页即可，没有也不强求] + 仓库路径拿到），在你【那条回复的最末尾】附上这个块（对方看不到，框架会接住写进配置、并标记本群已初始化、以后不再问）：
<EMMY_CONFIG>{"name":"群备注","role":"fix-bug","base_app_token":"...","base_table_id":"...","repos":{"前端":"/绝对/路径","后端":"/绝对/路径"},"initialized":true}</EMMY_CONFIG>
（只有一个仓就 repos 里写一个；不要写 jenkins_jobs，发布流程不再靠 emmy.yaml 存 job 名）
**还没全部搞定就绝对不要吐这个块**（尤其状态选项没补全、repo 没拿到时）。中间每一步都照常用人话跟大家说进展。

对方刚说：__CONTENT__"""
    cc = config.chat_config(chat_id) or {}
    repos = cc.get("repos") or ({"默认": cc.get("repo")} if cc.get("repo") else {})
    if cc.get("base_app_token") or repos:
        # 之前配过但没走完初始化：把已知的告诉 Emmy，别重新问，只补缺的 + 自检表 schema
        content += ("\n\n【这个群之前配过一部分，已知：base_app_token=%s, base_table_id=%s, repos=%s】"
                    "——已知的直接用、别重新问；只补缺的，重点是自检补全表 schema，齐了就吐带 initialized 的块。"
                    % (cc.get("base_app_token") or "(无)", cc.get("base_table_id") or "(无)", repos or "(无)"))
    return tpl.replace("__CID__", chat_id).replace("__CONTENT__", content)


def _maybe_save_config(chat_id: str, text: str) -> tuple:
    """门禁模式下：若 Emmy 回复里带 <EMMY_CONFIG> 块，解析并落盘，再从给用户的回复里抹掉它。
    返回 (清洗后的文本, 本轮是否真的落盘了配置)——调用方据后者判断「这一轮是不是刚把群配好」。"""
    m = _CONFIG_RE.search(text)
    if not m:
        return text, False
    cleaned = (text[:m.start()] + text[m.end():]).strip()
    try:
        cfg = json.loads(m.group(1))
    except Exception as e:  # noqa: BLE001
        print(f"[run] ⚠️ 门禁配置块解析失败: {e}", flush=True)
        return cleaned or text, False  # 至少别把原始 JSON 块发给用户
    allowed = {k: cfg[k] for k in _ALLOWED_KEYS if cfg.get(k)}
    if not allowed.get("role"):
        return cleaned or text, False
    try:
        path = config.set_chat_config(chat_id, allowed)
        print(f"[run] ✓ 已写入群配置 {chat_id} -> {path}: {allowed}", flush=True)
        return (cleaned + "\n\n（配置我记好啦~ 以后这个群直接喊我干活就行 🦊）").strip(), True
    except Exception as e:  # noqa: BLE001
        print(f"[run] ⚠️ 写配置失败: {e}", flush=True)
        return cleaned or text, False


# ── 自动派工：已配置的修 BUG 群里，Emmy 确认并标「待修复」后会在回复末尾吐 <DISPATCH_FIX/> 信号；
#    框架接住 → 后台起 worker 改代码、提 PR、回写状态、@提问人。Emmy 自己【绝不碰代码】。──
_DISPATCH_RE = re.compile(r"<DISPATCH_FIX\s*/?>(?:\s*</DISPATCH_FIX>)?")
# ── 自动发布：用户确认「发布到 DEV」后 Emmy 吐 <PUBLISH/> 信号 → 框架起【发布 worker】
#    （core/publish.py，确定性、不经 claude）把「待发布」的 bugfix 合进 dev、push、jkit 构建 dev。──
# <PUBLISH/> 全发；<PUBLISH scope="前端"/> 只发指定模块（Emmy 据用户「部署前端/后端」意图填）
_PUBLISH_RE = re.compile(r'<PUBLISH(?:\s+scope="([^"]*)")?\s*/?>(?:\s*</PUBLISH>)?')
_fix_workers: dict = {}      # chat_id -> (proc, logf)（防重起）
_publish_workers: dict = {}  # chat_id -> (proc, logf)（防重起）
# 把「查重→占位→起进程」整段按 chat_id 原子化：同群多发言人【并行】跑 handle（队列按 chat:sender 分流），
# 几乎同时说「发布/修」会各自先过 returncode 判断都没起、再各起一个 → 重复 worker。锁按 chat_id（与登记表同维度）。
_spawn_locks: dict = {}      # chat_id -> asyncio.Lock


WORKER_LOG_DIR = os.path.expanduser("~/.emmy/logs")


async def _reap_worker(registry: dict, chat_id: str, proc, logf, label: str) -> None:
    """监督后台 worker：等它退出 → 关日志、从登记表删除（让该群能接新活）、退出码异常则告警。
    没有这个，proc.returncode 永远是 None，该群会被永久判定为「还在忙」、再也派不了。"""
    try:
        rc = await proc.wait()
    except Exception:  # noqa: BLE001
        rc = -1
    try:
        logf.close()
    except Exception:  # noqa: BLE001
        pass
    if registry.get(chat_id) and registry[chat_id][0] is proc:  # 只回收自己这次的
        registry.pop(chat_id, None)
    tag = "✓ 跑完" if rc == 0 else f"⚠️ 异常退出(rc={rc})"   # None/非零都告警，别把退化场景当成功
    print(f"[run] {label}({chat_id}) {tag}", flush=True)


async def _spawn_worker(registry: dict, chat_id: str, script: str, log_prefix: str,
                        emoji: str, extra: list = None) -> bool:
    """后台起一个 worker 子进程（该群已有同类在跑就不重起）。退出由 _reap_worker 回收。
    extra：额外命令行参数（如发布范围）。输出写 ~/.emmy/logs/<prefix>-<chat>.log，方便 tail。"""
    if chat_id not in _spawn_locks:
        _spawn_locks[chat_id] = asyncio.Lock()
    async with _spawn_locks[chat_id]:   # 查重→占位→起进程 原子化，防同群多发言人并发起重复 worker
        cur = registry.get(chat_id)
        if cur is not None and cur[0].returncode is None:
            return False                 # 还在跑，不重起
        os.makedirs(WORKER_LOG_DIR, exist_ok=True)
        log_path = os.path.join(WORKER_LOG_DIR, "%s-%s.log" % (log_prefix, chat_id))
        logf = open(log_path, "a", buffering=1)  # 行缓冲，tail 能实时看到
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", os.path.join(PROJECT_DIR, "core", script), chat_id, *(extra or []),
            cwd=PROJECT_DIR, stdout=logf, stderr=logf)  # -u：无缓冲，日志实时滚
        registry[chat_id] = (proc, logf)
        asyncio.create_task(_reap_worker(registry, chat_id, proc, logf, log_prefix))
        print(f"[run] {emoji} 已为 {chat_id} 起 {log_prefix}（pid={proc.pid}），日志: {log_path}", flush=True)
        return True


async def _dispatch_fix_worker(chat_id: str) -> bool:
    """收到 <DISPATCH_FIX/> → 后台起修复 worker（改码/提 PR/回写/@提问人）。"""
    return await _spawn_worker(_fix_workers, chat_id, "worker.py", "worker", "🛠️")


async def _dispatch_publish(chat_id: str, scope: str = "") -> bool:
    """收到 <PUBLISH/> → 后台起发布 worker（合 dev/jkit 构建/转待验收/通知）。
    scope：发布范围（空=全发；指定如「前端」=只发该模块）。"""
    return await _spawn_worker(_publish_workers, chat_id, "publish.py", "publish", "🚀",
                               extra=[scope] if scope else None)


def _diagnose(res: dict) -> str:
    """大脑出错时在终端打印详细诊断（含原始输出，方便排查），返回给用户的简短文案。"""
    provider, _model = brain.current()
    print(f"[run] ⚠️ {provider} 出错: {res.get('error')} (returncode={res.get('returncode')})", flush=True)
    if res.get("raw_stdout") is not None:
        print(f"[run] ---- {provider} raw stdout ----\n" + (res.get("raw_stdout") or "(空)"), flush=True)
        print(f"[run] ---- {provider} stderr ----\n" + (res.get("raw_stderr") or "(空)"), flush=True)
    return "（出错了：%s）" % (res.get("error") or res.get("text") or "未知")


async def handle(msg: dict, system_prompt: str, system_prompt_p2p: str) -> None:
    chat_id = msg["chat_id"]
    sender_id = msg.get("sender_id") or ""
    content = (msg.get("content") or "").strip()
    file_ids = msg.get("file_message_ids") or []
    fwd_ids = msg.get("forward_message_ids") or []
    reply_ids = msg.get("reply_src_ids") or []
    is_p2p = msg.get("chat_type") == "p2p"
    cc = None if is_p2p else config.chat_config(chat_id)
    at = None if is_p2p else (sender_id or None)   # 群聊回复 @ 回发言人（区分这话是冲谁说的）；私聊不 @

    # 纯空消息（既没文字、又没文件、也没转发记录）→ 温和提示，不 ack、不调 claude
    if not content and not file_ids and not fwd_ids:
        await reply.send(chat_id, EMPTY_TIP, idempotency_key=msg.get("event_id"), at_user_id=at)
        return

    # 注：群聊的「队列播报」第一段由 ChatDispatcher.submit 在入队时已发（私聊不播报），这里直接干活。

    # 文件 / 转发记录内容注入：仅【私聊】或【已配置群】才读；门禁未配置阶段不注入（免得大段内容污染配置收集）
    if file_ids and (is_p2p or cc is not None):
        file_text = await attachments.gather(file_ids)
        if file_text:
            content = (content + "\n\n" + file_text).strip()
    if fwd_ids and (is_p2p or cc is not None):
        fwd_text = await attachments.gather_forwarded(fwd_ids)
        if fwd_text:
            content = (content + "\n\n" + fwd_text).strip()
    # 回复上下文：若这条是「回复」别人某条消息，补上被回复的原消息（让 Emmy 看懂指代）。
    # onboarding 也必须读：用户常回复上一条“表/仓库我都给了”的消息说“再看下”，不读就只剩这三个字。
    if reply_ids:
        reply_text = await attachments.gather_reply_context(reply_ids)
        if reply_text:
            content = (content + "\n\n" + reply_text).strip()

    # 读完文件仍没有任何可用内容（图片/读不了的文件且无文字）→ 温和提示
    if not content:
        await reply.send(chat_id, EMPTY_TIP, idempotency_key=msg.get("event_id"), at_user_id=at)
        return

    # 续聊判定按「群+发言人」：同群不同人各自独立 session（brain 适配器也按发言人派生 session_id）
    conv = _conv_key(msg)
    resume = conv in _seen_chats
    _seen_chats.add(conv)

    # 私聊（p2p）：正常跟 Claude Code 对话——精简 system prompt（不带群里的 BUG 能力）+ 私聊定位
    if is_p2p:
        res = await brain.run(
            P2P_PREFIX + content, chat_id, resume=resume,
            system_prompt=system_prompt_p2p, cwd=PROJECT_DIR, sender_id=sender_id)
        text = _diagnose(res) if res["is_error"] else (res["text"] or "（没有返回内容）")
        await reply.send(chat_id, text, idempotency_key=msg.get("event_id"))
        return

    # 群聊：入群初始化闸（没初始化过 → onboarding 自检引导；初始化完成 → 注入群上下文正常干活）
    inited = _is_initialized(cc)
    if not inited:
        content = await _annotate_base_links(content)
    prompt = _with_chat_context(chat_id, content, sender_id) if inited else _onboard_prompt(chat_id, content)
    res = await brain.run(
        prompt, chat_id, resume=resume, system_prompt=system_prompt, cwd=PROJECT_DIR, sender_id=sender_id)
    if res["is_error"]:
        text = _diagnose(res)
    else:
        text = res["text"] or "（没有返回内容）"
        # 内部信号（派工 / 发布）对用户不可见——任何分支都先无条件剥离，绝不外泄；
        # 是否命中要在剥离前记下来（剥离后就搜不到了）。
        had_dispatch = bool(_DISPATCH_RE.search(text))
        pub_m = _PUBLISH_RE.search(text)
        had_publish = bool(pub_m)
        publish_scope = (pub_m.group(1) or "").strip() if pub_m else ""   # 发布范围（空=全发）
        text = _PUBLISH_RE.sub("", _DISPATCH_RE.sub("", text)).strip()
        if not inited:  # onboarding 模式：接住配置块并落盘（含 initialized 标记）
            text, just_saved = _maybe_save_config(chat_id, text)
            # 刚把修 BUG 群配好这一轮：顺手起一次 worker 扫表——onboarding 期间可能已把
            # 某些 BUG 标了「待修复」（但当时没派工），worker 是扫表型、会把它们一并扫修；
            # 表里没有待修的就静默退出，不会刷屏。
            if just_saved:
                cc2 = config.chat_config(chat_id) or {}
                repos2 = cc2.get("repos") or ({"_": cc2.get("repo")} if cc2.get("repo") else {})
                if _is_initialized(cc2) and cc2.get("role") == "fix-bug" and repos2:
                    if await _dispatch_fix_worker(chat_id):
                        text += "\n\n🛠️ 群配好啦，我顺手把表里待修的过一遍，有就开修、修好挨个通知~"
        else:  # 已初始化群：接住派工 / 发布信号 → 后台起对应 worker
            if had_dispatch:
                started = await _dispatch_fix_worker(chat_id)
                text += ("\n\n🛠️ 代码侧开工啦，修好我来群里通知大家~" if started
                         else "\n\n🛠️ 代码侧已经在忙这个群的活了，这条排上了，修好通知你~")
            if had_publish:
                started = await _dispatch_publish(chat_id, publish_scope)
                rng = ("「%s」" % publish_scope) if publish_scope else "待发布的"
                text += (("\n\n🚀 收到，开始把%s合进 DEV 并构建，跑完群里通知验收~" % rng) if started
                         else "\n\n🚀 这个群的发布已经在跑了，这次不重复触发哈~")
    await reply.send(chat_id, text, idempotency_key=msg.get("event_id"), at_user_id=at)


# ── 消息聚合：飞书连发多条（如一次拖几个文件、或分几段说）会到达成多个独立事件；
#    用 per-chat 的防抖窗口攒一攒，合并成一条再处理 → 一次对话、一次回复。
#    注：一窗口内全是非文本（图片/文件，content 为空）的批次合并后内容为空，由 handle 回一句温和提示。──
AGGREGATE_DELAY = 1.2       # 秒：窗口内同一 chat 的新消息都并进来，最后一条到齐后再触发
AGGREGATE_MAX_WAIT = 8.0    # 秒：硬上限——从该批首条算起最多攒这么久就强制触发，避免持续连发被无限延后


def _conv_key(msg: dict) -> str:
    """会话隔离 key：同一群里按发言人分开（群级数据如表/状态仍共享，但对话上下文/队列各自独立、不串味）。"""
    return "%s:%s" % (msg.get("chat_id", ""), msg.get("sender_id") or "")


def _merge_msgs(msgs: list) -> dict:
    """同一 chat 的多条消息合并成一条：内容按行拼接，回复挂最后一条，幂等键用第一条。
    同时收集文件类消息的 message_id —— 框架据此下载并读出文本内容注入给 Emmy。"""
    base = dict(msgs[-1])
    # merge_forward（飞书「会话记录」卡片）的原始 content 只是占位、不可读 → 不进拼接，
    # 改由 forward_message_ids 用 messages-mget 展开成全文（见 attachments.gather_forwarded）。
    base["content"] = "\n".join(
        c for m in msgs
        for c in ((m.get("content") or "").strip(),)
        if c and m.get("message_type") != "merge_forward")
    base["event_id"] = msgs[0].get("event_id", "")
    base["file_message_ids"] = [
        m["message_id"] for m in msgs
        if m.get("message_type") in _RESOURCE_TYPES and m.get("message_id")]
    base["forward_message_ids"] = [
        m["message_id"] for m in msgs
        if m.get("message_type") == "merge_forward" and m.get("message_id")]
    # 本批所有消息 id —— 框架据此 mget 发现「回复」关系，补齐被回复的原消息（事件不带 reply_to）
    base["reply_src_ids"] = [m["message_id"] for m in msgs if m.get("message_id")]
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
        cid = _conv_key(msg)   # 按「群+发言人」聚合：同群不同人不合并成一条
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
    """按「群+发言人」(conv_key) 分独立串行队列调度：
      - 同一个发言人【串行】（一条处理完再下一条）—— 保住其 claude session 不被并发写坏；
      - 同群不同人 / 不同群【并行】—— 一个人不再阻塞另一个人；
      - 全局 Semaphore 限并发 —— 同时在跑的会话数有上限，超出的排队等。"""

    def __init__(self, handle_fn, system_prompt: str, system_prompt_p2p: str,
                 max_concurrent: int = MAX_CONCURRENT_CHATS) -> None:
        self._handle = handle_fn
        self._sp = system_prompt
        self._sp_p2p = system_prompt_p2p
        self._sem = asyncio.Semaphore(max_concurrent)
        self._queues: dict = {}    # conv_key -> asyncio.Queue
        self._tasks: dict = {}     # conv_key -> asyncio.Task（每个发言人一个串行消费者）
        self._active: set = set()  # 正在跑 handle 的 conv_key（算"几个会话在忙"）

    async def submit(self, msg: dict) -> None:
        key = _conv_key(msg)   # 按「群+发言人」分独立队列：同群不同人各自串行、互不阻塞
        q = self._queues.get(key)
        ahead = q.qsize() if q is not None else 0   # 入队前，自己前面还等着几条
        if q is None:
            q = asyncio.Queue()
            self._queues[key] = q
            self._tasks[key] = asyncio.create_task(self._run_chat(key, q))
        # 群聊 + 有实质内容：刚收到就先「播报队列情况」+ @发言人（私聊/空消息不播报）
        has_content = bool((msg.get("content") or "").strip()) or bool(msg.get("file_message_ids"))
        if msg.get("chat_type") != "p2p" and has_content:
            await reply.send(msg["chat_id"], _queue_ack(ahead, len(self._active)),
                             idempotency_key=(msg.get("event_id") or "") + ":ack",
                             at_user_id=msg.get("sender_id"))
        await q.put(msg)

    async def _run_chat(self, key: str, q: "asyncio.Queue") -> None:
        while True:
            msg = await q.get()
            try:
                async with self._sem:   # 占一个全局并发名额（满了就在这等）
                    self._active.add(key)
                    try:
                        await self._handle(msg, self._sp, self._sp_p2p)
                    finally:
                        self._active.discard(key)
            except Exception as e:      # 单条失败不拖垮该发言人、更不拖垮别人
                print(f"[run] handle error ({key}): {e}", flush=True)
                try:  # 别让用户「没后续」——出错也回一句，至少有反馈（群聊 @ 回发言人）
                    await reply.send(msg["chat_id"], "哎呀我这边卡了一下下，稍后再喊我一次试试？🙏",
                                     idempotency_key=(msg.get("event_id") or "") + ":err",
                                     at_user_id=(msg.get("sender_id") if msg.get("chat_type") != "p2p" else None))
                except Exception:
                    pass
            finally:
                q.task_done()


# ── 单实例锁：同一时间只允许一个 Emmy 监听器在跑 ──
# 两个监听器会【各自】连飞书长连接、每条 @消息都收两遍 → 重复回复、重复派工（worker 还会抢同一
# worktree）。flock 排他锁能跨进程互斥；进程崩溃/退出时 fd 关闭、锁自动释放，不用手动清 PID 文件。
_LOCK_FH = None  # 全局持有锁句柄，进程存活期间别让它被 GC 关掉（关了锁就没了）


def _acquire_single_instance_lock(path: Optional[str] = None):
    """拿到锁返回文件句柄（真值）；已被别的进程占用返回 None。"""
    global _LOCK_FH
    if path is None:
        d = os.path.expanduser("~/.emmy")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "run.lock")
    # 用 "a+" 打开（不截断）——抢锁失败的进程不会把持锁者写的 pid 冲掉；拿到锁后再清空写自己的 pid。
    fh = open(path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    _LOCK_FH = fh
    return fh


def parse_args(argv: Optional[list] = None):
    p = argparse.ArgumentParser(description="Emmy 飞书智能体")
    p.add_argument("--brain", choices=brain.SUPPORTED,
                   help="选择大脑适配器：claude 或 codex。默认读取 EMMY_BRAIN / emmy.yaml defaults.brain / claude")
    p.add_argument("--model", help="覆盖本次启动使用的模型。默认读取 EMMY_MODEL / emmy.yaml defaults")
    return p.parse_args(argv)


async def main(args=None) -> None:
    args = args or parse_args()
    provider, model = brain.configure(args.brain, args.model)
    system_prompt = load_system_prompt()                            # 群聊：人设 + 全部能力
    system_prompt_p2p = load_system_prompt(include_abilities=False)  # 私聊：仅人设，纯 CC 对话

    # 监听 → 防抖聚合 → 按群分发（同群串行、跨群并行、全局限并发）
    dispatcher = ChatDispatcher(handle, system_prompt, system_prompt_p2p)
    debouncer = Debouncer(AGGREGATE_DELAY, dispatcher.submit)

    async def on_message(msg: dict) -> None:
        debouncer.feed(msg)

    model_tip = (" model=%s" % model) if model else ""
    print("🦊 Emmy 启动，开始监听飞书 @消息… brain=%s%s" % (provider, model_tip), flush=True)
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
    _ARGS = parse_args()
    # 单实例闸：已有监听器在跑就别再起第二个（两个会重复处理每条消息、重复派工）
    if _acquire_single_instance_lock() is None:
        print("⚠️ 已经有一个 Emmy 监听器在跑了（~/.emmy/run.lock 被占用），本次不启动。", flush=True)
        print("   想重启的话：先停掉在跑的那个（launchd 用 ./start.sh stop；前台的 Ctrl-C 或 kill 掉），再起一个。", flush=True)
        sys.exit(1)
    try:
        asyncio.run(main(_ARGS))
    except KeyboardInterrupt:
        print("\nEmmy 已停止")
