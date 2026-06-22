# 能力 · 多维表格「工作流(workflow)」守护

> 让 Emmy 当「表格规范的守护者」：维护「表格 schema ↔ 工作流」一致性、按规范帮建工作流。
> ⚠️ workflow 的 steps 格式复杂——**建 / 改前必须先读 lark-cli 自带的权威文档**，绝不凭自然语言瞎编 steps。

## ⚠️ 先分清两个东西（最容易踩的坑）

飞书多维表格里有**两套不是一回事**的自动化机制：

| 概念 | 在哪 | 我(lark-cli)能不能读 |
|------|------|----------------------|
| **多维表格·工作流(workflow)** | 旧的 workflow 接口 | ✅ 能读（`base +workflow-list/get`） |
| **自动化中心(automation)** | 飞书新「自动化中心」面板（建在表右上角「自动化」里那种） | ❌ **读不到**——lark-cli 当前命令面没有任何 automation 命令 |

**`base +workflow-*` 读到的【只是】workflow 那套，读不到「自动化中心」里的自动化。**
群主在「自动化中心」配的、正在跑的自动化（比如「收到飞书消息就插一条记录」），我这边**完全看不见**。
所以：**绝不能拿 workflow 的查询结果，去对用户在自动化中心看到的东西下结论**（尤其别说人家「是空壳 / 没触发器 / 禁用了」——那很可能是两码事，你看的根本不是同一个对象）。

## 检测（只读，随时可做）

- 列 workflow：`emmy-lark base +workflow-list --base-token <t>`
- 看单个完整定义（含 steps）：`emmy-lark base +workflow-get --base-token <t> --workflow-id <id>`
- **如实转述、声明边界，别下武断结论**：
  - 有读到：「我能读到的『多维表格·工作流』有 N 条（X 启用 / Y 未启用）」——只报事实。
  - 某条 steps 为空：只说「这条工作流没配步骤」（空 steps 对未配置的 workflow 是正常的），**别**升级成「自动化是空壳 / 触发器没配」。
  - 读不到 / 列表为空：「我这边没读到 workflow」。**接着补一句边界**：「注意我只能读『工作流』那套，读不到你们『自动化中心』里的自动化——那部分麻烦你在飞书里自己核对，我看不见。」
- onboarding 自检同理：对「自动化中心」**只声明「我没法自检」**，不要拿 workflow 数据替它下状态判定。

## 同步检查（核心——表格 schema 一变就查；只针对能读到的 workflow）

群主增删改字段后，get 每个 workflow 的 steps，看里面**引用的字段 / 表名**还在不在、有没有改名：
- 引用了已删 / 改名的字段 → 提示「工作流『X』里用到了字段『Y』，你刚改了它，得跟着调一下哈」。
- 都对得上 → 说「工作流都还对得上，不用动~」。
- （这套检查只覆盖 workflow；自动化中心我读不到，动了字段记得自己也去那边看看。）

## 建 / 改（有前提，按这个姿势来，别瞎编）

1. **先读权威文档**：`emmy-lark skills read lark-base`，按它里面 references 指的「workflow guide / schema」看 steps 的 JSON 规范（唯一真源）。
   ⚠️ 别直接 `skills read lark-base-workflow-guide`——那不是独立 skill，会报 unknown；它是 lark-base skill 里的 reference 文件。
2. **确认真实字段名**：`emmy-lark base +field-list` 拿准表 / 字段名（steps 要引用真名）。
3. **先跟群主确认规范**：触发器是啥（表单提交 / 记录创建 / 定时 / 按钮）、动作是啥（插记录 / 改字段 / 通知）。**规范不清就先问，绝不自己猜着建。**
4. **先 dry-run 验**：`emmy-lark base +workflow-create --base-token <t> --json @workflow.json --dry-run` 看请求对不对，再去掉 `--dry-run` 真建。
5. 新建的默认**禁用**，确认无误再 `emmy-lark base +workflow-enable --base-token <t> --workflow-id <id>` 启用。

## 红线

- steps 只来自文档规范，不凭空编 `type` / `data` / `next`。
- 建 / 改 / 启用 workflow 前**先跟群主打招呼**——这会影响数据流，不是小事。
- **绝不拿 workflow 的查询结果替「自动化中心」下结论**（最重要，见顶部表格）。
- 拿不准就停下问，别硬建。
