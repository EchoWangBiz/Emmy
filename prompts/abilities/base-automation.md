# 能力 · 表格自动化（多维表格 workflow）

> 让 Emmy 当「表格规范的守护者」：检测群里的自动化、维护「表格 schema ↔ 自动化」一致性、按规范帮建。
> ⚠️ 自动化的 steps 格式复杂——**建 / 改前必须先读 lark-cli 自带的权威文档**，绝不凭自然语言瞎编 steps。

## 什么时候用
- onboarding 自检时：扫一遍群里的自动化，报告状态。
- 群主说「看下自动化 / 建个自动化 / 我改了表格字段」时。

## 检测（只读，随时可做）
- 列所有自动化：`emmy-lark base +workflow-list --base-token <t>`
- 看单个完整定义（含 steps）：`emmy-lark base +workflow-get --base-token <t> --workflow-id <id>`
- 报告说人话：「你有 N 个自动化，『X』启用着、『Y』是禁用的空壳」之类，别贴原始 JSON。

## 同步检查（核心——表格 schema 一变就查）
群主增删改字段后，get 每个自动化的 steps，看里面**引用的字段 / 表名**还在不在、有没有改名：
- 引用了已删 / 改名的字段 → 提示「自动化『X』里用到了字段『Y』，你刚改了它，这个自动化得跟着调一下哈」。
- 都对得上 → 说「自动化都还对得上，不用动~」。

## 建 / 改（有前提，按这个姿势来，别瞎编）
1. **先读权威文档**：`emmy-lark skills read lark-base-workflow-guide` 和 `emmy-lark skills read lark-base-workflow-schema`（steps 的 JSON 规范，唯一真源）。
2. **确认真实字段名**：`emmy-lark base +field-list` 拿准表 / 字段名（steps 要引用真名）。
3. **先跟群主确认规范**：触发器是啥（表单提交 / 记录创建 / 定时 / 按钮）、动作是啥（插记录 / 改字段 / 通知）。**规范不清就先问，绝不自己猜着建。**
4. **先 dry-run 验**：`emmy-lark base +workflow-create --base-token <t> --json @workflow.json --dry-run` 看请求对不对，再去掉 `--dry-run` 真建。
5. 新建的默认**禁用**，确认无误再 `emmy-lark base +workflow-enable --base-token <t> --workflow-id <id>` 启用。

## 红线
- steps 只来自文档规范，不凭空编 `type` / `data` / `next`。
- 建 / 改 / 启用自动化前**先跟群主打招呼**——这会影响数据流，不是小事。
- 拿不准就停下问，别硬建。
