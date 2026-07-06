#!/usr/bin/env python3
"""
core/worker.py —— 修复 worker（代码侧 agent，独立进程）

主链路：读 BUG 表「待修复」→ 定位本地项目 → 开 git worktree → 让 claude 在 worktree 里改代码
        → 提 PR（绝不合主干，停在 PR 等人）→ 回写表格状态（待发布 / 待人工确认）。

【为什么是独立进程】飞书侧 Emmy(run.py) 的 claude 把 Bash(git:*) 挡了、只放 Bash(lark-cli:*)，
  改不了代码。worker 是另一个进程、另一套 allowedTools（放开 git/gh/构建工具，仍挡 rm/sudo）。

【安全红线】① worker 在独立 worktree 改，绝不碰用户工作副本 ② 只提 PR，绝不 merge/push dev|main
            ③ 拿不准 → 不硬改，回 BLOCKED，状态停"待人工确认"等人答。

跑法（端到端，需 emmy.yaml 配好 fix-bug 群的 base/repo）：python core/worker.py <chat_id>
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import claude_runner, config, repo_locate  # noqa: E402

PIPE = asyncio.subprocess.PIPE

# worker 的 claude 权限：放开改代码要用的，仍挡破坏性命令
WORKER_ALLOWED = ("Bash(git:*) Bash(gh:*) Bash(npm:*) Bash(yarn:*) Bash(pnpm:*) Bash(bun:*) "
                  "Bash(python3:*) Bash(node:*) Bash(ls:*) Bash(cat:*) Bash(grep:*) "
                  "Bash(rg:*) Bash(find:*) Bash(tail:*) Bash(head:*) Bash(wc:*) Edit Write Read")
WORKER_DISALLOWED = ["Bash(rm:*)", "Bash(sudo:*)", "Bash(curl:*)", "Bash(ssh:*)", "Bash(git push origin dev:*)"]

STATUS_FIELD = "状态"
# worker 回写要用到的字段 + 状态选项；开工前校验这些齐不齐，缺了就别白跑一趟
NEED_FIELDS = ("状态", "修复分支/PR", "AI备注", "待确认问题")
NEED_STATUS_OPTIONS = ("待修复", "修复中", "待人工确认", "待发布")
# worker 的 worktree 一律开在【目标 repo 外】的 Emmy 自管目录，绝不在目标项目里留临时目录
WORKTREE_BASE = os.path.expanduser("~/.emmy/worktrees")

# 编号→git 分支名：编号来自飞书表格（非受信外部输入），直接拼进分支/refspec 有注入风险
# （`bugfix/x:refs/heads/INJECTED`）。不【拒绝】非纯字母数字的编号（用户常用 NO.001 / BUG-12 / #0001
# 这种合法写法），而是【消毒】成只含字母数字下划线的 slug——任何编号都能用、分支名一定安全；
# 显示/引用仍用原编号（详见 emmy-dangerous-cmd-gate 的「文件/表格内容→argv 间接注入」）。
_UNSAFE_SLUG = re.compile(r"[^A-Za-z0-9_]+")


def branch_slug(num) -> str:
    """把 BUG 编号消毒成安全的 git 分支 slug（只留字母数字下划线）。空/全特殊字符 → 'bug'。纯函数。"""
    s = _UNSAFE_SLUG.sub("_", str(num or "")).strip("_")[:64]
    return s or "bug"


# ---------------- prompt ----------------
def build_fix_prompt(bug: dict, base_branch: str = "dev", screenshots: list = None) -> str:
    """给 worker 的 claude 的修复指引（含 git-workflow 约束 + DONE/BLOCKED 输出契约）。
    screenshots：BUG 截图的本地路径列表——这类前端/UI BUG 往往描述很简（一句话），截图才是问题现场，
    所以让 claude 动手前先用 Read 看图、据图定位（实测能把"盲找半天"变成"直奔问题组件"）。"""
    extra = ("  ⚠️ 这条之前卡在「待人工确认」、提问人已补充：%s —— 按这个补充信息接着修。\n"
             % bug["答复"]) if bug.get("答复") else ""
    shot_block = ""
    if screenshots:
        paths = "\n".join("     - %s" % p for p in screenshots)
        shot_block = (
            "📷【这条 BUG 配了截图，往往就是问题现场的画面】——文字描述可能很简，"
            "**动手前务必先用 Read 工具逐张看这些图**，图里通常直接显示了哪儿不对/该长啥样：\n%s\n\n"
            % paths)
    return (
        "你是代码侧的修复 agent，当前目录是一个 git worktree（基于 %s 切出的独立 bugfix 分支），"
        "你的改动不会影响别人的工作副本，放心改。\n\n"
        "要修的 BUG：\n"
        "  编号: %s\n  摘要: %s\n  详情(复现/期望/实际): %s\n%s\n"
        "%s"  # 截图段（有截图才有）
        "⚠️【无人值守环境，务必照做】你是后台自动跑的、没有人能给你点『批准』：\n"
        "   - 任何需要审批的命令都会被【直接拦住】、把你卡死——【绝对不要】跑测试套件、构建、dev server "
        "（bun/npm/yarn run test、build、dev、start 这类），它们要么被拦、要么太慢会让你超时。\n"
        "   - 验证交给 CI 和 PR review，【不是你这一步要做的】。你只管：定位 → 改最小必要的代码 → 提交 → 推分支。\n"
        "   - bash 命令尽量【单条、别套管道/复合】（`a | b`、`a && b` 里只要有一段不在白名单也会触发审批卡住）。\n\n"
        "【第 0 步：先评估，别埋头瞎找】动手改之前，先花几分钟判断：\n"
        "   a) 看懂这个 BUG 了吗？根因大概在哪？（结合截图 + 读【关键】文件就好，别一上来翻遍全库）\n"
        "   b) 信息/需求够不够动手、有没有歧义？\n"
        "   → 想清楚后，用一两句话先写出你的【根因判断 + 修复计划】，再动手。\n"
        "   → 如果【信息不足 / 需求有歧义 / 拿不准要不要这么改】：【立刻】返回 BLOCKED 把问题问具体"
        "（问得越具体、提问人越好一次答全）——**问一句，远胜过埋头瞎找半小时**。\n"
        "   ⚠️ 如果你已经读了很多文件还没头绪、或在反复兜圈子，这就是『该停下来提问』的信号，别硬扛。\n\n"
        "【绝不白干 = 沉没成本红线】你的时间有限（约 18 分钟），但【无论如何都要留下有用产出】：\n"
        "   - 改完了 → 走下面流程提交推分支、给 DONE。\n"
        "   - 没把握 / 快没时间 / 卡住了 → 【立刻】给 BLOCKED，并写清你已经查明的：根因在哪、可疑文件与函数、"
        "卡在哪一步、你的修复思路。这些线索对接手的人极其有用——【绝不允许『跑了半天啥也没留下』】。\n\n"
        "请按这个流程：\n"
        "1. （第 0 步评估通过、有把握后）在【当前分支】改最小必要的代码修复。\n"
        "2. git add + git commit（message 写清改了啥）。\n"
        "3. 提交后推分支：git push -u origin <当前分支>。\n"
        "   - GitLab 仓库：push 输出里有一行带 merge request 的链接，把它当 PR 链接用；拿不到就写『分支已推，去开 MR』。\n"
        "   - GitHub 仓库（且有 gh）：gh pr create --base %s 提 PR。\n"
        "   ⚠️ 绝不 merge 到 %s、绝不 push %s/main、绝不自己合 MR/PR —— 只到『可 review』就停下等人。\n\n"
        "最后一行必须是下面两种之一(便于我解析)：\n"
        "  DONE: <PR链接> | <一句话改了啥>\n"
        "  BLOCKED: <根因/可疑文件/卡点/你的思路 或 要问提问人的具体问题>\n"
        % (base_branch, bug.get("编号", "?"), bug.get("摘要", ""), bug.get("详情", ""), extra,
           shot_block, base_branch, base_branch, base_branch)
    )


def _safe_seg(s) -> str:
    """模块名→安全的目录段（保留中文/字母数字，其余换 _），用作多仓 worktree 子目录名。"""
    seg = re.sub(r"[^\w一-鿿.-]", "_", str(s or "")).strip("_")
    return seg[:40] or "repo"


def build_fix_prompt_multi(bug: dict, repo_dirs: list, branch: str,
                           base_branch: str = "dev", screenshots: list = None) -> str:
    """多仓修复指引：把该群【所有仓】各挂一个 worktree 子目录，让 claude 跨仓修。
    repo_dirs: [(模块名, 子目录名)] —— 子目录在 cwd 下，各自已在 bugfix 分支上。"""
    extra = ("  ⚠️ 这条之前卡在「待人工确认」、提问人已补充：%s —— 按这个接着修。\n"
             % bug["答复"]) if bug.get("答复") else ""
    shot_block = ""
    if screenshots:
        paths = "\n".join("     - %s" % p for p in screenshots)
        shot_block = ("📷【这条 BUG 配了截图，往往就是问题现场】**动手前先用 Read 逐张看图**：\n%s\n\n" % paths)
    repo_lines = "\n".join("     - %s → 子目录 ./%s（用 `git -C %s ...` 操作这个仓，已在 %s 分支上）"
                           % (m, d, d, branch) for m, d in repo_dirs)
    return (
        "你是代码侧修复 agent。这个群有多个代码仓，我已给【每个仓】各开一个 git worktree，挂在当前目录的子目录下，"
        "你可以【跨仓】一起改（改动不影响别人工作副本，放心改）：\n%s\n\n"
        "要修的 BUG：\n  编号: %s\n  摘要: %s\n  详情(复现/期望/实际): %s\n%s\n"
        "%s"  # 截图段
        "【怎么定位仓】先看【前端/web 那个仓】是不是前端就能解决（UI/样式/渲染/交互/碎图这类）；"
        "如果发现根因要后端配合（接口返回、数据、图片 URL、字段缺失），就【也】去对应服务端仓改——"
        "前后端联动的 bug 两边都改、保持一致。**只改真正需要动的仓，没动的仓【别碰、别提交】。**\n\n"
        "⚠️【无人值守环境】没人能给你点批准：【绝对不要】跑测试/构建/dev server（被拦或超时卡死）；"
        "验证交给 CI 和 PR review。bash 尽量单条、别套管道/复合。\n\n"
        "【第 0 步：先评估】先判断根因在哪个仓、看懂没、有没有歧义；想清楚写一两句【根因判断+计划】再动手。"
        "信息不足/有歧义/拿不准 → 【立刻】返回 BLOCKED 问具体——问一句胜过瞎找半天。\n"
        "【绝不白干】时间有限（约 18 分钟），改完就提交推分支给 DONE；卡住/快超时就【立刻】BLOCKED 并写清"
        "已查明的根因/可疑文件/卡点/思路——绝不允许跑半天啥也没留下。\n\n"
        "请按流程（每个【你改了的】仓都做一遍，没改的仓跳过）：\n"
        "1. 在该仓子目录里改最小必要代码。\n"
        "2. 分两条提交：`git -C <子目录> add -A` 然后 `git -C <子目录> commit -m '改了啥'`。\n"
        "3. 推分支：`git -C <子目录> push -u origin %s`。GitLab 的 push 输出里有 MR 链接（我会自己据分支构造，你不用抄）。\n"
        "   ⚠️ 绝不 merge、绝不 push %s/main、绝不自己合 MR/PR —— 只到『可 review』就停。\n\n"
        "最后一行必须是下面之一（便于我解析）：\n"
        "  DONE: <一句话：改了哪个/哪些仓、各改了啥>\n"
        "  BLOCKED: <根因/可疑文件/卡点/思路 或 要问提问人的具体问题>\n"
        % (repo_lines, bug.get("编号", "?"), bug.get("摘要", ""), bug.get("详情", ""), extra,
           shot_block, branch, base_branch)
    )


def parse_worker_reply(text: str) -> dict:
    """从 claude 最终回复里抠出 DONE/BLOCKED。"""
    for line in reversed((text or "").strip().splitlines()):
        s = line.strip()
        if s.startswith("DONE:"):
            body = s[len("DONE:"):].strip()
            pr, _, note = body.partition("|")
            return {"outcome": "done", "pr": pr.strip(), "note": note.strip()}
        if s.startswith("BLOCKED:"):
            return {"outcome": "blocked", "question": s[len("BLOCKED:"):].strip()}
    return {"outcome": "unknown", "note": (text or "")[:300]}


# ---------------- lark-cli 读写表格 ----------------
def _lark_success(d) -> bool:
    """lark-cli --format json 输出的成功标志（ok!=False 且 code in (0,None)）。"""
    if not isinstance(d, dict):
        return False
    return d.get("ok") is not False and d.get("code") in (0, None)


async def _lark_json(args: list) -> tuple:
    """跑 lark-cli，返回 (ok, data)。ok = returncode==0 且 lark 返回成功；失败打印诊断、绝不静默吞。"""
    proc = await asyncio.create_subprocess_exec("lark-cli", *args, stdout=PIPE, stderr=PIPE)
    out, err = await proc.communicate()
    try:
        d = json.loads(out.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        d = None
    ok = proc.returncode == 0 and _lark_success(d)
    if not ok:
        detail = err.decode("utf-8", "replace")[:200] if err else (str(d)[:200] if d else "(无输出)")
        print("[worker] lark-cli 失败 rc=%s: %s" % (proc.returncode, detail), flush=True)
    return ok, d


def build_list_cmd(base_token: str, table_id: str) -> list:
    return ["base", "+record-list", "--base-token", base_token, "--table-id", table_id, "--format", "json"]


def build_update_cmd(base_token: str, table_id: str, record_ids: list, patch: dict) -> list:
    payload = json.dumps({"record_id_list": record_ids, "patch": patch}, ensure_ascii=False)
    return ["base", "+record-batch-update", "--base-token", base_token, "--table-id", table_id, "--json", payload]


def _flatten(val):
    """把 lark-cli 单元格值规整成简单字符串：['待处理']→'待处理'，[{'text':..}]→文本，None→''。"""
    if val is None:
        return ""
    if isinstance(val, list):
        return ",".join(p for p in (_flatten(v) for v in val) if p)
    if isinstance(val, dict):
        return str(val.get("text") or val.get("name") or val.get("value") or "")
    return str(val)


def _should_fix(fields: dict) -> bool:
    """worker 该处理这条吗：待修复(首次) 或 待人工确认+提问人答复非空(补充后续修，BLOCKED 回路)。"""
    st = str(fields.get(STATUS_FIELD, "")).strip()
    if st == "待修复":
        return True
    return st == "待人工确认" and bool(str(fields.get("提问人答复", "")).strip())


def pending_records(listing: dict) -> list:
    """挑出 worker 该处理的记录（待修复 + 补充答复后待续修的）。纯函数。
    兼容 lark-cli 的【表格式】（data.fields + data.data 二维值 + data.record_id_list）与【items】两种结构。"""
    data = (listing or {}).get("data") or {}
    out = []
    if isinstance(data.get("data"), list) and data.get("fields"):
        fields = data["fields"]
        rids = data.get("record_id_list") or []
        for row, rid in zip(data["data"], rids):
            rf = {name: _flatten(v) for name, v in zip(fields, row)}
            if _should_fix(rf):
                out.append({"record_id": rid, "fields": rf})
        return out
    items = data.get("items") or (listing or {}).get("items") or []
    for it in items:
        fields = it.get("fields") or {}
        if _should_fix(fields):
            out.append({"record_id": it.get("record_id") or it.get("id"), "fields": fields})
    return out


async def read_pending(base_token: str, table_id: str) -> list:
    _ok, listing = await _lark_json(build_list_cmd(base_token, table_id))
    return pending_records(listing or {})


async def write_back(base_token: str, table_id: str, record_id: str, patch: dict) -> bool:
    """回写一条记录；成功返回 True（失败已在 _lark_json 打印，调用方据此决定是否报假成功）。"""
    ok, _ = await _lark_json(build_update_cmd(base_token, table_id, [record_id], patch))
    return ok


def _schema_gaps(field_list_data: dict) -> tuple:
    """从 field-list 的 data 算出 (缺的字段, 状态字段缺的选项)。纯函数，便于单测。"""
    fields = (field_list_data or {}).get("fields") or []
    names = {(f.get("name") or f.get("field_name")) for f in fields}
    missing_fields = [n for n in NEED_FIELDS if n not in names]
    status = next((f for f in fields if (f.get("name") or f.get("field_name")) == STATUS_FIELD), None)
    opts = {o.get("name") for o in (status.get("options") or [])} if status else set()
    missing_options = [o for o in NEED_STATUS_OPTIONS if o not in opts]
    return missing_fields, missing_options


async def check_table_schema(base_token: str, table_id: str) -> tuple:
    """读 field-list，返回 (缺字段, 缺状态选项)——worker 开工前校验，缺了别白跑。"""
    _ok, d = await _lark_json(["base", "+field-list", "--base-token", base_token,
                               "--table-id", table_id, "--format", "json"])
    return _schema_gaps((d or {}).get("data") or {})


# ---------------- 修完回群通知 + @提问人 ----------------
def _at_markup(members_items: list, name: str) -> str:
    """把提问人名字对到群成员 open_id 的 <at> 标记；对不上 / 重名 → 纯文本名（绝不 @ 错人）。"""
    name = str(name or "").strip()
    if not name:
        return ""
    hits = [m for m in (members_items or []) if str(m.get("name", "")).strip() == name]
    if len(hits) == 1:
        oid = hits[0].get("open_id") or hits[0].get("member_id")
        if oid:
            return '<at user_id="%s"></at>' % oid
    return name


def _result_message(at: str, num: str, r: dict) -> str:
    """按修复结果拼一句群通知（@提问人在前；失败也 @ 并带可操作真因，别只埋表格）。"""
    who = (at + " ") if at else ""
    outcome = r.get("result")
    if outcome == "done":
        msg = "%s你提的 #%s 修好啦~ PR：%s，麻烦验收下哈 🛠️" % (who, num, r.get("pr") or "(见表格)")
        if r.get("wrote") is False:   # 代码/PR 都好了，只是表格状态没写进去
            msg += "\n（表格状态我没写进去，群主帮把 #%s 点成「待发布」就行~）" % num
        return msg
    if outcome == "blocked":
        return "%s#%s 我修的时候有个拿不准的地方：%s 帮我确认下哈~" % (who, num, r.get("q") or "见表格「待确认问题」")
    return "%s#%s 这次没跑成 —— %s" % (who, num, r.get("reason") or "我看下日志再来")


async def _send_group(chat_id: str, text: str) -> bool:
    ok, _ = await _lark_json(["im", "+messages-send", "--as", "bot", "--chat-id", chat_id,
                              "--msg-type", "text",
                              "--content", json.dumps({"text": text}, ensure_ascii=False)])
    return ok


async def notify_results(chat_id: str, results: list) -> None:
    """results: [(rec, fix_result)]。逐条发群通知 + @提问人。"""
    _ok, members = await _lark_json(["im", "chat.members", "get", "--chat-id", chat_id, "--format", "json"])
    items = (((members or {}).get("data") or {}).get("items")) or (members or {}).get("items") or []
    for rec, r in results:
        f = rec.get("fields") or {}
        num = _field(f)["编号"]
        asker = f.get("提问人") or f.get("报告人") or f.get("反馈人") or ""
        await _send_group(chat_id, _result_message(_at_markup(items, asker), num, r))
    # 有修好转「待发布」的 → 提一句怎么发布（用户回「发布」即触发自动发布到 DEV）
    if any(r.get("result") == "done" for _, r in results):
        await _send_group(chat_id, "以上修好的都在「待发布」啦~ 要发布到 DEV 的话，跟我说一声「发布」就自动合进 DEV、构建、通知验收 🚀")


# ---------------- worker claude 调用 ----------------
def _session_dir_for(cwd: str) -> str:
    """claude 把 cwd 转义成 ~/.claude/projects/<munged> 存 session（/ . _ 都换成 -）。"""
    munged = re.sub(r"[/._]", "-", cwd)
    return os.path.join(os.path.expanduser("~/.claude/projects"), munged)


def _salvage_claude_analysis(cwd: str, max_chars: int = 500) -> str:
    """claude 被超时强杀（没正常返回）时，从它的 session transcript 里捞出最后几段实质分析，
    免得「跑了十几分钟啥也没留下」（沉没成本）。捞不到就返回空串。"""
    try:
        sdir = _session_dir_for(cwd)
        files = [os.path.join(sdir, f) for f in os.listdir(sdir) if f.endswith(".jsonl")]
        if not files:
            return ""
        latest = max(files, key=os.path.getmtime)
        texts = []
        with open(latest, encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                msg = d.get("message") if isinstance(d.get("message"), dict) else {}
                c = msg.get("content")
                if isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "text":
                            t = str(b.get("text", "")).strip()
                            if len(t) > 30:
                                texts.append(t)
    except OSError:
        return ""
    if not texts:
        return ""
    picked, total = [], 0
    for t in reversed(texts):   # 取最后几段，拼到预算长度
        picked.insert(0, t)
        total += len(t)
        if total >= max_chars:
            break
    return (" … ".join(picked))[:max_chars]


async def _run_claude(prompt: str, cwd: str, timeout: int = 1080) -> dict:
    cmd = ["claude", "-p", prompt, "--output-format", "json",
           "--permission-mode", "default",
           "--allowedTools", WORKER_ALLOWED,
           "--disallowedTools", *WORKER_DISALLOWED,
           "--model", "claude-sonnet-5", "--strict-mcp-config"]
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, stdout=PIPE, stderr=PIPE)
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        # 超时不白干：从 session 捞 claude 最后的分析当线索交回
        return {"is_error": True, "text": "", "error": "timeout",
                "analysis": _salvage_claude_analysis(cwd)}
    return claude_runner.parse_result(out.decode("utf-8", "replace"))


def _field(bug_fields: dict) -> dict:
    """把表格字段映射成 build_fix_prompt 要的 key（容错不同列名）。"""
    f = bug_fields
    return {
        "编号": f.get("问题编号") or f.get("编号") or "?",
        "摘要": f.get("问题摘要") or f.get("摘要") or f.get("问题内容") or "",
        "详情": " | ".join(str(f.get(k, "")) for k in
                          ("复现步骤", "期望", "实际", "复现/期望/实际", "问题内容", "问题类型", "所属模块")
                          if f.get(k)),
        "答复": f.get("提问人答复") or "",   # BLOCKED 续修时把提问人补充并进 prompt
    }


def _pick_repo(repos: dict, module: str):
    """按 BUG 的「所属模块」选 repo 路径。repos: {模块名: 路径}。
    只一个仓 → 直接用；多个 → 按模块名模糊匹配；匹配不出 → None（交 BLOCKED 让提问人指定）。纯函数。"""
    repos = {k: v for k, v in (repos or {}).items() if v}
    if not repos:
        return None
    if len(repos) == 1:
        return next(iter(repos.values()))
    m = (module or "").strip()
    for name, path in repos.items():
        if m and (name in m or m in name):
            return path
    return None


# ---------------- MR/PR 链接（框架确定性构造，不靠 claude 编）----------------
def _mr_url(repo_url: str, branch: str, base: str = "dev") -> str:
    """构造 MR/PR 创建链接。claude 从 push 输出抠链接不可靠（分支已存在时抠不到会瞎编成
    登录页之类），所以由 worker 用 repo URL + 分支名确定性拼。repo_url 是规范化 https://host/group/proj。"""
    from urllib.parse import quote
    b, t = quote(branch, safe=""), quote(base, safe="")
    if "gitlab" in repo_url:
        return ("%s/-/merge_requests/new?merge_request%%5Bsource_branch%%5D=%s"
                "&merge_request%%5Btarget_branch%%5D=%s" % (repo_url, b, t))
    return "%s/compare/%s...%s?expand=1" % (repo_url, t, b)   # GitHub


# ---------------- BUG 截图下载 ----------------
_IMG_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
_ATTACH_BASE = os.path.expanduser("~/.emmy/bug-attachments")


async def download_bug_attachments(base_token: str, table_id: str, record_id: str) -> list:
    """下载这条 BUG 记录的截图附件，返回本地图片【绝对路径】列表（喂给 claude 先看图再修）。
    lark-cli `+record-download-attachment` 的 --output 要求是 cwd 内相对路径（安全限制），
    所以把子进程 cwd 设到目标目录、用 ./。没附件/下载失败都不致命（返回空列表，照常按文字修）。"""
    out_dir = os.path.join(_ATTACH_BASE, record_id)
    os.makedirs(out_dir, exist_ok=True)
    for f in os.listdir(out_dir):  # 清上一轮残留，避免串图
        try:
            os.remove(os.path.join(out_dir, f))
        except OSError:
            pass
    args = ["base", "+record-download-attachment", "--base-token", base_token,
            "--table-id", table_id, "--record-id", record_id, "--output", "./", "--format", "json"]
    try:
        proc = await asyncio.create_subprocess_exec("lark-cli", *args, cwd=out_dir, stdout=PIPE, stderr=PIPE)
        _out, err = await proc.communicate()
    except Exception as e:  # noqa: BLE001
        print("[worker] 下载附件异常: %r" % e, flush=True)
        return []
    if proc.returncode != 0:  # 多半是这条没附件，正常；打一行诊断就好
        print("[worker] 附件下载未成功（可能无附件）rc=%s: %s"
              % (proc.returncode, (err.decode("utf-8", "replace")[:120] if err else "")), flush=True)
        return []
    imgs = [os.path.join(out_dir, f) for f in sorted(os.listdir(out_dir))
            if f.lower().endswith(_IMG_EXTS)]
    return imgs


# ---------------- 单条修复 ----------------
async def fix_one(rec: dict, repo_path: str, base_token: str, table_id: str) -> dict:
    bug = _field(rec.get("fields") or {})
    rid = rec["record_id"]

    loc = repo_locate.locate(repo_path)
    if not loc:
        await write_back(base_token, table_id, rid,
                         {STATUS_FIELD: "待人工确认",
                          "AI备注": "项目定位失败：%s 不是有效 git 仓库" % repo_path})
        return {"id": bug["编号"], "result": "locate-fail",
                "reason": "代码路径好像不对，群主帮我核下 emmy.yaml 的 repo 哈"}

    top = loc["toplevel"]
    branch = "bugfix/%s" % branch_slug(bug["编号"])   # 编号消毒成安全 slug（NO.001→NO_001）
    # worktree 开到目标 repo 外（~/.emmy/worktrees/<repo>/<branch>），不污染目标项目
    repo_key = loc["url"].replace("https://", "").replace("/", "__")
    wt = os.path.join(WORKTREE_BASE, repo_key, branch.replace("/", "-"))
    os.makedirs(os.path.dirname(wt), exist_ok=True)
    repo_locate.remove_worktree(top, wt)  # 清残留
    ok, msg = repo_locate.make_worktree(top, branch, wt, base="dev")
    if not ok:
        await write_back(base_token, table_id, rid,
                         {STATUS_FIELD: "待人工确认", "AI备注": "worktree 创建失败：%s" % msg[:200]})
        return {"id": bug["编号"], "result": "worktree-fail",
                "reason": "我这边开发环境出了点问题（worktree 没建起来）"}

    try:
        # 领单加锁：先把状态改成"修复中"
        await write_back(base_token, table_id, rid, {STATUS_FIELD: "修复中"})
        # 下载 BUG 截图（前端/UI BUG 描述常很简，截图才是问题现场）→ 让 claude 先看图再修
        shots = await download_bug_attachments(base_token, table_id, rid)
        if shots:
            print("[worker] #%s 带 %d 张截图，已下载给 claude 看：%s" % (bug["编号"], len(shots), shots), flush=True)
        res = await _run_claude(build_fix_prompt(bug, screenshots=shots), cwd=wt)
        # 超时不白干：把 claude 临死前的分析当线索交给人，转「待人工确认」（沉没成本红线）
        if res.get("error") == "timeout":
            analysis = (res.get("analysis") or "").strip()
            q = ("这条偏复杂、我在限定时间内没改完。我已经查到的线索：%s" % analysis) if analysis \
                else "这条我在限定时间内没改完、也没留下清晰线索，麻烦人工接手看下哈。"
            await write_back(base_token, table_id, rid,
                             {STATUS_FIELD: "待人工确认", "待确认问题": q[:500],
                              "AI备注": "worker 超时未完成（已尽量留下排查线索）"})
            return {"id": bug["编号"], "result": "blocked", "q": q}
        reply = parse_worker_reply(res.get("text", ""))
        if reply["outcome"] == "done":
            # 先确认分支真推上去了（claude 可能嘴上说 done 但没 push）
            if not repo_locate.branch_on_remote(top, branch):
                await write_back(base_token, table_id, rid,
                                 {STATUS_FIELD: "待人工确认",
                                  "AI备注": ("说改完了但分支没推上去，需人工核实。" + reply.get("note", ""))[:200]})
                return {"id": bug["编号"], "result": "unknown",
                        "reason": "代码改了但分支没推上去，我再看看 / 麻烦人工核实下"}
            pr_url = _mr_url(loc["url"], branch)   # 框架确定性构造，不用 claude 给的
            wrote = await write_back(base_token, table_id, rid,
                                     {STATUS_FIELD: "待发布", "修复分支/PR": pr_url,
                                      "AI备注": reply.get("note", "")})
            return {"id": bug["编号"], "result": "done", "pr": pr_url, "wrote": wrote}
        elif reply["outcome"] == "blocked":
            await write_back(base_token, table_id, rid,
                             {STATUS_FIELD: "待人工确认", "待确认问题": reply.get("question", "")})
            return {"id": bug["编号"], "result": "blocked", "q": reply.get("question")}
        else:
            await write_back(base_token, table_id, rid,
                             {STATUS_FIELD: "待人工确认", "AI备注": "worker 未明确完成：" + reply.get("note", "")[:200]})
            return {"id": bug["编号"], "result": "unknown",
                    "reason": "我处理了一下但没完全搞定，麻烦人工看一眼"}
    finally:
        repo_locate.remove_worktree(top, wt)  # 清 worktree，保留分支(供 PR)


async def fix_one_multi(rec: dict, repos: dict, base_token: str, table_id: str) -> dict:
    """多仓修复：给该群【所有仓】各开一个 worktree（挂同一父目录的子目录），跑一次 claude 跨仓修，
    跑完逐仓检测哪个分支推上去了 → 各自出 PR、回写（修复分支/PR 可能多条）。不靠「所属模块」路由。"""
    bug = _field(rec.get("fields") or {})
    rid = rec["record_id"]
    branch = "bugfix/%s" % branch_slug(bug["编号"])   # 编号消毒成安全 slug
    parent = os.path.join(WORKTREE_BASE, "multi", branch.replace("/", "-"))
    located = []  # [(module, subdir, top, url, wt)]
    for module, path in repos.items():
        loc = repo_locate.locate(path)
        if not loc:
            for _m, _d, _top, _u, _w in located:
                repo_locate.remove_worktree(_top, _w)
            await write_back(base_token, table_id, rid,
                             {STATUS_FIELD: "待人工确认",
                              "AI备注": "项目定位失败：模块「%s」路径 %s 不是有效 git 仓库" % (module, path)})
            return {"id": bug["编号"], "result": "locate-fail",
                    "reason": "模块「%s」的代码路径好像不对，群主核下 emmy.yaml" % module}
        subdir = _safe_seg(module)
        wt = os.path.join(parent, subdir)
        repo_locate.remove_worktree(loc["toplevel"], wt)
        ok, msg = repo_locate.make_worktree(loc["toplevel"], branch, wt, base="dev")
        if not ok:
            for _m, _d, _top, _u, _w in located:
                repo_locate.remove_worktree(_top, _w)
            await write_back(base_token, table_id, rid,
                             {STATUS_FIELD: "待人工确认", "AI备注": "worktree 创建失败（%s）：%s" % (module, msg[:150])})
            return {"id": bug["编号"], "result": "worktree-fail", "reason": "我这边开发环境出了点问题"}
        located.append((module, subdir, loc["toplevel"], loc["url"], wt))

    try:
        await write_back(base_token, table_id, rid, {STATUS_FIELD: "修复中"})
        shots = await download_bug_attachments(base_token, table_id, rid)
        prompt = build_fix_prompt_multi(bug, [(m, d) for m, d, _t, _u, _w in located], branch, screenshots=shots)
        res = await _run_claude(prompt, cwd=parent)
        if res.get("error") == "timeout":
            analysis = (res.get("analysis") or "").strip()
            q = ("这条偏复杂、限定时间没改完。已查到的线索：%s" % analysis) if analysis \
                else "这条限定时间没改完、也没留下清晰线索，麻烦人工接手哈。"
            await write_back(base_token, table_id, rid,
                             {STATUS_FIELD: "待人工确认", "待确认问题": q[:500], "AI备注": "worker 超时未完成"})
            return {"id": bug["编号"], "result": "blocked", "q": q}
        reply = parse_worker_reply(res.get("text", ""))
        # 逐仓检测哪个分支真推上去了（= 实际改动的仓）
        fixed = [(m, u) for m, _d, top, u, _w in located if repo_locate.branch_on_remote(top, branch)]
        if fixed:
            pr_text = " | ".join("%s: %s" % (m, _mr_url(u, branch)) for m, u in fixed)
            wrote = await write_back(base_token, table_id, rid,
                                     {STATUS_FIELD: "待发布", "修复分支/PR": pr_text,
                                      "AI备注": ("改了：%s。%s" % ("、".join(m for m, _ in fixed), reply.get("note", "")))[:300]})
            return {"id": bug["编号"], "result": "done", "pr": pr_text, "wrote": wrote}
        # 没有任何仓推上去
        if reply["outcome"] == "blocked":
            await write_back(base_token, table_id, rid,
                             {STATUS_FIELD: "待人工确认", "待确认问题": reply.get("question", "")})
            return {"id": bug["编号"], "result": "blocked", "q": reply.get("question")}
        await write_back(base_token, table_id, rid,
                         {STATUS_FIELD: "待人工确认",
                          "AI备注": ("worker 未推任何分支（说：%s）" % reply.get("note", ""))[:200]})
        return {"id": bug["编号"], "result": "unknown",
                "reason": "我处理了一下但没改出能提交的东西，麻烦人工看一眼"}
    finally:
        for _m, _d, top, _u, wt in located:
            repo_locate.remove_worktree(top, wt)


async def run_worker(chat_id: str) -> list:
    cc = config.chat_config(chat_id)
    if not cc or cc.get("role") != "fix-bug":
        print("chat %s 未配置为 fix-bug 群（检查 emmy.yaml）" % chat_id)
        return []
    base_token, table_id = cc.get("base_app_token"), cc.get("base_table_id")
    # 多 repo：优先 repos(dict 模块名→路径)，兼容旧的单值 repo
    repos = cc.get("repos") or ({"默认": cc.get("repo")} if cc.get("repo") else {})
    repos = {k: v for k, v in repos.items() if v}
    if not (base_token and table_id and repos):
        print("emmy.yaml 缺 base_app_token/base_table_id/repos(或 repo)")
        return []
    # 开工前置硬校验：表格缺字段/缺状态选项就别白跑一趟，回群精确指出让群主补
    missing_f, missing_o = await check_table_schema(base_token, table_id)
    if missing_f or missing_o:
        parts = []
        if missing_f:
            parts.append("缺字段「%s」" % "、".join(missing_f))
        if missing_o:
            parts.append("状态缺选项「%s」" % "、".join(missing_o))
        print("[worker] 表格 schema 不全，未开张:", missing_f, missing_o)
        await _send_group(chat_id, "开工前发现表格还没配齐——%s。群主在多维表格补一下，我就能跑、也能正常回写状态啦 🦊"
                          % "；".join(parts))
        return []
    try:
        paired = []
        seen = set()  # 已处理过的 record_id，防写状态失败时同一条被无限重修
        for _round in range(20):  # 处理完一批后再扫一次，接住期间新标「待修复」的（让"排上了"成真）；上限防失控
            pend = [r for r in await read_pending(base_token, table_id) if r["record_id"] not in seen]
            if not pend:
                break
            print("待修复 %d 条（第 %d 轮）" % (len(pend), _round + 1))
            if _round == 0 and len(pend) > 1:  # 批量任务：先宣布计划
                nums = "、".join("#" + _field(r.get("fields") or {})["编号"] for r in pend)
                await _send_group(chat_id, "我看了下，这批有 %d 条要修（%s）~ 我排着一条条来，每条改完都 @对应的人验收哈 🛠️"
                                  % (len(pend), nums))
            for rec in pend:  # 串行：一条条修，稳
                seen.add(rec["record_id"])
                bug = _field(rec.get("fields") or {})
                await _send_group(chat_id, "#%s 我开始改了，大概几分钟，改完 @你~ 🛠️" % bug["编号"])  # 开工播报
                if len(repos) > 1:   # 多仓：挂【全部仓】跨仓修（先看前端、需要再连服务端），不再靠「所属模块」路由
                    r = await fix_one_multi(rec, repos, base_token, table_id)
                else:                # 单仓：直接用那一个
                    r = await fix_one(rec, next(iter(repos.values())), base_token, table_id)
                paired.append((rec, r))
                print("  ->", r)
        if paired:
            await notify_results(chat_id, paired)  # 修完回群通知 + @提问人（闭环最后一步）
        return [r for _, r in paired]
    except Exception as e:  # noqa: BLE001 —— 别让 worker 崩了群里就停在「开工啦」没下文
        print("[worker] 主流程异常: %r" % e, flush=True)
        try:
            await _send_group(chat_id, "哎呀我这次没跑通，让 Echo 看下 worker 日志哈~ 🙏")
        except Exception:  # noqa: BLE001
            pass
        return []


async def check_ready(chat_id: str) -> None:
    """端到端测试前的就绪自检：配置 / repo / dev分支 / claude / gh / 待修复数。"""
    import shutil
    import subprocess
    print("🔎 worker 就绪自检 — chat %s\n" % chat_id)
    ok = True
    cc = config.chat_config(chat_id)
    if not cc:
        print("  ✗ emmy.yaml 没配这个群（在 chats 下加 %s:）" % chat_id)
        return
    if cc.get("role") != "fix-bug":
        print("  ✗ 该群 role 不是 fix-bug"); ok = False
    miss = [k for k in ("base_app_token", "base_table_id") if not cc.get(k)]
    repos = cc.get("repos") or ({"默认": cc.get("repo")} if cc.get("repo") else {})
    repos = {k: v for k, v in repos.items() if v}
    if not repos:
        miss.append("repos(或 repo)")
    if miss:
        print("  ✗ 群配置缺: %s" % ", ".join(miss)); ok = False
    else:
        print("  ✓ 群配置齐全（role/base/%d 个仓库）" % len(repos))

    for name, repo in repos.items():
        loc = repo_locate.locate(repo)
        if not loc:
            print("  ✗ [%s] 不是有效 git 仓库: %s" % (name, repo)); ok = False
            continue
        r = subprocess.run(["git", "-C", loc["toplevel"], "rev-parse", "--verify", "dev"],
                           capture_output=True)
        if r.returncode == 0:
            print("  ✓ [%s] 定位 %s（dev 分支在）" % (name, loc["url"]))
        else:
            print("  ✗ [%s] 没有 dev 分支（worker 从 dev 切子分支）" % name); ok = False

    if shutil.which("claude"):
        cp = subprocess.run(["claude", "-p", "ok", "--output-format", "json"],
                            capture_output=True, text=True)
        if '"is_error":false' in cp.stdout.replace(" ", ""):
            print("  ✓ claude 登录可用")
        else:
            print("  ✗ claude 未登录（claude / claude setup-token）"); ok = False
    else:
        print("  ✗ claude 命令不存在（worker 当前仍用 Claude Code 改代码）"); ok = False

    if shutil.which("gh"):
        gh = subprocess.run(["gh", "auth", "status"], capture_output=True)
        print("  ✓ gh 已认证" if gh.returncode == 0 else "  ✗ gh 未认证（gh auth login，提 PR 用）")
        ok = ok and gh.returncode == 0
    else:
        print("  ✗ gh 命令不存在（提 PR/MR 用；GitLab 项目也需要对应 CLI 或后续适配）")
        ok = False

    if not miss:
        ok_list, listing = await _lark_json(build_list_cmd(cc["base_app_token"], cc["base_table_id"]))
        if ok_list:
            pend = pending_records(listing or {})
            print("  ✓ 待修复 %d 条" % len(pend))
        else:
            print("  ✗ BUG 表记录读取失败（通常缺 base:record:read，worker 扫表需要它）")
            ok = False

    print("\n%s" % (("✅ 就绪！可以 python core/worker.py %s 真跑了" % chat_id)
                    if ok else "⚠️ 上面有 ✗，处理后再跑"))


# ---------------- 自测（python3 core/worker.py --selftest）----------------
def _selftest() -> None:
    # 1) build_fix_prompt 含关键约束
    p = build_fix_prompt({"编号": "0009", "摘要": "登录报错", "详情": "点登录→403"})
    assert "0009" in p and "登录报错" in p
    assert "DONE:" in p and "BLOCKED:" in p
    assert "绝不 merge" in p and "可 review" in p and "merge request" in p  # GitLab/GitHub 通用、不自动合
    assert "无人值守" in p and "不要】跑测试" in p   # 无人值守：别跑测试/构建
    assert "📷" not in p                          # 无截图时不出现截图段
    # 带截图：含「先看图」指令 + 路径（前端 BUG 截图就是问题现场）
    ps = build_fix_prompt({"编号": "0024", "摘要": "x", "详情": "y"},
                          screenshots=["/x/a.jpg", "/x/b.png"])
    assert "📷" in ps and "Read" in ps and "/x/a.jpg" in ps and "/x/b.png" in ps
    # 编号→分支 slug：任何编号都消毒成只含字母数字下划线（不再拒绝 NO.001/BUG-12 这类合法编号，且防 refspec 注入）
    assert branch_slug("0024") == "0024" and branch_slug("NO.001") == "NO_001"
    assert branch_slug("BUG-12") == "BUG_12" and branch_slug("#0001") == "0001"
    assert branch_slug("x:refs/heads/INJECTED") == "x_refs_heads_INJECTED"   # 冒号/斜杠消成 _
    assert branch_slug("a/b") == "a_b" and branch_slug("-rf") == "rf"        # 斜杠/前导横线
    assert branch_slug("") == "bug" and branch_slug("???") == "bug"          # 空/全特殊→兜底
    assert "/" not in branch_slug("x/y") and ":" not in branch_slug("a:b")   # 保证无注入字符
    print("✓ branch_slug 编号消毒成安全分支名（NO.001→NO_001，挡 : / 空格 等注入字符）")

    assert "第 0 步" in p and "先评估" in p          # 先评估难度
    assert "问一句" in p and "BLOCKED" in p           # 有疑问先问、别瞎找
    assert "沉没成本" in p and "绝不允许" in p          # 时间不够也要留分析
    print("✓ build_fix_prompt 含 先评估/有疑问先问/沉没成本红线 + 截图段 + 输出契约")

    # 1b) _salvage_claude_analysis：超时时能从 session transcript 捞出 claude 的分析
    import tempfile
    _wt = os.path.join(tempfile.gettempdir(), "emmy_salvage_wt", "bugfix-x")
    _sd = _session_dir_for(_wt)
    os.makedirs(_sd, exist_ok=True)
    _sf = os.path.join(_sd, "s.jsonl")
    with open(_sf, "w", encoding="utf-8") as _fh:
        _fh.write(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "根因在 Foo.tsx 的 adaptBar，企业空间被过滤了，还差最后一步"}]}}) + "\n")
    sav = _salvage_claude_analysis(_wt)
    assert "Foo.tsx" in sav and "adaptBar" in sav, sav
    assert _salvage_claude_analysis("/no/such/cwd/xyz") == ""   # 捞不到不报错
    os.remove(_sf); os.rmdir(_sd)
    print("✓ _salvage_claude_analysis：超时从 session 捞分析（无 session 返回空、不报错）")

    # 2) parse_worker_reply
    assert parse_worker_reply("...\nDONE: https://x/pr/1 | 修了登录")["outcome"] == "done"
    assert parse_worker_reply("DONE: https://x/pr/1 | 修了登录")["pr"] == "https://x/pr/1"
    assert parse_worker_reply("一堆分析\nBLOCKED: 两种改法不确定选哪个")["outcome"] == "blocked"
    assert parse_worker_reply("没头没尾")["outcome"] == "unknown"
    print("✓ parse_worker_reply 解析 DONE/BLOCKED/unknown")

    # 3) lark-cli 命令构造（正确 flag：--base-token / --json record_id_list+patch）
    lc = build_list_cmd("bascn_x", "tbl_x")
    assert lc[:2] == ["base", "+record-list"] and "--base-token" in lc and "bascn_x" in lc
    uc = build_update_cmd("bascn_x", "tbl_x", ["rec_1"], {"状态": "待发布"})
    assert "+record-batch-update" in uc and "--json" in uc
    payload = json.loads(uc[uc.index("--json") + 1])
    assert payload["record_id_list"] == ["rec_1"] and payload["patch"]["状态"] == "待发布"
    print("✓ lark-cli 命令构造（--base-token / record_id_list+patch）")

    # _lark_success：ok=false / code!=0 判失败，其余成功
    assert _lark_success({"ok": True, "data": {}}) and _lark_success({"code": 0})
    assert not _lark_success({"ok": False}) and not _lark_success({"code": 99}) and not _lark_success(None)
    print("✓ _lark_success 判 lark-cli 成败（不再静默吞错）")

    # 4) pending_records 过滤 状态=待修复
    listing = {"data": {"items": [
        {"record_id": "r1", "fields": {"状态": "待修复", "问题摘要": "A"}},
        {"record_id": "r2", "fields": {"状态": "已验收", "问题摘要": "B"}},
        {"record_id": "r3", "fields": {"状态": "待修复", "问题摘要": "C"}},
    ]}}
    pend = pending_records(listing)
    assert [r["record_id"] for r in pend] == ["r1", "r3"], pend
    print("✓ pending_records 只挑 待修复（items 格式）")

    # 表格式（lark-cli 真实返回）：fields 列名 + data 二维 + record_id_list，状态是 ['待修复'] 数组
    grid = {"data": {
        "fields": ["问题编号", "问题内容", "提问人", "状态"],
        "data": [
            ["0006", "改LOGO", "王文胜", ["待处理"]],
            ["0007", "国际化", "齐凯", ["待修复"]],
            ["0008", "别的", "李四", ["已验收"]],
        ],
        "record_id_list": ["rec6", "rec7", "rec8"],
    }}
    pg = pending_records(grid)
    assert [r["record_id"] for r in pg] == ["rec7"], pg
    assert pg[0]["fields"]["问题内容"] == "国际化" and pg[0]["fields"]["提问人"] == "齐凯"
    print("✓ pending_records 表格式（lark-cli 真实结构）+ 值规整")

    assert _flatten(["待处理"]) == "待处理" and _flatten(None) == "" and _flatten("x") == "x"
    assert _flatten([{"text": "a"}, {"text": "b"}]) == "a,b"
    print("✓ _flatten 规整单元格值")

    # 5) worker 权限：放开 git、仍挡 rm/sudo
    assert "Bash(git:*)" in WORKER_ALLOWED and "Bash(gh:*)" in WORKER_ALLOWED
    assert "Bash(rm:*)" in WORKER_DISALLOWED and "Bash(sudo:*)" in WORKER_DISALLOWED
    print("✓ worker allowedTools 放开 git/gh、挡 rm/sudo")

    # 6) @提问人：单一匹配→open_id，重名/对不上→纯文本（绝不 @ 错人）
    members = [{"name": "张三", "open_id": "ou_a"}, {"name": "李四", "open_id": "ou_b"}]
    assert _at_markup(members, "张三") == '<at user_id="ou_a"></at>'
    assert _at_markup(members, "王五") == "王五"
    assert _at_markup([{"name": "张三", "open_id": "o1"}, {"name": "张三", "open_id": "o2"}], "张三") == "张三"
    assert _at_markup(members, "") == ""
    print("✓ _at_markup：单一匹配 @、重名/对不上用文本名")

    # 7) 完成通知文案：done / 写失败降级 / blocked / 失败带@和真因
    assert "修好啦" in _result_message('<at>', "0006", {"result": "done", "pr": "http://pr/1"})
    assert "没写进去" in _result_message("", "0006", {"result": "done", "pr": "p", "wrote": False})
    assert "拿不准" in _result_message("", "0007", {"result": "blocked", "q": "选哪个改法"})
    fm = _result_message('<at>', "0008", {"result": "locate-fail", "reason": "代码路径好像不对"})
    assert "<at>" in fm and "代码路径好像不对" in fm
    print("✓ _result_message：done/写失败降级/blocked/失败带@和真因")

    # 8) _mr_url：框架构造 MR/PR 链接（GitLab / GitHub）
    gl = _mr_url("https://gitlab.prosperllm.ai/g/p", "bugfix/0006")
    assert "merge_requests/new" in gl and "bugfix%2F0006" in gl and "target_branch%5D=dev" in gl, gl
    gh = _mr_url("https://github.com/o/r", "bugfix/0006")
    assert "/compare/dev...bugfix%2F0006" in gh, gh
    print("✓ _mr_url 框架构造 MR/PR 链接（GitLab/GitHub）")

    # 9) _schema_gaps：缺字段 + 缺状态选项 / 齐全
    mf, mo = _schema_gaps({"fields": [
        {"name": "状态", "options": [{"name": "待修复"}, {"name": "修复中"}]},
        {"name": "问题编号"}]})
    assert "修复分支/PR" in mf and "AI备注" in mf and "待确认问题" in mf, mf
    assert "待发布" in mo and "待人工确认" in mo and "待修复" not in mo, mo
    full = {"fields": [{"name": "状态", "options": [{"name": o} for o in NEED_STATUS_OPTIONS]},
                       {"name": "修复分支/PR"}, {"name": "AI备注"}, {"name": "待确认问题"}]}
    assert _schema_gaps(full) == ([], []), _schema_gaps(full)
    print("✓ _schema_gaps 校验缺字段/缺状态选项（开工前置）")

    # 10) _pick_repo：单仓直接用 / 多仓按模块匹配 / 匹配不出 None
    assert _pick_repo({"默认": "/p"}, "随便") == "/p"
    assert _pick_repo({"前端": "/web", "后端": "/srv"}, "前端登录页") == "/web"
    assert _pick_repo({"前端": "/web", "后端": "/srv"}, "后端") == "/srv"
    assert _pick_repo({"前端": "/web", "后端": "/srv"}, "其他") is None  # 多仓匹配不出 → 交 BLOCKED
    assert _pick_repo({}, "x") is None
    print("✓ _pick_repo 多仓路由（单仓/模块匹配/匹配不出）")

    # 11) BLOCKED 续修：待人工确认+提问人答复非空 才续修；答复并进 prompt
    assert _should_fix({"状态": "待修复"})
    assert _should_fix({"状态": "待人工确认", "提问人答复": "用第二种改法"})
    assert not _should_fix({"状态": "待人工确认", "提问人答复": ""})  # 没补充就不续
    assert not _should_fix({"状态": "待处理"})
    bp = build_fix_prompt({"编号": "6", "摘要": "x", "详情": "y", "答复": "用方案B"})
    assert "用方案B" in bp and "提问人已补充" in bp
    print("✓ BLOCKED 续修：_should_fix 判定 + build_fix_prompt 带补充答复")

    # 12) 多仓：_safe_seg + build_fix_prompt_multi（挂全部仓、跨仓修、先看前端、各仓 git -C）
    assert _safe_seg("前端") == "前端" and _safe_seg("a/b c") == "a_b_c" and _safe_seg("") == "repo"
    mp = build_fix_prompt_multi({"编号": "0001", "摘要": "碎图", "详情": "上传后碎图"},
                                [("前端", "前端"), ("服务端", "服务端")], "bugfix/0001")
    assert "0001" in mp and "碎图" in mp
    assert "跨仓" in mp and "没动的仓" in mp and "前端/web" in mp        # 跨仓 + 只改需要的 + 先看前端
    assert "git -C 前端" in mp and "git -C 服务端" in mp                # 各仓用 git -C 操作
    assert "DONE:" in mp and "BLOCKED:" in mp and "无人值守" in mp
    print("✓ build_fix_prompt_multi：多仓跨仓指引（先看前端/只改需要的/各仓 git -C/契约）+ _safe_seg")

    print("\nworker 纯逻辑自测全部通过 ✅（端到端真改代码需 emmy.yaml 配好 MASS repo 后一起测）")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _selftest()
    elif len(sys.argv) > 2 and sys.argv[1] == "--check":
        asyncio.run(check_ready(sys.argv[2]))
    elif len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        asyncio.run(run_worker(sys.argv[1]))
    else:
        print("用法:\n"
              "  python core/worker.py --check <chat_id>   # 就绪自检（测试前先跑这个）\n"
              "  python core/worker.py <chat_id>           # 真跑修复\n"
              "  python core/worker.py --selftest          # 纯逻辑自测")
