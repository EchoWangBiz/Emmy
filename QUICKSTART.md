# Emmy 快速上手

跑起一个属于你自己的飞书智能体，大约 **10 分钟**——装环境全自动；智能体由 `lark-cli` 引导创建（已有则自动复用），不用手动去开放平台一步步建。

---

## 你需要先准备

| 前置 | 说明 | 能否自动 |
|------|------|---------|
| macOS | Apple Silicon 或 Intel 都行 | — |
| Claude 订阅 | Pro/Max，Emmy 用它当大脑 | ❌ 需你已有账号 |
| 飞书账号 | `init` 用 `lark-cli` 引导创建/复用智能体 | 🔶 半自动（`config init --new` 引导，浏览器点一下） |
| Homebrew / Node / Claude Code / lark-cli / Python 3.12 | 运行依赖 | ✅ `./init.sh` 自动装 |

> 如果你公司不允许员工自建应用，可让管理员代建一个并给你 `App ID / App Secret`，`init` 里用 `lark-cli config init` 填入即可（详见 [ONBOARDING.md](ONBOARDING.md)）。

---

## 三步上手

### 第一步：克隆项目

```bash
git clone <repo> && cd emmy
```

### 第二步：`./init.sh` — 一键体检 + 装环境

```bash
./init.sh
```

`init.sh` 会**自动**做这些（已装的会跳过，不会重复装）：

1. **体检**：逐项检查 Homebrew / Node / Claude Code / lark-cli / Python 3.12 是否就位，打印 `✓ / ✗` 清单
2. **征求同意后安装**缺失的工具（会先列出"将要安装什么"，你确认 `y` 才动手）：
   - 缺 Homebrew → 跑官方安装脚本
   - `brew install --cask claude-code`
   - `brew install node@22`（Node LTS）
   - `npm i -g @larksuite/cli`
   - `brew install python@3.12` + 建项目虚拟环境装 Python 依赖
   - （可选）`npx skills add larksuite/cli` 给 Claude Code 装上 lark API 技能包，让 Emmy 调 lark-cli 更准
3. **引导你完成无法自动的步骤**（会打印清晰指引）：
   - `claude` 登录（或 `claude setup-token` 生成长期 token，后台长挂更稳）
   - **创建/复用智能体**：先用 `lark-cli auth status` 检测——已有就直接复用；没有则用 `lark-cli config init --new` 引导你在浏览器创建一个应用（自动申请所需权限）
   - 若是新建的应用，可能还需在飞书后台开机器人能力、订阅消息事件、发布一次（[ONBOARDING.md](ONBOARDING.md) 有指引，`init` 会带你走）
4. **验证**：跑 `lark-cli doctor` 和 `claude -p` 自检，全绿才算就绪

> 任何一步失败，`init.sh` 不会卡死——它会打印一条你可以直接复制粘贴的修复命令，修完重跑 `./init.sh` 即可（幂等）。

### 第三步：`./start.sh` — 起 Emmy

```bash
./start.sh fg       # 前台运行，第一次调试推荐（看实时日志，Ctrl-C 退出）
# 或
./start.sh start    # 安装 launchd 后台守护，登录即自动运行、崩溃自愈
```

> 子命令一览：`fg`（前台）· `start`（后台常驻）· `stop` · `restart` · `status` · `logs`。

起来之后，**把机器人拉进群，@它 发消息**。

**新群第一次 @ 会先「配置门禁」**——Emmy 用对话带你把这个群一次性配好（这群干啥 / BUG 多维表格分享链接 / 代码项目本地路径 / 可选 Jenkins job），配好自动写进 `emmy.yaml`、标记完成，以后不再问。通常**不用手填配置文件**。

配好之后就能直接使唤：

```
# 通用飞书活
@Emmy 帮我写一条今天的工作小结发到这个群

# 旗舰能力：BUG 工单闭环（在「修 BUG 群」里）
@Emmy 把群里信息齐的 BUG 都修了        # → 整理工单、派代码侧自动改码提 PR、修好 @你验收
@Emmy #12 进度咋样了                   # → 按状态汇报当前盘子
@Emmy 发布                            # → 把「待发布」的合进 DEV、jkit 构建、通知验收
```

---

## 验证装好了

```bash
lark-cli doctor          # 飞书侧：config / auth / connectivity 全 pass
claude -p "ok"           # 大脑侧：能正常返回
./start.sh status        # Emmy 进程在跑
./start.sh logs          # 看实时日志（tail ~/.emmy/logs/emmy.log）
```

## 日常操作

```bash
./start.sh start         # 起（后台常驻）
./start.sh stop          # 停
./start.sh restart       # 重启
./start.sh status        # 看状态
./start.sh logs          # 看日志
```

> 日志都在 `~/.emmy/logs/`：主进程 `emmy.log` / `emmy.err.log`；后台修复 worker `worker-<chat>.log`、发布 worker `publish-<chat>.log`（可单独 `tail` 看某个群的活）。

## 常见问题

- **@Emmy 没反应？** 依次确认：机器人已被拉进群、app 已"发布版本"、长连接订阅方式已保存（见 [ONBOARDING.md](ONBOARDING.md)）、`./start.sh status` 在跑。
- **过一阵 Emmy 不回了？** 多半是 Claude Code 登录态过期。重新 `claude` 登录，或改用 `claude setup-token` 长期 token（后台长挂推荐）。
- **修 BUG 群怎么配？** 把机器人拉进群、@它一次，跟着「配置门禁」对话走完即可（需要 BUG 多维表格的分享链接 + 代码项目的本地绝对路径；多仓按「前端/后端」分别给）。
- **派工了但 worker 没动静？** 看 `~/.emmy/logs/worker-<chat>.log`。常见原因：BUG 信息不全（worker 会停在「待人工确认」并把疑问写进表里，等你补「提问人答复」后下一轮自动续修）、或代码仓库路径没配对。
- **自动发布到 DEV 要什么？** 在跑 Emmy 这台机器上装好 `jkit` 并登录一次，再把每个项目的 Jenkins job 名告诉 Emmy（配置门禁里会问）。不配也行——那就只到「待发布」，发布人工来。
- **Intel Mac？** 没问题，`init.sh` 用 `$(brew --prefix)` 自适应路径。
- **Node 是 v26 不是 LTS？** 能用；`init.sh` 只会提醒、不强制降级。

---

更繁琐的飞书 app 配置细节 → **[ONBOARDING.md](ONBOARDING.md)**　·　完整设计 → **[计划书.md](计划书.md)**
