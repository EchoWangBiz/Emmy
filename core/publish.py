#!/usr/bin/env python3
"""
core/publish.py —— 发布 worker（把「待发布」的修复合进 dev 并触发 Jenkins 部署）

被 run.py 在收到 Emmy 的 <PUBLISH/> 信号后拉起（确定性框架代码、不经 claude）。
主链路：读表「待发布」→ 按 repo 分组 → 把各 bugfix 分支合进 dev、push → jkit 构建 dev
        → ✅ 成功：状态→待验收 + 群通知（不 @人）；
        → ❌ 冲突/构建失败：jkit diagnose 抓错、报群，状态不动。

⚠️ 这是【唯一】被允许 push dev 的地方（用户确认放开红线）：只合「待发布」记录对应的
   bugfix 分支、只进 dev 绝不碰 main、干净合并才推、冲突即停、绝不 force。
   危险动作全在这份确定性代码里、参数硬编码，不让 claude 自由发挥。

跑法：python core/publish.py <chat_id>
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import worker, config, repo_locate  # noqa: E402  复用成熟函数

PIPE = asyncio.subprocess.PIPE
PUBLISH_WT_BASE = os.path.expanduser("~/.emmy/publish-worktrees")
_TMP_BRANCH = "_emmy_publish_tmp"
_PUBLISH_STATUS = "待发布"
_DEPLOYED_STATUS = "待验收"


# ---------------- 挑「待发布」记录（纯函数，复用 worker._flatten）----------------
def pending_publish(listing: dict) -> list:
    """从 record-list 结果挑出状态=「待发布」的记录。兼容表格式 + items 两种结构。"""
    data = (listing or {}).get("data") or {}
    out = []
    if isinstance(data.get("data"), list) and data.get("fields"):
        rids = data.get("record_id_list") or []
        for row, rid in zip(data["data"], rids):
            rf = {name: worker._flatten(v) for name, v in zip(data["fields"], row)}
            if str(rf.get(worker.STATUS_FIELD, "")).strip() == _PUBLISH_STATUS:
                out.append({"record_id": rid, "fields": rf})
        return out
    for it in (data.get("items") or (listing or {}).get("items") or []):
        f = it.get("fields") or {}
        if str(f.get(worker.STATUS_FIELD, "")).strip() == _PUBLISH_STATUS:
            out.append({"record_id": it.get("record_id") or it.get("id"), "fields": f})
    return out


def _jenkins_job(cc: dict, module: str):
    """按「所属模块」选 Jenkins job 名（jenkins_jobs: {模块名: job}）。复用 _pick_repo 的匹配逻辑：
    只一个 job → 直接用；多个 → 按模块名模糊匹配；匹配不出 → None。纯函数。"""
    jobs = {k: v for k, v in (cc.get("jenkins_jobs") or {}).items() if v}
    return worker._pick_repo(jobs, module)


# 发布范围同义词：用户说「部署前端/web」「部署后端/server」时，对上记录的「所属模块」
_SCOPE_SYN = [
    {"前端", "web", "frontend", "fe", "门户", "页面"},
    {"后端", "server", "srv", "backend", "be", "服务", "接口"},
]


def _scope_match(module: str, scope: str) -> bool:
    """这条记录的「所属模块」是否落在用户指定的发布范围内。scope 空=不限范围(全发)。纯函数。"""
    s = (scope or "").strip().lower()
    if not s:
        return True                  # 没指定 → 都算（默认全发）
    m = (module or "").strip().lower()
    if not m:
        return False                 # 记录没填模块、又指定了范围 → 不匹配（别误发到不该发的）
    if s in m or m in s:
        return True
    for grp in _SCOPE_SYN:           # 前端↔web、后端↔server 这类同义
        if any(w in m for w in grp) and any(w in s for w in grp):
            return True
    return False


# ---------------- git：把 bugfix 分支合进 dev 并 push（受控放开红线）----------------
def _g(repo_path: str, args: list, timeout: int = 60):
    return repo_locate._git(["-C", repo_path, *args], timeout=timeout)


def merge_branches_to_dev(repo_path: str, branches: list, base: str = "dev") -> dict:
    """在【临时 worktree】里把 branches 逐个合进 base、push origin base。
    不碰用户工作副本（独立 worktree + 临时分支，结果 push 到 origin/<base>）、绝不 force。
    返回 {merged:[分支], conflicts:[分支], missing:[分支], pushed:bool, error:str}。
    注：branches 里的编号已由调用方过白名单（is_safe_num），这里再用【显式锚定 refspec】作纵深防御。"""
    repo_key = (repo_locate.repo_origin(repo_path) or repo_path).replace("https://", "").replace("/", "__")
    uniq = "%d_%d" % (os.getpid(), int(time.time()))   # 唯一后缀：并发发布不互踩、不误删同名分支
    wt = os.path.join(PUBLISH_WT_BASE, "%s-%s" % (repo_key, uniq))
    tmp_branch = "%s_%s" % (_TMP_BRANCH, uniq)
    os.makedirs(os.path.dirname(wt), exist_ok=True)
    # base 单独 fetch，失败即整组中止（拉不到最新 dev 就别乱合）
    rb = _g(repo_path, ["fetch", "origin", base], timeout=120)
    if rb.returncode != 0:
        return {"merged": [], "conflicts": [], "missing": [], "pushed": False,
                "error": "拉取最新 %s 失败：%s" % (base, (rb.stderr or rb.stdout)[:200])}
    # 每个分支单独 fetch + 显式锚定 refspec（dst 固定，编号即使含 ':' 也注入不了别的 ref）；
    # 远程没有该分支（没推过/编号错）→ 归入 missing，不混进「冲突」、也不拖垮其余分支
    fetched, missing = [], []
    for b in branches:
        rf = _g(repo_path, ["fetch", "origin", "--no-tags",
                            "+refs/heads/%s:refs/remotes/origin/%s" % (b, b)], timeout=120)
        (fetched if rf.returncode == 0 else missing).append(b)
    _g(repo_path, ["worktree", "remove", "--force", wt])    # 清残留（路径已唯一，纯自我防御）
    _g(repo_path, ["branch", "-D", tmp_branch])
    r = _g(repo_path, ["worktree", "add", "-b", tmp_branch, wt, "origin/%s" % base])
    if r.returncode != 0:
        return {"merged": [], "conflicts": [], "missing": missing, "pushed": False,
                "error": "建发布临时 worktree 失败：%s" % (r.stderr or r.stdout)[:200]}
    merged, conflicts = [], []
    try:
        for b in fetched:
            rm = _g(wt, ["merge", "--no-ff", "origin/%s" % b,
                         "-m", "Merge %s into %s (emmy auto-publish)" % (b, base)])
            if rm.returncode != 0:
                _g(wt, ["merge", "--abort"])
                conflicts.append(b)
            else:
                merged.append(b)
        pushed, error = False, ""
        if merged:
            rp = _g(repo_path, ["push", "origin", "%s:%s" % (tmp_branch, base)], timeout=120)
            pushed = rp.returncode == 0
            if not pushed:
                error = "合并好了但推 %s 失败（多半远程有新提交、需重跑一次）：%s" % (
                    base, (rp.stderr or rp.stdout)[:200])
    finally:
        _g(repo_path, ["worktree", "remove", "--force", wt])  # 清理 worktree + 临时分支
        _g(repo_path, ["branch", "-D", tmp_branch])
    return {"merged": merged, "conflicts": conflicts, "missing": missing, "pushed": pushed, "error": error}


# ---------------- jkit：触发 Jenkins 构建 / 排错 ----------------
async def _run_jkit(args: list, timeout: int = 1800) -> tuple:
    """跑 jkit，返回 (ok, output)。jkit 没装 → (False, '__NO_JKIT__')；超时 → (False, '__TIMEOUT__')。"""
    try:
        proc = await asyncio.create_subprocess_exec("jkit", *args, stdout=PIPE, stderr=asyncio.subprocess.STDOUT)
    except FileNotFoundError:
        return False, "__NO_JKIT__"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return False, "__TIMEOUT__"
    return proc.returncode == 0, out.decode("utf-8", "replace")


async def jenkins_build(job: str) -> tuple:
    """触发 dev 构建并同步等完成。返回 (ok, output)。"""
    return await _run_jkit(["run", job, "--wait", "--log"])


async def jenkins_diagnose(job: str) -> str:
    """构建失败时抓错误上下文（不刷全量日志）。"""
    _ok, out = await _run_jkit(["diagnose", job], timeout=180)
    return out


def _nums(branches: list) -> str:
    return "、".join("#" + b.split("/")[-1] for b in branches)


def _tail(text: str, n: int = 500) -> str:
    text = (text or "").strip()
    return text[-n:] if len(text) > n else text


# ---------------- 主流程 ----------------
async def run_publish(chat_id: str, scope: str = "") -> None:
    """scope：发布范围——空=把所有「待发布」都发；指定(如 前端/web、后端/server)=只发对应模块。
    单仓时 scope 无意义（就一个服务）、忽略。"""
    cc = config.chat_config(chat_id)
    if not cc or cc.get("role") != "fix-bug":
        print("chat %s 未配置为 fix-bug 群" % chat_id, flush=True)
        return
    base_token, table_id = cc.get("base_app_token"), cc.get("base_table_id")
    repos = {k: v for k, v in (cc.get("repos") or
             ({"默认": cc.get("repo")} if cc.get("repo") else {})).items() if v}
    jobs = {k: v for k, v in (cc.get("jenkins_jobs") or {}).items() if v}
    if not (base_token and table_id and repos):
        print("emmy.yaml 缺 base/repos", flush=True)
        return
    if not jobs:
        await worker._send_group(chat_id, "想发布但还没配 Jenkins（jkit）——群主在初始化里把 jkit 装上、"
                                 "告诉我各项目的 job 名,我才能自动发哈 🦊")
        return
    _ok, listing = await worker._lark_json(worker.build_list_cmd(base_token, table_id))
    pend = pending_publish(listing or {})
    if not pend:
        await worker._send_group(chat_id, "现在没有「待发布」的活儿哈~ 等修复完的我再发 🦊")
        return

    # 发布范围：多仓时按 scope 过滤（只发指定服务）；单仓时 scope 无意义，全发
    apply_scope = bool(scope) and len(repos) > 1

    # 按 repo 分组（按「所属模块」路由），认不出模块的单列、不在范围的跳过
    groups, unrouted, skipped = {}, [], []
    for rec in pend:
        module = (rec.get("fields") or {}).get("所属模块", "")
        rp = worker._pick_repo(repos, module)
        if not rp:
            unrouted.append(rec)
            continue
        if apply_scope and not _scope_match(module, scope):
            skipped.append(rec)
            continue
        groups.setdefault(rp, {"module": module, "records": []})["records"].append(rec)
    if apply_scope and not groups:
        await worker._send_group(chat_id, "没找到属于「%s」的待发布记录哈~（其余待发布的没动）" % scope)
        return

    published = []   # 成功发布并已转「待验收」的编号
    for rp, g in groups.items():
        recs, module = g["records"], g["module"]
        # 编号 → 记录映射：显式建（不要字典推导），挡住「缺编号(?)互相覆盖」与「非法编号注入」两种坑
        by_num, invalid, dup = {}, [], []
        for r in recs:
            num = worker._field(r["fields"])["编号"]
            if not worker.is_safe_num(num):     # 缺编号(?)/含非法字符 → 不拿去拼分支
                invalid.append(r["record_id"])
            elif num in by_num:                 # 同组编号重复 → 别被 dict 静默覆盖吞掉
                dup.append(num)
            else:
                by_num[num] = r
        if invalid:
            await worker._send_group(chat_id, "这些记录编号缺失/非法、没法定位分支，没发布（群主补好「问题编号」再发）：%s"
                                     % "、".join(invalid))
        if dup:
            await worker._send_group(chat_id, "这些编号在同模块里重复了，只处理了一条、其余跳过，核对下：%s" % "、".join("#" + d for d in dup))
        if not by_num:
            continue
        branches = ["bugfix/%s" % n for n in by_num]
        job = _jenkins_job(cc, module)
        if not job:
            await worker._send_group(chat_id, "这些(模块=%s)还没配 Jenkins job 名,没法自动构建,群主补一下~ %s"
                                     % (module or "(空)", _nums(branches)))
            continue
        # 1) 合并进 dev
        mr = merge_branches_to_dev(rp, branches)
        if mr.get("missing"):   # 远程没有的分支（没推过/编号错）≠ 冲突，单独说清
            await worker._send_group(chat_id, "这些分支远程不存在/没推过、跳过（不是冲突，确认下是否真修过/真推过）：%s" % _nums(mr["missing"]))
        if mr["conflicts"]:
            await worker._send_group(chat_id, "这些合到 dev 有冲突、得人工处理（其余继续）：%s" % _nums(mr["conflicts"]))
        if not mr["merged"]:
            if mr.get("error"):
                await worker._send_group(chat_id, mr["error"])
            continue
        if not mr["pushed"]:
            await worker._send_group(chat_id, mr["error"] or "推 dev 没成,稍后重试")
            continue
        # 2) jkit 构建 dev
        ok, out = await jenkins_build(job)
        if out == "__NO_JKIT__":
            await worker._send_group(chat_id, "这台机器没装/没配 jkit,发布跑不了——群主去初始化里把 jkit 配上 🦊")
            break   # 后续 group 也只会同样报没 jkit；break 而非 return，好让下面把已发布的收尾通知发完
        merged_nums = [b.split("/")[-1] for b in mr["merged"]]
        if ok:
            rids = [by_num[n]["record_id"] for n in merged_nums if n in by_num]
            wok, _ = await worker._lark_json(worker.build_update_cmd(base_token, table_id, rids,
                                                                     {worker.STATUS_FIELD: _DEPLOYED_STATUS}))
            if wok:
                published += merged_nums
            else:   # 构建过了但表没写进去：别报假成功，告知人工兜底（表里仍「待发布」，下次 publish 会幂等重试）
                await worker._send_group(chat_id, "⚠️ %s 构建通过、但回写表状态失败（表里还是「待发布」）——"
                                         "群主手动改成「待验收」或稍后再喊我发一次：%s" % (module or job, _nums(branches)))
        else:
            tip = "构建超时" if out == "__TIMEOUT__" else _tail(await jenkins_diagnose(job), 400)
            await worker._send_group(chat_id, "❌ %s 部署没成（dev 构建失败）：\n%s" % (module or job, tip))

    # 收尾通知（不 @人）
    if published:
        await worker._send_group(chat_id, "✅ 已发布到 DEV、构建通过~ 这些可以验收了：%s 🛠️" % _nums(["bugfix/" + n for n in published]))
    if skipped:   # 按指定范围发的，范围外的待发布没动，说一声
        await worker._send_group(chat_id, "（你说只发「%s」，所以这些待发布的这次没动：%s，要发也喊我~）"
                                 % (scope, _nums(["bugfix/" + worker._field(r["fields"])["编号"] for r in skipped])))
    if unrouted:
        await worker._send_group(chat_id, "这些没认出所属模块、没法路由发布,群主标下模块再发：%s"
                                 % _nums(["bugfix/" + worker._field(r["fields"])["编号"] for r in unrouted]))


# ---------------- 自测（python core/publish.py --selftest）----------------
def _selftest() -> None:
    # 1) pending_publish 只挑「待发布」（表格式）
    listing = {"data": {
        "fields": ["问题编号", "状态", "所属模块"],
        "data": [["0024", ["待发布"], ["前端"]], ["0025", ["待修复"], ["前端"]],
                 ["0007", ["待发布"], ["后端"]]],
        "record_id_list": ["recA", "recB", "recC"]}}
    pend = pending_publish(listing)
    assert [p["record_id"] for p in pend] == ["recA", "recC"], pend   # 只 0024/0007（待发布）
    assert pending_publish({}) == []
    print("✓ pending_publish 只挑「待发布」")

    # 2) _jenkins_job：单 job 直接用 / 多 job 按模块匹配 / 匹配不出 None
    assert _jenkins_job({"jenkins_jobs": {"前端": "web-dev"}}, "随便") == "web-dev"     # 单个直接用
    cc = {"jenkins_jobs": {"前端": "web-dev", "后端": "srv-dev"}}
    assert _jenkins_job(cc, "前端门户") == "web-dev" and _jenkins_job(cc, "后端服务") == "srv-dev"
    assert _jenkins_job(cc, "数据库") is None                                          # 匹配不出
    assert _jenkins_job({}, "x") is None                                              # 没配
    print("✓ _jenkins_job 模块→job 路由（单个/匹配/匹配不出）")

    # 3) _scope_match：发布范围过滤（空=全发；同义词；空模块+指定范围不误发）
    assert _scope_match("前端门户", "") is True            # 没指定范围 → 都发
    assert _scope_match("前端门户", "前端") is True          # 子串
    assert _scope_match("前端门户", "web") is True           # 同义 前端↔web
    assert _scope_match("后端服务", "server") is True        # 同义 后端↔server
    assert _scope_match("前端门户", "后端") is False          # 不同模块
    assert _scope_match("前端门户", "server") is False
    assert _scope_match("", "前端") is False                # 记录没填模块 + 指定范围 → 不误发
    print("✓ _scope_match 范围过滤（全发/同义/隔离/空模块不误发）")

    # 4) 文案小工具
    assert _nums(["bugfix/0024", "bugfix/0007"]) == "#0024、#0007"
    assert _tail("x" * 600, 400) == "x" * 400 and _tail("abc", 400) == "abc"
    print("✓ _nums / _tail")

    print("\npublish 纯逻辑自测通过 ✅（合并 dev + jkit 构建需真环境，配好 jkit 后端到端测）")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    elif len(sys.argv) > 1:
        # python core/publish.py <chat_id> [scope]   scope 空=全发,指定=只发该模块
        asyncio.run(run_publish(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""))
    else:
        print("用法: python core/publish.py <chat_id> [scope]  |  --selftest")
