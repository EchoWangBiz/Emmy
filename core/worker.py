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
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import claude_runner, config, repo_locate  # noqa: E402

PIPE = asyncio.subprocess.PIPE

# worker 的 claude 权限：放开改代码要用的，仍挡破坏性命令
WORKER_ALLOWED = ("Bash(git:*) Bash(gh:*) Bash(npm:*) Bash(yarn:*) Bash(pnpm:*) "
                  "Bash(python3:*) Bash(node:*) Bash(ls:*) Bash(cat:*) Bash(grep:*) "
                  "Bash(rg:*) Bash(find:*) Edit Write Read")
WORKER_DISALLOWED = ["Bash(rm:*)", "Bash(sudo:*)", "Bash(curl:*)", "Bash(ssh:*)", "Bash(git push origin dev:*)"]

STATUS_FIELD = "状态"
# worker 的 worktree 一律开在【目标 repo 外】的 Emmy 自管目录，绝不在目标项目里留临时目录
WORKTREE_BASE = os.path.expanduser("~/.emmy/worktrees")


# ---------------- prompt ----------------
def build_fix_prompt(bug: dict, base_branch: str = "dev") -> str:
    """给 worker 的 claude 的修复指引（含 git-workflow 约束 + DONE/BLOCKED 输出契约）。"""
    return (
        "你是代码侧的修复 agent，当前目录是一个 git worktree（基于 %s 切出的独立 bugfix 分支），"
        "你的改动不会影响别人的工作副本，放心改。\n\n"
        "要修的 BUG：\n"
        "  编号: %s\n  摘要: %s\n  详情(复现/期望/实际): %s\n\n"
        "请按这个流程：\n"
        "1. 读懂相关代码、定位问题根因。\n"
        "2. 在【当前分支】改代码修复（已是独立 bugfix 分支）。\n"
        "3. git add + git commit（message 写清改了啥）。\n"
        "4. git push -u origin <当前分支>，再 gh pr create --base %s 提 PR。\n"
        "   ⚠️ 绝对不要 merge 到 %s、不要 push %s/main —— 只提 PR，停在这等人 review。\n"
        "5. 遇到【拿不准/高风险/需求不清】→【不要硬改】，停下，把问题讲清楚。\n\n"
        "最后一行必须是下面两种之一(便于我解析)：\n"
        "  DONE: <PR链接> | <一句话改了啥>\n"
        "  BLOCKED: <你拿不准的具体问题>\n"
        % (base_branch, bug.get("编号", "?"), bug.get("摘要", ""), bug.get("详情", ""),
           base_branch, base_branch, base_branch)
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
async def _lark_json(args: list) -> Optional[dict]:
    proc = await asyncio.create_subprocess_exec("lark-cli", *args, stdout=PIPE, stderr=PIPE)
    out, _ = await proc.communicate()
    try:
        return json.loads(out.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None


def build_list_cmd(base_token: str, table_id: str) -> list:
    return ["base", "+record-list", "--base-token", base_token, "--table-id", table_id, "--format", "json"]


def build_update_cmd(base_token: str, table_id: str, record_ids: list, patch: dict) -> list:
    payload = json.dumps({"record_id_list": record_ids, "patch": patch}, ensure_ascii=False)
    return ["base", "+record-batch-update", "--base-token", base_token, "--table-id", table_id, "--json", payload]


def pending_records(listing: dict) -> list:
    """从 record-list 返回里挑出 状态=待修复 的记录（纯函数，便于单测）。"""
    items = (((listing or {}).get("data") or {}).get("items")) or (listing or {}).get("items") or []
    out = []
    for it in items:
        fields = it.get("fields") or {}
        if str(fields.get(STATUS_FIELD, "")).strip() == "待修复":
            out.append({"record_id": it.get("record_id") or it.get("id"), "fields": fields})
    return out


async def read_pending(base_token: str, table_id: str) -> list:
    listing = await _lark_json(build_list_cmd(base_token, table_id))
    return pending_records(listing or {})


async def write_back(base_token: str, table_id: str, record_id: str, patch: dict) -> None:
    await _lark_json(build_update_cmd(base_token, table_id, [record_id], patch))


# ---------------- worker claude 调用 ----------------
async def _run_claude(prompt: str, cwd: str, timeout: int = 900) -> dict:
    cmd = ["claude", "-p", prompt, "--output-format", "json",
           "--permission-mode", "dontAsk",
           "--allowedTools", WORKER_ALLOWED,
           "--disallowedTools", *WORKER_DISALLOWED,
           "--model", "claude-sonnet-4-6", "--strict-mcp-config"]
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, stdout=PIPE, stderr=PIPE)
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return {"is_error": True, "text": "", "error": "timeout"}
    return claude_runner.parse_result(out.decode("utf-8", "replace"))


def _field(bug_fields: dict) -> dict:
    """把表格字段映射成 build_fix_prompt 要的 key（容错不同列名）。"""
    f = bug_fields
    return {
        "编号": f.get("问题编号") or f.get("编号") or "?",
        "摘要": f.get("问题摘要") or f.get("摘要") or "",
        "详情": " | ".join(str(f.get(k, "")) for k in ("复现步骤", "期望", "实际", "复现/期望/实际") if f.get(k)),
    }


# ---------------- 单条修复 ----------------
async def fix_one(rec: dict, repo_path: str, base_token: str, table_id: str) -> dict:
    bug = _field(rec.get("fields") or {})
    rid = rec["record_id"]

    loc = repo_locate.locate(repo_path)
    if not loc:
        await write_back(base_token, table_id, rid,
                         {STATUS_FIELD: "待人工确认",
                          "AI备注": "项目定位失败：%s 不是有效 git 仓库，请确认 emmy.yaml 的 repo 路径" % repo_path})
        return {"id": bug["编号"], "result": "locate-fail"}

    top = loc["toplevel"]
    branch = "bugfix/%s" % bug["编号"]
    # worktree 开到目标 repo 外（~/.emmy/worktrees/<repo>/<branch>），不污染目标项目
    repo_key = loc["url"].replace("https://", "").replace("/", "__")
    wt = os.path.join(WORKTREE_BASE, repo_key, branch.replace("/", "-"))
    os.makedirs(os.path.dirname(wt), exist_ok=True)
    repo_locate.remove_worktree(top, wt)  # 清残留
    ok, msg = repo_locate.make_worktree(top, branch, wt, base="dev")
    if not ok:
        await write_back(base_token, table_id, rid,
                         {STATUS_FIELD: "待人工确认", "AI备注": "worktree 创建失败：%s" % msg[:200]})
        return {"id": bug["编号"], "result": "worktree-fail"}

    try:
        # 领单加锁：先把状态改成"修复中"
        await write_back(base_token, table_id, rid, {STATUS_FIELD: "修复中"})
        res = await _run_claude(build_fix_prompt(bug), cwd=wt)
        reply = parse_worker_reply(res.get("text", ""))
        if reply["outcome"] == "done":
            await write_back(base_token, table_id, rid,
                             {STATUS_FIELD: "待发布", "修复分支/PR": reply.get("pr", ""),
                              "AI备注": reply.get("note", "")})
            return {"id": bug["编号"], "result": "done", "pr": reply.get("pr")}
        elif reply["outcome"] == "blocked":
            await write_back(base_token, table_id, rid,
                             {STATUS_FIELD: "待人工确认", "待确认问题": reply.get("question", "")})
            return {"id": bug["编号"], "result": "blocked", "q": reply.get("question")}
        else:
            await write_back(base_token, table_id, rid,
                             {STATUS_FIELD: "待人工确认", "AI备注": "worker 未明确完成：" + reply.get("note", "")[:200]})
            return {"id": bug["编号"], "result": "unknown"}
    finally:
        repo_locate.remove_worktree(top, wt)  # 清 worktree，保留分支(供 PR)


async def run_worker(chat_id: str) -> list:
    cc = config.chat_config(chat_id)
    if not cc or cc.get("role") != "fix-bug":
        print("chat %s 未配置为 fix-bug 群（检查 emmy.yaml）" % chat_id)
        return []
    base_token, table_id, repo = cc.get("base_app_token"), cc.get("base_table_id"), cc.get("repo")
    if not all([base_token, table_id, repo]):
        print("emmy.yaml 缺 base_app_token/base_table_id/repo")
        return []
    pend = await read_pending(base_token, table_id)
    print("待修复 %d 条" % len(pend))
    results = []
    for rec in pend:  # 串行：一条条修，稳
        results.append(await fix_one(rec, repo, base_token, table_id))
        print("  ->", results[-1])
    return results


# ---------------- 自测（python3 core/worker.py --selftest）----------------
def _selftest() -> None:
    # 1) build_fix_prompt 含关键约束
    p = build_fix_prompt({"编号": "0009", "摘要": "登录报错", "详情": "点登录→403"})
    assert "0009" in p and "登录报错" in p
    assert "只提 PR" in p and "DONE:" in p and "BLOCKED:" in p
    assert "不要 merge" in p
    print("✓ build_fix_prompt 含 BUG 信息 + 只提PR约束 + 输出契约")

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

    # 4) pending_records 过滤 状态=待修复
    listing = {"data": {"items": [
        {"record_id": "r1", "fields": {"状态": "待修复", "问题摘要": "A"}},
        {"record_id": "r2", "fields": {"状态": "已验收", "问题摘要": "B"}},
        {"record_id": "r3", "fields": {"状态": "待修复", "问题摘要": "C"}},
    ]}}
    pend = pending_records(listing)
    assert [r["record_id"] for r in pend] == ["r1", "r3"], pend
    print("✓ pending_records 只挑 待修复")

    # 5) worker 权限：放开 git、仍挡 rm/sudo
    assert "Bash(git:*)" in WORKER_ALLOWED and "Bash(gh:*)" in WORKER_ALLOWED
    assert "Bash(rm:*)" in WORKER_DISALLOWED and "Bash(sudo:*)" in WORKER_DISALLOWED
    print("✓ worker allowedTools 放开 git/gh、挡 rm/sudo")

    print("\nworker 纯逻辑自测全部通过 ✅（端到端真改代码需 emmy.yaml 配好 MASS repo 后一起测）")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _selftest()
    elif len(sys.argv) > 1:
        asyncio.run(run_worker(sys.argv[1]))
    else:
        print("用法: python core/worker.py <chat_id>  |  python core/worker.py --selftest")
