---
name: git-workflow
description: >-
  本仓库（Emmy）的 Git 两阶段提交规范。在「开始任何 coding 前」与「coding 完成准备合并/提交时」必须使用。
  约束：coding 前先从集成分支 dev 切出 temp/<功能> 子分支；在子分支上提交；验证通过后切回 dev 先 fetch 评估分歧再合并；
  预计无冲突时 AI 可自行完成提交、合并到 dev 并 push dev，无需逐次确认；仅当实际出现冲突，才把解决方案写到 docs/git-merge-log/ 并等待用户确认。唯一例外：发布到 main 走 Pull Request。
  Two-phase git workflow: temp/<topic> feature branch for coding, then merge to
  dev after verification, with conflict-resolution plans logged for confirmation.
---

# Git 使用规范（两阶段）

## 分支角色（Branch roles）

| 角色 | 分支 | 说明 |
|---|---|---|
| 集成分支 integration（“主分支”） | `dev` | 合并目标。所有子分支合并到这里。 |
| 发布分支 release | `main` | GitHub 默认分支；由 `dev` 经 **Pull Request** 上线，**不在本 skill 范围内直接 push**。 |
| 子分支 feature | `temp/<功能>` | 每个 coding 任务一条，从 `dev` 切出。例：`temp/h3-security`、`temp/start-launchd`。 |

---

## 阶段一：Coding（在子分支上）

**开始 coding 前**（不得直接在 `dev` 上改业务代码 / 脚本）：

```bash
git switch dev
git pull --rebase            # 确保子分支基线最新
git switch -c temp/<功能>     # 例: temp/h3-security
```

**Coding 过程中**：在子分支上小步提交，commit message 清晰（`feat/fix/docs/chore` + 一句话；正文可附 `Co-Authored-By`）。

**子分支收尾**：声明完成前跑 **Emmy 验收门禁**（按本次改动涉及的部分跑）：

```bash
# ① shell 脚本语法（改了 init.sh / start.sh 时）
bash -n init.sh && [ -f start.sh ] && bash -n start.sh

# ② Python 编译（改了 *.py 时）
python3 -m py_compile core/*.py run.py

# ③ 模块自测（改了对应模块时）
python3 core/listener.py && python3 core/claude_runner.py && python3 core/reply.py
```

涉及到的项全绿才算「子分支验证无误」，方可进入阶段二。

> Emmy 是 Python + shell 项目，没有 npm/lint/build 门禁；验证靠各模块内置自测 + 语法/编译检查。

---

## 阶段二：合并到 dev

> **自主性策略**：若**预计无冲突**，AI 可**自行完成**阶段一提交、合并到 `dev`、**并 `push dev` 到远端**，**全程无需逐次征求确认**；仅当**实际出现冲突**时才转入情况 B（写方案 + 等待确认）。唯一例外：发布到 `main` 必须走 Pull Request（见红线）。**不要为常规 git 动作反复询问。**

```bash
git switch dev
git fetch origin             # ① 先抓取，评估分歧（不自动合并）
git log --oneline HEAD...origin/dev  # ② 查看分歧大小
git merge --no-ff temp/<功能>   # ③ 合并子分支（fetch 已确认基线最新）
```

> **fetch 前置**：合并前必须先 `git fetch` 确认分歧，再决定策略。直接 `git pull` 可能在不知情的情况下触发合并冲突。若发现 origin 有新提交，先告知用户分歧情况，再执行合并。

### 情况 A：无冲突（默认路径 · 自行完成）
合并成功 → 跑验收门禁 → `git push`（`dev`）→ 删除子分支。**完成，无需询问**。

### 情况 B：有冲突 ⚠️（关键规则）

**禁止**静默自动解决并提交。必须：

1. **不要** `git commit` 完成合并；保持冲突态（或 `git merge --abort` 后重做，二选一，但都要先产出方案）。
2. 生成**冲突解决方案文档**到 `docs/git-merge-log/`，文件名 `YYYY-MM-DD-<功能>.md`，逐文件列出：
   - 冲突文件路径；
   - 两侧（`dev` / `temp/<功能>`）各自意图；
   - **建议的解决方案**与理由；
   - 影响面与验证计划。
3. **等待用户确认**该方案。
4. 用户确认后：按方案改冲突标记 → `git add` → 跑验收门禁 → `git commit` 完成合并。

> 日期请向环境获取（环境提供 currentDate），不要凭空捏造。

---

## doc / chore 直接提交 dev 的条件

纯文档或治理类变更（无应用代码 / 脚本改动，如 `README.md` / `计划书.md` / `.gitignore` / 本 skill）可**不切子分支**，直接在 `dev` 提交，但须满足：
1. `git fetch origin` 确认本地不落后于 origin（`HEAD...origin/dev` 无差异或本地领先）。
2. 落后时先同步（`git merge --no-ff origin/dev`），解决冲突后再提交。
3. 提交后立即 `git push origin dev`。

---

## 红线

- 不在 `dev` 上直接 coding（业务代码 / 脚本）；一律在 `temp/<功能>` 子分支。
- **doc/chore 豁免**：无应用代码 / 脚本改动时可直接提交 `dev`，但须先 `fetch` 确认不落后（见上节）。
- **无冲突**：不必为合并单独征求确认，AI 自行完成（默认路径）。
- **有冲突**：未产出 `docs/git-merge-log/` 方案且未获确认前，不提交 `dev`。
- 合并前必须先 `fetch`（不是 `pull`），评估分歧后再合并。
- `push dev` 属常规自动步骤（happy path 一部分），无需逐次确认；不要为它反复询问。
- `main` 不直接 push；发布走 **Pull Request**。
