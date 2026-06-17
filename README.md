# Emmy 🦊 — 你自己的飞书 AI 智能体

> Emmy 是一个**跑在你自己 Mac 上**的飞书 AI 智能体。
> 在飞书里 `@Emmy` 说一句话，她就帮你写文档、发通知、记多维表格、建任务、查日历——
> 大脑直接用你本机的 **Claude Code**，干活靠 **lark-cli**，**不用配 API Key、不用服务器、不用公网**。

---

## 这是什么

每位同事在自己的 Mac 上 `git clone` 这个项目、跑一次 `./init.sh`，就拥有一个**专属于你**的飞书机器人。
它用**你自己的** Claude 订阅当大脑、用**你自己的**飞书身份干活，数据和凭证都只在你本机。

```
飞书里 @Emmy 发消息
        │  (lark-cli 长连接, 无需公网)
        ▼
   Emmy 监听进程 (你的 Mac, 后台常驻)
        │
        ▼
   本机 Claude Code (claude -p)  ← 你的大脑
        │  用 Bash 调 lark-cli
        ▼
   飞书 OpenAPI (文档/消息/表格/任务/日历...)
```

## 能干什么

| 能力 | 示例指令（在飞书 @Emmy） |
|------|------------------------|
| 📝 文档写作/整理 | "帮我写本周技术周报" |
| 💬 消息通知 | "通知项目群：部署已完成" |
| 📊 多维表格 | "把今天的数据记进 Bitable" |
| ✅ 项目任务 | "建个任务：修复登录 bug，截止周五" |
| 📚 知识库 | "把这份文档归档到知识库" |
| 📅 日历 | "下周一下午 3 点约个会" |

> Emmy 干的事就是你平时能用 lark-cli 干的事——能力随 lark-cli 全覆盖（im / docs / base / task / wiki / calendar / drive / sheets …）。
>
> **而且能不断长本事**：本质是让 **lark-cli（手脚）和 Claude Code（大脑）两个 agent 协作**，把 lark-cli 培养成一个真正的 agent。通过补充 Agent skills / command，Emmy 将来能编排更复杂的工作流——比如「拉群 → 组织内测 → 收集群里 BUG → 整理成多维表格 → 归纳分类 → 移交给代码 agent 自动修复」。

## 为什么这么设计（架构亮点）

- **不配 LLM**：大脑直接复用本机已装、已登录的 Claude Code，省掉 API Key、provider 配置、自写 agent loop。
- **不要服务器**：飞书事件用 `lark-cli event consume` **长连接(WebSocket)** 本地接收，**无需公网 IP、无需 Webhook、无需暴露端口**。
- **clone 即用**：`./init.sh` 自动体检装环境，再用 `lark-cli` 引导你**创建专属智能体**（`auth status` 检测到已有则直接复用），一步步带你配好。
- **人人独立**：每人一个飞书 app、一份本机实例、各自的凭证与会话，互不干扰，没有多租户复杂度。
- **后台常驻**：用 macOS `launchd` 守护，登录即起、崩溃自愈。

## 快速上手

👉 看 **[QUICKSTART.md](QUICKSTART.md)** —— 三步跑起来。
👉 飞书 app 怎么建（最繁琐的一次性步骤）看 **[ONBOARDING.md](ONBOARDING.md)**。

```bash
git clone <repo> && cd emmy
./init.sh        # 体检 + 装环境 + 引导配置
./start.sh       # 起 Emmy（后台常驻）
# 然后在飞书把机器人拉进群，@它 试试
```

## 环境要求

- **macOS**（Apple Silicon 或 Intel）
- 一个 **Claude 订阅**（Pro/Max；Emmy 用它当大脑）
- 一个**飞书账号**（`init` 会用 `lark-cli config init --new` 引导你创建一个智能体应用、已有则自动复用，**无需手动去开放平台一步步建**）
- 其余依赖（Homebrew / Node / Claude Code / lark-cli / Python 3.12）`./init.sh` 会帮你装

## 文档

| 文档 | 内容 |
|------|------|
| [QUICKSTART.md](QUICKSTART.md) | 最短上手路径：三步 + 前置清单 + 验证 |
| [ONBOARDING.md](ONBOARDING.md) | 飞书 app 建应用全流程（建 app / 权限 / 长连接订阅 / 发布 / 拉群） |
| [计划书.md](计划书.md) | 完整架构设计与技术方案 |

---

*Emmy 是内部工具，凭证与会话仅存于你本机（macOS Keychain + `~/.claude/`），不入库、不上传。*
