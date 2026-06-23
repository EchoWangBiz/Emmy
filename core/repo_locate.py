#!/usr/bin/env python3
"""
core/repo_locate.py —— 给修复 worker：定位本地项目 + 用 git worktree 隔离改动

设计要点（对抗审查实测过的安全方案）：
  - 定位靠 git remote URL【规范化匹配】，不靠路径名（ssh git@ 与 https 是同一 repo 的两种写法，要收敛）。
  - worker 【绝不在用户的工作副本上 checkout 改代码】——会污染用户当前分支/未提交改动。
    改用 git worktree 开一个独立工作目录（共享 .git 对象库、秒级、物理隔离；实测主副本 dirty 也不受影响）。
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Optional


def _git(args, cwd=None, timeout=30):
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                              text=True, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        class _R:  # 伪造一个失败结果，避免抛异常打断 worker
            returncode = 128
            stdout = ""
            stderr = str(e)
        return _R()


def normalize_url(url: str) -> str:
    """把同一 repo 的多种写法收敛成一个 key：
    git@host:owner/repo(.git) / ssh://git@host/... / https://host/owner/repo(.git) → https://host/owner/repo (小写)"""
    u = (url or "").strip()
    m = re.match(r"^git@([^:]+):(.+)$", u)           # scp 式 git@host:path
    if m:
        u = "https://%s/%s" % (m.group(1), m.group(2))
    u = re.sub(r"^ssh://git@", "https://", u)
    u = re.sub(r"^git://", "https://", u)
    u = u.rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    return u.lower()


def repo_origin(path: str) -> Optional[str]:
    """返回 path 的 git origin URL（规范化）；非 git 目录 / 无 origin / 路径不存在 → None。"""
    r = _git(["-C", path, "remote", "get-url", "origin"])
    if r.returncode != 0:
        return None
    return normalize_url(r.stdout.strip())


def repo_toplevel(path: str) -> Optional[str]:
    r = _git(["-C", path, "rev-parse", "--show-toplevel"])
    return r.stdout.strip() if r.returncode == 0 else None


def locate(repo_path: Optional[str], expected_url: Optional[str] = None) -> Optional[dict]:
    """校验 repo_path 是 git 仓库（可选 remote 匹配 expected_url）。
    命中返回 {toplevel, url}；非 git / 不匹配 / 路径无效 → None（worker 据此决定是否停下来问一句，绝不乱 clone）。"""
    if not repo_path or not os.path.isdir(repo_path):
        return None
    url = repo_origin(repo_path)
    if url is None:
        return None
    if expected_url and normalize_url(expected_url) != url:
        return None
    return {"toplevel": repo_toplevel(repo_path) or os.path.abspath(repo_path), "url": url}


def make_worktree(repo_path: str, branch: str, worktree_dir: str, base: str = "dev") -> tuple:
    """在 repo 上开独立 worktree（不碰用户副本）。返回 (ok, msg)。
    base：从哪个分支切（默认 dev，符合 git-workflow）。
    ⚠️【基于 origin/<base> 切，不用本地 base】：本地 base 分支可能很旧（用户没 pull），
    而 git fetch 只更新 origin/<base>、不会移动本地分支——从本地 base 切会基于过期代码改，
    修复对着旧码、合并时容易冲突。所以优先用刚 fetch 的 origin/<base> 作基线。
    分支已存在（二次派工/重试同一 BUG）→ 复用挂载，不再用 -b 强制新建而失败。"""
    # 拉最新 base，并优先以 origin/<base> 作基线（本地 base 可能落后）
    _git(["-C", repo_path, "fetch", "origin", base], timeout=60)
    base_ref = "origin/%s" % base
    if _git(["-C", repo_path, "rev-parse", "--verify", "--quiet", base_ref]).returncode != 0:
        base_ref = base   # 没有 origin/<base>（纯本地仓/无远程）→ 退回本地 base
    exists = _git(["-C", repo_path, "show-ref", "--verify", "--quiet",
                   "refs/heads/%s" % branch]).returncode == 0
    if exists:
        # 已有同名 bugfix 分支：挂上去续改（保留之前的提交），不重建
        r = _git(["-C", repo_path, "worktree", "add", worktree_dir, branch])
    else:
        r = _git(["-C", repo_path, "worktree", "add", "-b", branch, worktree_dir, base_ref])
        if r.returncode != 0:  # base_ref 不存在等情况，退回从当前 HEAD 切
            r = _git(["-C", repo_path, "worktree", "add", "-b", branch, worktree_dir])
    return (r.returncode == 0, (r.stderr or r.stdout).strip())


def remove_worktree(repo_path: str, worktree_dir: str) -> None:
    """清理 worktree（保留分支，便于 PR）。"""
    _git(["-C", repo_path, "worktree", "remove", "--force", worktree_dir])


def branch_on_remote(repo_path: str, branch: str) -> bool:
    """分支是否已推到 origin（worker 据此确认 claude 真的 push 了，而不是嘴上说 done）。"""
    r = _git(["-C", repo_path, "ls-remote", "--heads", "origin", branch], timeout=30)
    return r.returncode == 0 and bool(r.stdout.strip())


# ---------------- 自测（python3 core/repo_locate.py）----------------
def _selftest() -> None:
    # 1) URL 规范化：6 种写法收敛同一 key
    keys = {
        normalize_url("git@github.com:EchoWangBiz/Emmy.git"),
        normalize_url("https://github.com/EchoWangBiz/Emmy.git"),
        normalize_url("https://github.com/EchoWangBiz/Emmy"),
        normalize_url("ssh://git@github.com/EchoWangBiz/Emmy.git"),
        normalize_url("https://github.com/echowangbiz/emmy/"),
    }
    assert keys == {"https://github.com/echowangbiz/emmy"}, keys
    print("✓ URL 规范化：5 种写法收敛为 1")

    # 2) repo_origin / locate 在本仓库上
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    url = repo_origin(here)
    assert url == "https://github.com/echowangbiz/emmy", url
    assert locate(here) is not None
    assert locate(here, expected_url="git@github.com:EchoWangBiz/Emmy.git") is not None  # 异写法也匹配
    assert locate(here, expected_url="https://github.com/someone/other") is None         # 错 repo 拒绝
    print("✓ repo_origin / locate（含异写法匹配、错repo拒绝）")

    # 3) 非 git / 不存在路径 → None
    assert repo_origin("/tmp") is None
    assert locate("/no/such/path") is None
    print("✓ 非 git / 无效路径返回 None")

    # 4) worktree 隔离实测：add 一个临时 worktree → 干净 → remove
    import tempfile
    wt = os.path.join(tempfile.gettempdir(), "emmy_wt_selftest")
    remove_worktree(here, wt)  # 清残留
    _git(["-C", here, "branch", "-D", "emmy_wt_selftest_br"])  # 清残留分支
    ok, msg = make_worktree(here, "emmy_wt_selftest_br", wt, base="dev")
    assert ok, "worktree add 失败: " + msg
    # 基线应是 origin/dev（最新）而非可能过期的本地 dev——有远程才校验
    if _git(["-C", here, "rev-parse", "--verify", "--quiet", "origin/dev"]).returncode == 0:
        wt_head = _git(["-C", wt, "rev-parse", "HEAD"]).stdout.strip()
        od = _git(["-C", here, "rev-parse", "origin/dev"]).stdout.strip()
        assert wt_head == od, "新 worktree 应基于 origin/dev(%s), 实际 %s" % (od[:8], wt_head[:8])
    # 主副本 dirty 不出现在 worktree（worktree 应干净）
    st = _git(["-C", wt, "status", "--porcelain"]).stdout
    assert st.strip() == "", "新 worktree 应干净, 实际: " + st
    remove_worktree(here, wt)  # 删工作目录、保留分支
    # 二次派工：分支已存在 → 复用挂载应成功（修复「二次派工必 worktree-fail」）
    ok2, msg2 = make_worktree(here, "emmy_wt_selftest_br", wt, base="dev")
    assert ok2, "分支已存在时复用失败: " + msg2
    remove_worktree(here, wt)
    _git(["-C", here, "branch", "-D", "emmy_wt_selftest_br"])
    print("✓ git worktree 开/清 + 隔离 + 分支已存在复用")

    print("\nrepo_locate 自测全部通过 ✅")


if __name__ == "__main__":
    _selftest()
