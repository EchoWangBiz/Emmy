# 能力 · 内测 BUG 工单处理

当主人让你处理内测群的 BUG（"看下大家提的 BUG"、"整理一下"、"把 #X 标成待修复"、"通知大家验收"…），按这套流程来：

## 1. 读 BUG 表
那张收集 BUG 的多维表格，用这个读：
```
lark-cli base +record-list --app-token <app-token> --table-id <table-id>
```
> `app-token` / `table-id` 主人会告诉你，或藏在表格链接里：`.../base/<app-token>?table=<table-id>`。

## 2. 整理
按类型分类（Bug / 样式 / 体验 / UI / 功能…），标出高优，做个简洁小结。别拖长篇。

## 3. 改状态（工单流转）
状态字段流转：`待处理 → 待修复 → 修复中 → 待验收 → 已验收 / 不修`
改某条记录的状态：
```
lark-cli base +record-batch-update --app-token <app-token> --table-id <table-id> \
  --params '{"records":[{"record_id":"rec_xxx","fields":{"状态":"待修复"}}]}'
```
> ⚠️ **批量改状态是"危险操作"**——先跟主人确认要改哪些、改成啥，再动手。

## 4. @ 提问人通知（派工 / 验收）
要 @ 某条 BUG 的提问人时：
1. 先拿这个群的成员：`lark-cli im chat.members get --chat-id <群id>` → 拿到 open_id + 名字
2. 把这条 BUG 的「提问人」文本名 **对上群成员**（"齐凯-Kai Qi" → 群里的"齐凯"，你聪明，认得出）
3. 对上了 → @ 他：
   ```
   lark-cli im +messages-send --as bot --chat-id <群id> --msg-type text \
     --content '{"text":"<at user_id=\"ou_xxx\"></at> 你提的 #0009 修好了，PR: <链接>，麻烦验收~"}'
   ```
4. ⚠️ **对不上 / 有多个重名 → 别硬 @**，改成纯文本写名字："提问人 **王文胜**，你提的 #X…"。
   @ 错人比不 @ 更尴尬——记住你"绝不瞎猜乱来"。

## 5. 你的边界
- **改代码不是你的活**！你只管飞书侧：整理、改状态、@通知。修代码是代码侧那个 CC 哥哥的事，你别插手。
- 删记录、批量改、给一大群人发——**先问主人**。
