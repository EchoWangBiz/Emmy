#!/usr/bin/env python3
"""
core/lark_gate.py —— Emmy 飞书命令的安全门禁判定（代码层硬门禁，不靠 prompt 软约束）

背景：Emmy 大脑可无人工确认自主执行飞书命令；文件/消息内容会被注入 prompt，存在间接
prompt 注入面。所以不让她直连 lark-cli，改走包装命令 bin/emmy-lark，由本模块判定放行与否。

【方法：白名单（默认拒绝）】——经红队验证，黑名单（拦 delete/remove 关键词）两头都漏：
  漏危险（transfer_owner / cells-clear / docs overwrite / record-batch-create / field-update
  这类不可逆操作命名千变万化），又误杀正常（消息体/flag/数据值里恰好含 delete）。
所以只放行 Emmy BUG 流程真正需要的少数命令前缀，其余一律拦：
  - 不可逆/破坏性操作天然落在白名单外 → 默认拒绝，不必逐个枚举；
  - 判定只看「命令前缀（位置参数）」，不扫 flag 名 / 数据载荷 → 不会误杀；
  - 批量写额外做 fail-closed 阈值（算不出条数也拦）。
要开新能力 = 显式往白名单加一条（这正是「显式授权」该有的样子）。
"""
from __future__ import annotations

import json
from typing import List, Tuple

BATCH_LIMIT = 20                       # record 批量写超过这个条数即拦
# 批量写载荷里「条数」可能出现的 key：
#   record-batch-update → record_id_list（被 patch 的记录）
#   record-batch-create → rows（每行一条新记录，跟 fields 顺序）
_RECORD_LIST_KEYS = ("record_id_list", "records", "record_ids", "records_list", "rows")

# 只放行这些命令前缀（按位置参数小写匹配）。Emmy 当前能力 = BUG 管理（base 多维表格 + im 消息）。
# 新增能力请在此显式添加；危险操作（删除/清空/覆盖/转移/移动/撤销/api 写/未知子命令）一律落在白名单外。
_ALLOWED_PREFIXES = (
    # —— base 多维表格：读 ——
    "base +record-list", "base +record-search", "base +record-get",
    "base +field-list", "base +table-list", "base +view-list",
    # —— base 多维表格：受控写（建/改记录、建字段；批量写另受阈值约束）——
    # 注：lark-cli 无单条 record-create/record-update 命令；建记录走 batch-create（按 rows）
    #     或 upsert（不带 --record-id 即建单条）。
    "base +record-batch-create", "base +record-upsert",
    "base +record-batch-update", "base +field-create",
    # —— im 消息：读 ——
    "im chat.members get", "im +messages-mget", "im +messages-resources-download",
    "im +chat-search", "im +chat-list", "im +chat-messages-list",
    # —— im 消息：写（@通知 / 回复，Emmy 核心能力）——
    "im +messages-send", "im +messages-reply",
    # —— im 置顶：onboarding 时把 BUG 表入口 pin 到群里 ——
    "im pins list", "im pins create",
    # —— 联系人：搜人（@ 提问人要用）——
    "contact +search-user",
    # —— 表格自动化：检测/维护/按规范建（base-automation 能力）——
    "base +workflow-list", "base +workflow-get",
    "base +workflow-create", "base +workflow-update",
    "base +workflow-enable", "base +workflow-disable",
    # —— 读 lark-cli 自带文档（建自动化前学 steps 格式，只读）——
    "skills read",
)
# 白名单内但需 fail-closed 阈值核验的批量写（建/改记录，超阈值或算不出条数都拦）
_BATCH_WRITE_PREFIXES = ("base +record-batch-update", "base +record-batch-create")


def _count_records(argv: List[str]):
    """从 --json 载荷里数 record 条数；解析不出来返回 None（交由 fail-closed 处理）。"""
    if "--json" not in argv:
        return None
    try:
        payload = json.loads(argv[argv.index("--json") + 1])
    except (IndexError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    for k in _RECORD_LIST_KEYS:
        v = payload.get(k)
        if isinstance(v, list):
            return len(v)
    return None


def classify(argv: List[str]) -> Tuple[bool, str]:
    """判断这条 lark-cli 调用是否高危。返回 (blocked, reason)。argv 不含程序名本身。
    白名单命中且通过附加核验 → (False, '')；否则 → (True, 原因)。"""
    a = [str(t) for t in (argv or [])]
    if not a:
        return False, ""                       # 裸命令无害
    if any(t in ("--help", "-h", "help") for t in a):
        return False, ""                       # 查文档/帮助一律放行（不执行实际操作）

    pos = [t for t in a if not t.startswith("-")]
    cmd_prefix = " ".join(pos).lower()

    matched = None
    for allowed in _ALLOWED_PREFIXES:
        if cmd_prefix == allowed or cmd_prefix.startswith(allowed + " "):
            matched = allowed
            break
    if matched is None:
        shown = " ".join(pos[:2]) if pos else "(空)"
        return True, f"{shown} —— 不在安全白名单内（默认拒绝；如确需，请人工执行或显式加白名单）"

    # 批量写：fail-closed —— 算不出条数（无 --json / @file / stdin / 脏 JSON）或超阈值都拦
    if any(cmd_prefix == p or cmd_prefix.startswith(p + " ") for p in _BATCH_WRITE_PREFIXES):
        n = _count_records(a)
        if n is None:
            return True, "批量写但无法静态核验条数（载荷缺失/来自文件或 stdin）——保险起见拦截"
        if n > BATCH_LIMIT:
            return True, f"批量写 {n} 条记录（超过门禁阈值 {BATCH_LIMIT}，防批量篡改）"

    return False, ""


# ---------------- 自测（python3 core/lark_gate.py）----------------
def _selftest() -> None:
    def blocked(args):
        return classify(args)[0]

    # —— 放行：Emmy 日常需要的 base/im/contact 读写 ——
    assert not blocked(["base", "+record-list", "--base-token", "t", "--table-id", "tb"])
    assert not blocked(["base", "+field-list", "--base-token", "t", "--table-id", "tb"])
    assert not blocked(["base", "+field-create", "--base-token", "t", "--json", '{"name":"状态"}'])
    assert not blocked(["im", "chat.members", "get", "--chat-id", "oc_x"])
    assert not blocked(["im", "+messages-send", "--as", "bot", "--chat-id", "oc_x",
                        "--content", '{"text":"修好了 @你 验收"}'])
    assert not blocked(["im", "+messages-resources-download", "--message-id", "om_x", "--file-key", "f"])
    assert not blocked(["contact", "+search-user", "--query", "张三"])
    assert not blocked(["im", "--help"])
    assert not blocked(["im", "pins", "list", "--chat-id", "oc_x"])
    assert not blocked(["im", "pins", "create", "--chat-id", "oc_x", "--message-id", "om_x"])
    assert not blocked(["base", "+workflow-list", "--base-token", "t"])
    assert not blocked(["base", "+workflow-create", "--base-token", "t", "--json", "@w.json"])
    assert not blocked(["skills", "read", "lark-base-workflow-guide"])
    assert not blocked(["base", "+record-batch-update", "--base-token", "t", "--table-id", "tb",
                        "--json", '{"record_id_list":["rec1"],"patch":{"状态":"待修复"}}'])
    # 登记新 BUG：小批量建记录放行（rows ≤ 阈值）
    assert not blocked(["base", "+record-batch-create", "--base-token", "t", "--table-id", "tb",
                        "--json", '{"fields":["问题摘要","状态"],"rows":[["菜单栏高度异常","待修复"]]}'])
    assert not blocked(["base", "+record-upsert", "--base-token", "t", "--table-id", "tb",
                        "--json", '{"问题摘要":"菜单栏高度异常","状态":"待修复"}'])   # 不带 record-id = 建单条
    print("✓ 放行：base/im/contact 白名单内读写 + 单条批量改 + 小批量建记录/upsert + --help")

    # —— 不误杀（红队 false-positive 全消）：白名单内、关键词在 flag/数据值里 ——
    assert not blocked(["im", "+messages-send", "--as", "bot", "--chat-id", "oc_x",
                        "--content", '{"text":"请 delete 掉旧缓存再 remove 重启"}'])   # 消息体含 delete/remove
    assert not blocked(["base", "+record-search", "--base-token", "t",
                        "--filter", "CurrentValue.[状态]=已delete"])                  # 过滤值含 delete
    assert not blocked(["base", "+record-upsert", "--base-token", "t",
                        "--json", '{"fields":{"备注":"remove later"}}'])              # upsert + 值含 remove
    print("✓ 不误杀：白名单内命令，delete/remove 出现在 flag/消息体/过滤值里照样放行")

    # —— 拦截（红队 missed-danger 全堵）：不可逆/破坏性操作落在白名单外 ——
    assert blocked(["drive", "permission.members", "transfer_owner", "--token", "doccnX",
                    "--data", '{"member_id":"ou_evil"}'])                              # 转移所有权
    assert blocked(["sheets", "+cells-clear", "--url", "u", "--range", "A1:Z99999", "--scope", "all"])
    assert blocked(["sheets", "+cells-replace", "--url", "u", "--find", ".*", "--regex", "--replacement", ""])
    assert blocked(["docs", "+update", "--doc", "doxX", "--command", "overwrite", "--content", ""])
    assert blocked(["base", "+record-delete", "--base-token", "t", "--record-id", "rec_x"])  # 删记录
    assert blocked(["base", "+field-update", "--base-token", "t", "--field-id", "f"])  # 改字段类型 high-risk
    assert blocked(["base", "+table-delete", "--base-token", "t"])
    assert blocked(["wiki", "+move", "--node-token", "n"])
    assert blocked(["approval", "instances", "cancel", "--instance-id", "i"])
    assert blocked(["drive", "+version-revert", "--file-token", "f"])
    assert blocked(["api", "POST", "/open-apis/bitable/v1/.../batch_update"])          # api 写
    assert blocked(["api", "GET", "/open-apis/im/v1/chats"])                           # api 一律不放（含读）
    print("✓ 拦截：transfer_owner/cells-clear/cells-replace/docs-overwrite/record-delete/field-update/table-delete/move/cancel/revert/api")

    # —— 批量写 fail-closed（红队 #5/#6）——
    big = '{"record_id_list":[%s]}' % ",".join('"r%d"' % i for i in range(25))
    assert blocked(["base", "+record-batch-update", "--base-token", "t", "--json", big])      # 改：超阈值
    assert blocked(["base", "+record-batch-update", "--base-token", "t", "--json", "@payload.json"])  # @file 无法核验
    assert blocked(["base", "+record-batch-update", "--base-token", "t"])                     # 无 --json
    big_rows = '{"fields":["A"],"rows":[%s]}' % ",".join('["v%d"]' % i for i in range(25))
    assert blocked(["base", "+record-batch-create", "--base-token", "t", "--json", big_rows]) # 建：超阈值
    assert blocked(["base", "+record-batch-create", "--base-token", "t", "--json", "@rows.json"])  # @file 无法核验
    assert blocked(["base", "+record-batch-create", "--base-token", "t"])                     # 无 --json
    print("✓ 批量写 fail-closed：建/改 超阈值 / @file / 缺载荷 都拦，只放明确小批量")

    # —— 边界 ——
    assert not blocked([])
    assert not blocked(["--help"])
    assert blocked(["totally", "+unknown-verb"])     # 未知子命令默认拒绝
    print("✓ 边界：空/--help 放行；未知子命令默认拒绝")

    print("\nlark_gate 安全门禁（白名单）自测全部通过 ✅")


if __name__ == "__main__":
    _selftest()
