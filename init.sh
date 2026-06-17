#!/usr/bin/env bash
#
# Emmy init —— 一键初始化
#   ① 体检运行依赖（Homebrew / Node / Claude Code / lark-cli / Python 3.12）
#   ② 检测 Claude Code（大脑）是否已登录可用
#   ③ 检测飞书智能体（手脚）：已有则复用，没有则引导用 lark-cli 创建
#
# 第一版聚焦「体检 + 检测」骨架；自动安装与创建引导会逐步完善。
# 设计原则：幂等（已就绪的跳过）、不擅自改全局环境（先给安装命令，后续版本再做"征得同意后自动装"）。

set -uo pipefail

# ---------- 输出辅助 ----------
BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; DIM=$'\033[2m'; RESET=$'\033[0m'
ok()   { printf "  ${GREEN}✓${RESET} %s\n" "$1"; }
bad()  { printf "  ${RED}✗${RESET} %s\n" "$1"; }
warn() { printf "  ${YELLOW}!${RESET} %s\n" "$1"; }
step() { printf "\n${BOLD}%s${RESET}\n" "$1"; }
hint() { printf "    ${DIM}↳ %s${RESET}\n" "$1"; }

MISSING=0   # 累计阻塞项；>0 则结尾提示用户处理后重跑

# ---------- ① 体检依赖 ----------
check_deps() {
  step "① 体检运行依赖"

  if command -v brew >/dev/null 2>&1; then
    ok "Homebrew（$(brew --prefix)）"
  else
    bad "Homebrew 未安装"
    hint '安装：/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    MISSING=1
  fi

  if command -v claude >/dev/null 2>&1; then
    ok "Claude Code（$(claude --version 2>/dev/null | head -1)）"
  else
    bad "Claude Code 未安装"
    hint "安装：brew install --cask claude-code"
    MISSING=1
  fi

  if command -v lark-cli >/dev/null 2>&1; then
    ok "lark-cli（$(lark-cli --version 2>/dev/null | head -1)）"
  else
    bad "lark-cli 未安装"
    hint "安装：npm i -g @larksuite/cli"
    MISSING=1
  fi

  if command -v node >/dev/null 2>&1; then
    local nv; nv="$(node --version 2>/dev/null)"
    ok "Node（$nv）"
    case "$nv" in
      v18*|v20*|v22*) ;;
      *) hint "建议用 Node 20/22 LTS（当前 $nv 超出官方测试范围，能用但可能有怪问题）" ;;
    esac
  else
    bad "Node 未安装"
    hint "安装：brew install node@22"
    MISSING=1
  fi

  if command -v python3.12 >/dev/null 2>&1; then
    ok "Python 3.12"
  else
    warn "未找到 python3.12（Emmy 监听进程将需要）"
    hint "安装：brew install python@3.12"
  fi
}

# ---------- ② Claude Code（大脑）----------
check_claude() {
  step "② 检测 Claude Code（大脑）"
  if ! command -v claude >/dev/null 2>&1; then
    bad "claude 不在，跳过认证检测"
    return
  fi
  local out
  if out="$(claude -p "ok" --output-format json 2>/dev/null)" \
     && ! printf '%s' "$out" | grep -q '"is_error":[[:space:]]*true'; then
    ok "Claude Code 已登录、可用"
  else
    bad "Claude Code 未登录或认证失效"
    hint "登录：claude  （交互登录）"
    hint "或：claude setup-token  （生成长期 token，后台长挂更稳）"
    MISSING=1
  fi
}

# ---------- ③ 飞书智能体（手脚）：核心 —— auth status 检测幂等 ----------
check_lark_agent() {
  step "③ 检测飞书智能体（手脚）"
  if ! command -v lark-cli >/dev/null 2>&1; then
    bad "lark-cli 不在，跳过"
    return
  fi

  local status_json verdict
  status_json="$(lark-cli auth status 2>/dev/null || true)"

  # 用本机自带 python3 解析 auth status 的 JSON（数据走环境变量，避免与 heredoc stdin 冲突）
  verdict="$(STATUS_JSON="$status_json" python3 <<'PY' 2>/dev/null
import os, json
try:
    d = json.loads(os.environ.get("STATUS_JSON") or "{}")
except Exception:
    print("NONE"); raise SystemExit
app  = d.get("appId") or ""
ids  = d.get("identities") or {}
bot  = (ids.get("bot") or {}).get("status")
user = ids.get("user") or {}
uname = user.get("userName", "")
ustatus = user.get("status")
# Emmy 收发消息靠 bot 身份；user 身份仅用于访问个人资源，未就绪不阻塞复用
if not app:
    print("NONE")
elif bot == "ready":
    if ustatus == "ready":
        print("REUSE_FULL\t%s\t%s" % (app, uname))
    else:
        print("REUSE_BOT\t%s\t%s" % (app, ustatus or "无"))
else:
    print("PARTIAL\t%s\tbot=%s" % (app, bot))
PY
)"
  [ -z "$verdict" ] && verdict="ERROR"

  case "$verdict" in
    REUSE_FULL*)
      ok "已有可用智能体，直接复用（app: $(printf '%s' "$verdict" | cut -f2)，授权人：$(printf '%s' "$verdict" | cut -f3)）"
      ;;
    REUSE_BOT*)
      ok "已有可用智能体，直接复用（app: $(printf '%s' "$verdict" | cut -f2)，机器人身份就绪）"
      hint "user 身份待刷新（$(printf '%s' "$verdict" | cut -f3)）——仅访问个人日历/邮件等资源时需 lark-cli auth login，不影响收发消息"
      ;;
    PARTIAL*)
      warn "检测到 app 但机器人身份未就绪（$(printf '%s' "$verdict" | cut -f3-)）"
      hint "重新授权：lark-cli auth login --as bot"
      MISSING=1
      ;;
    NONE)
      bad "本机还没有飞书智能体"
      hint "新建（推荐）：lark-cli config init --new  —— 浏览器引导建应用、自动申请权限"
      hint "或绑定已有：lark-cli config init  —— 填入 App ID / App Secret"
      MISSING=1
      ;;
    *)
      warn "无法解析 lark-cli auth status 输出，请手动检查：lark-cli auth status"
      ;;
  esac
}

# ---------- 主流程 ----------
main() {
  printf "${BOLD}🦊 Emmy init —— 环境与智能体体检${RESET}\n"
  check_deps
  check_claude
  check_lark_agent

  step "小结"
  if [ "$MISSING" -eq 0 ]; then
    ok "全部就绪！下一步：./start.sh 起 Emmy"
  else
    warn "上面有 ✗ 项——按提示装好/配好后，重跑 ./init.sh（幂等，已就绪的会跳过）"
  fi
}

main "$@"
