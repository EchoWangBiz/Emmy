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
import shutil
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


def _jenkins_job(cc: dict, module: str, sole: bool = False):
    """按模块选 Jenkins job（jenkins_jobs: {模块名: job}）。sole=True(本次只有一个模块要发) 且只有一个 job
    → 直接用；否则【严格】按模块名匹配——多模块时绝不拿单个 job 顶替别的模块（否则会把后端构建成前端 job）。纯函数。"""
    jobs = {k: v for k, v in (cc.get("jenkins_jobs") or {}).items() if v}
    if not jobs:
        return None
    if sole and len(jobs) == 1:
        return next(iter(jobs.values()))
    m = (module or "").strip()
    for name, job in jobs.items():
        if name == module or (m and (name in m or m in name)):
            return job
    return None


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
    注：branches 名已由调用方用 branch_slug 消毒成只含字母数字下划线，这里再用【显式锚定 refspec】作纵深防御。"""
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


# ---------------- 路由 + 配置闸（纯函数，便于自测，不再假通）----------------
def _route_by_branches(pend: list, modules: list, branch_exists) -> dict:
    """每条「待发布」→ `bugfix/branch_slug(编号)` → 逐仓查这个分支在不在该仓远程（worker 改了哪些仓就推了
    哪些分支），路由到对应仓。不靠「所属模块」。branch_exists(module, branch)->bool 由调用方注入（便于测）。
    返回 {module: [(branch, rec)]}。"""
    routed = {}
    for rec in pend:
        num = worker._field(rec.get("fields") or {})["编号"]
        branch = "bugfix/%s" % worker.branch_slug(num)
        for m in modules:
            if branch_exists(m, branch):
                routed.setdefault(m, []).append((branch, rec))
    return routed


def _config_gaps(modules_with_work: list, jobs: dict, jkit_present: bool) -> list:
    """发布前配置完整性检查（确定性）：jkit 装没 + 涉及的每个模块有没有 Jenkins job。
    返回缺失项文案列表（空=配置完整、可发）。纯函数。"""
    gaps = []
    if not jkit_present:
        gaps.append("这台机器还没装 jkit（自动发布要用它触发 Jenkins 构建）——在 init.sh 里装一下、并 jkit auth login。")
    sole = len(modules_with_work) == 1
    for m in modules_with_work:
        if not _jenkins_job({"jenkins_jobs": jobs}, m, sole=sole):
            gaps.append("模块「%s」还没配 Jenkins job——在 emmy.yaml 的 jenkins_jobs 里加一条「%s: <job名>」。" % (m, m))
    return gaps


# ---------------- 主流程 ----------------
async def run_publish(chat_id: str, scope: str = "") -> None:
    """先做【配置完整性闸】(jkit 装没 + 涉及模块有没有 job)：齐了才发、缺了精确告诉去配。
    路由不靠「所属模块」——按 branch_slug 拼分支、逐仓查它在哪个仓远程(worker 改了哪些仓就推了哪些)。
    scope：空=全发；指定(前端/web、后端/server)=只发对应模块(多仓时)。"""
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
    _ok, listing = await worker._lark_json(worker.build_list_cmd(base_token, table_id))
    pend = pending_publish(listing or {})
    if not pend:
        await worker._send_group(chat_id, "现在没有「待发布」的活儿哈~ 等修复完的我再发 🦊")
        return

    # 定位所有仓（路径不对是配置问题，直接报）
    located, bad = {}, []
    for module, path in repos.items():
        loc = repo_locate.locate(path)
        (located.__setitem__(module, {"path": path, "top": loc["toplevel"]}) if loc else bad.append("%s=%s" % (module, path)))
    if bad:
        await worker._send_group(chat_id, "这些仓路径不对、没法发布（群主核下 emmy.yaml 的 repos）：%s" % "、".join(bad))
        return

    # 发布范围：多仓时按 scope 过滤模块
    apply_scope = bool(scope) and len(located) > 1
    modules = [m for m in located if not (apply_scope and not _scope_match(m, scope))]
    if apply_scope and not modules:
        await worker._send_group(chat_id, "没找到属于「%s」的仓哈~（其余待发布的没动）" % scope)
        return

    # 路由：逐仓查分支是否在远程（worker 改了哪些仓就推了哪些）
    def _exists(m, branch):
        return repo_locate.branch_on_remote(located[m]["top"], branch)
    routed = _route_by_branches(pend, modules, _exists)
    if not routed:
        nums = _nums(["bugfix/" + worker.branch_slug(worker._field(r["fields"])["编号"]) for r in pend])
        await worker._send_group(chat_id, "这些「待发布」的，我在仓里没找到对应的 bugfix 分支（没真推过？编号对不上？）：%s" % nums)
        return

    # ★ 配置完整性闸（B）：齐了才发，缺了精确告诉去配——不再半路崩/假通
    gaps = _config_gaps(list(routed), jobs, shutil.which("jkit") is not None)
    if gaps:
        await worker._send_group(chat_id, "发布前还差点配置，配好再喊我发哈：\n%s" % "\n".join("· " + g for g in gaps))
        return

    # 逐仓：合进 dev → jkit 构建该仓 job → 转待验收
    published = []
    for module in routed:
        items = routed[module]                                   # [(branch, rec)]
        branches = list(dict.fromkeys(b for b, _r in items))     # 去重保序（同编号别合两次）
        mr = merge_branches_to_dev(located[module]["path"], branches)
        if mr["conflicts"]:
            await worker._send_group(chat_id, "[%s] 这些合 dev 有冲突、得人工处理（其余继续）：%s" % (module, _nums(mr["conflicts"])))
        if not mr["merged"]:
            await worker._send_group(chat_id, mr.get("error") or ("[%s] 没有能合进 dev 的分支" % module))
            continue
        if not mr["pushed"]:
            await worker._send_group(chat_id, mr.get("error") or ("[%s] 推 dev 没成，稍后重试" % module))
            continue
        job = _jenkins_job(cc, module, sole=len(routed) == 1)    # 闸已保证有
        ok, out = await jenkins_build(job)
        if out == "__NO_JKIT__":                                 # 闸理论上挡过了，纯兜底
            await worker._send_group(chat_id, "这台机器没装/没配 jkit，发布跑不了 🦊")
            break
        merged = set(mr["merged"])
        ok_recs = [r for b, r in items if b in merged]
        if ok:
            rids = [r["record_id"] for r in ok_recs]
            wok, _ = await worker._lark_json(worker.build_update_cmd(base_token, table_id, rids,
                                                                     {worker.STATUS_FIELD: _DEPLOYED_STATUS}))
            if wok:
                published += [worker._field(r["fields"])["编号"] for r in ok_recs]
            else:   # 构建过了但表没写进去：别报假成功，告知人工兜底（表里仍「待发布」，下次幂等重试）
                await worker._send_group(chat_id, "⚠️ [%s] 构建通过、但回写表状态失败（仍「待发布」）——"
                                         "群主手动改「待验收」或稍后再喊我发一次：%s" % (module, _nums(branches)))
        else:
            tip = "构建超时" if out == "__TIMEOUT__" else _tail(await jenkins_diagnose(job), 400)
            await worker._send_group(chat_id, "❌ [%s] dev 构建失败：\n%s" % (module, tip))

    if published:
        await worker._send_group(chat_id, "✅ 已合进 DEV、构建通过~ 这些可以验收了：%s 🛠️"
                                 % "、".join("#" + n for n in dict.fromkeys(published)))


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

    # 2) _jenkins_job：唯一模块+唯一 job 才兜底直接用；否则严格按模块匹配（多模块不拿单 job 顶替别人）
    assert _jenkins_job({"jenkins_jobs": {"前端": "web-dev"}}, "随便", sole=True) == "web-dev"   # 唯一→直接用
    assert _jenkins_job({"jenkins_jobs": {"前端": "web-dev"}}, "随便") is None                   # 非 sole→严格→不匹配
    cc = {"jenkins_jobs": {"前端": "web-dev", "后端": "srv-dev"}}
    assert _jenkins_job(cc, "前端门户") == "web-dev" and _jenkins_job(cc, "后端服务") == "srv-dev"
    assert _jenkins_job(cc, "数据库") is None and _jenkins_job(cc, "随便", sole=True) is None     # 多 job 即使 sole 也严格
    assert _jenkins_job({}, "x") is None                                              # 没配
    print("✓ _jenkins_job 模块→job（唯一兜底/多 job 严格匹配/匹配不出）")

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

    # 5) _route_by_branches：按 branch_slug 拼分支 + 逐仓按"分支是否存在"路由（不靠所属模块）
    pend2 = [{"record_id": "r1", "fields": {"问题编号": "NO.001"}},
             {"record_id": "r2", "fields": {"问题编号": "0007"}}]
    def fake_exists(m, branch):   # NO.001→NO_001 在服务端、0007 在前端（模拟 worker 改了哪些仓就推了哪些）
        return (m == "服务端" and branch == "bugfix/NO_001") or (m == "前端" and branch == "bugfix/0007")
    routed = _route_by_branches(pend2, ["前端", "服务端"], fake_exists)
    assert set(routed) == {"前端", "服务端"}, routed
    assert routed["服务端"][0][0] == "bugfix/NO_001" and routed["服务端"][0][1]["record_id"] == "r1"
    assert routed["前端"][0][0] == "bugfix/0007"
    assert _route_by_branches(pend2, ["前端", "服务端"], lambda m, b: False) == {}   # 哪个仓都没这分支→空
    print("✓ _route_by_branches：branch_slug(NO.001→NO_001) + 逐仓按分支存在路由")

    # 6) _config_gaps：jkit 没装 / 模块缺 job → 精确报缺；齐了→空（这就是"完整即发、不完整让用户配"的闸）
    g = _config_gaps(["前端", "服务端"], {"前端": "j1"}, jkit_present=True)
    assert any("服务端" in x and "job" in x for x in g) and not any("前端" in x for x in g), g  # 只服务端缺 job
    assert any("jkit" in x for x in _config_gaps(["前端"], {"前端": "j1"}, jkit_present=False))  # 没装 jkit
    assert _config_gaps(["前端"], {"前端": "j1"}, jkit_present=True) == []                       # 配置完整→放行
    print("✓ _config_gaps：缺 jkit/缺 job 精确报、配置齐了放行")

    print("\npublish 纯逻辑自测通过 ✅（合并 dev + jkit 构建需真环境，配好 jkit 后端到端测）")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    elif len(sys.argv) > 1:
        # python core/publish.py <chat_id> [scope]   scope 空=全发,指定=只发该模块
        asyncio.run(run_publish(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""))
    else:
        print("用法: python core/publish.py <chat_id> [scope]  |  --selftest")
