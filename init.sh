#!/usr/bin/env bash
#
# Emmy init —— 一键初始化
#   ① 体检运行依赖（Homebrew / Node / Claude Code / lark-cli / Python 3.12）
#   ② 征得同意后，自动安装缺失的工具（brew 包先于 npm 包；幂等；失败给手动命令）
#   ③ 检测 Claude Code（大脑）是否已登录可用
#   ④ 检测飞书智能体（手脚）：已有则复用；没有则引导 lark-cli config init --new 创建
#
# 安全原则：非交互 / 无 tty 环境默认【不】擅自安装或创建（除非 EMMY_YES=1）。
#           不静默改全局环境——装什么先列清单，等你点头。

set -uo pipefail

# ---------- 输出辅助 ----------
BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; DIM=$'\033[2m'; RESET=$'\033[0m'
ok()   { printf "  ${GREEN}✓${RESET} %s\n" "$1"; }
bad()  { printf "  ${RED}✗${RESET} %s\n" "$1"; }
warn() { printf "  ${YELLOW}!${RESET} %s\n" "$1"; }
step() { printf "\n${BOLD}%s${RESET}\n" "$1"; }
hint() { printf "    ${DIM}↳ %s${RESET}\n" "$1"; }
work() { printf "  ${BOLD}↻${RESET} %s\n" "$1"; }

MISSING=0            # 累计阻塞项；>0 则结尾提示处理后重跑
NEED_BREW=0          # Homebrew 缺失（其他安装的前置）
MISSING_NAMES=()     # 缺失工具名（与 MISSING_CMDS 索引对应）
MISSING_CMDS=()      # 对应安装命令

add_missing() { MISSING_NAMES+=("$1"); MISSING_CMDS+=("$2"); }

# 交互确认：EMMY_YES=1 直接是；非 tty 默认否（不擅自动手）；否则问 y/N。
confirm() {
  if [ "${EMMY_YES:-0}" = "1" ]; then return 0; fi
  if [ ! -t 0 ]; then return 1; fi
  printf "  ${BOLD}%s${RESET} [y/N] " "$1"
  local ans=""
  read -r ans || return 1
  case "$ans" in
    [yY] | [yY][eE][sS]) return 0 ;;
    *) return 1 ;;
  esac
}

# ---------- ① 体检依赖（只检测、收集缺失，不立即安装）----------
check_deps() {
  step "① 体检运行依赖"

  if command -v brew >/dev/null 2>&1; then
    ok "Homebrew（$(brew --prefix)）"
  else
    bad "Homebrew 未安装"
    NEED_BREW=1
  fi

  if command -v claude >/dev/null 2>&1; then
    ok "Claude Code（$(claude --version 2>/dev/null | head -1)）"
  else
    bad "Claude Code 未安装"
    add_missing "Claude Code" "brew install --cask claude-code"
  fi

  if command -v node >/dev/null 2>&1; then
    local nv; nv="$(node --version 2>/dev/null)"
    ok "Node（$nv）"
    case "$nv" in
      v18* | v20* | v22*) ;;
      *) hint "建议 Node 20/22 LTS（当前 $nv 超官方测试范围，能用但可能有怪问题）" ;;
    esac
  else
    bad "Node 未安装"
    add_missing "Node" "brew install node"
  fi

  if command -v lark-cli >/dev/null 2>&1; then
    ok "lark-cli（$(lark-cli --version 2>/dev/null | head -1)）"
  else
    bad "lark-cli 未安装"
    add_missing "lark-cli" "npm i -g @larksuite/cli"
  fi

  if command -v python3.12 >/dev/null 2>&1; then
    ok "Python 3.12"
  else
    warn "未找到 python3.12（Emmy 监听进程需要）"
    add_missing "Python 3.12" "brew install python@3.12"
  fi
}

# ---------- ② 征得同意后自动安装 ----------
install_brew() {
  work "安装 Homebrew（需联网，可能要几分钟）…"
  if /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"; then
    # 把 brew 加进当前 shell（Apple Silicon / Intel 自适应），让后续 brew install 可用
    if [ -x /opt/homebrew/bin/brew ]; then
      eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [ -x /usr/local/bin/brew ]; then
      eval "$(/usr/local/bin/brew shellenv)"
    fi
    ok "Homebrew 安装完成"
    return 0
  fi
  bad "Homebrew 安装失败"
  hint "手动安装见 https://brew.sh"
  return 1
}

run_install() {
  local name="$1" cmd="$2"
  work "安装 $name …  ($cmd)"
  if eval "$cmd"; then
    ok "$name 安装完成"
  else
    bad "$name 安装失败"
    hint "请手动执行：$cmd"
    MISSING=1
  fi
}

maybe_install() {
  step "② 安装缺失的工具"

  if [ "$NEED_BREW" -eq 0 ] && [ "${#MISSING_NAMES[@]}" -eq 0 ]; then
    ok "所有依赖已就绪，无需安装"
    return
  fi

  printf "  检测到以下缺失，将要安装：\n"
  [ "$NEED_BREW" -eq 1 ] && printf "    • Homebrew  ${DIM}(官方安装脚本)${RESET}\n"
  local i
  for i in "${!MISSING_NAMES[@]}"; do
    printf "    • %s  ${DIM}(%s)${RESET}\n" "${MISSING_NAMES[$i]}" "${MISSING_CMDS[$i]}"
  done

  if ! confirm "现在自动安装以上工具吗？"; then
    warn "已跳过自动安装"
    hint "可手动执行上面的命令；或设 EMMY_YES=1 重跑。装好后 ./init.sh 会自动跳过已装的（幂等）"
    MISSING=1
    return
  fi

  # 1) Homebrew 前置（其余 brew/npm 安装都依赖它）
  if [ "$NEED_BREW" -eq 1 ]; then
    install_brew || { MISSING=1; return; }
  fi
  # 2) 先装 brew 包（claude / node / python），让 node→npm 就绪
  for i in "${!MISSING_NAMES[@]}"; do
    case "${MISSING_CMDS[$i]}" in
      npm\ *) : ;;  # npm 包留到第二轮（要等 node）
      *) run_install "${MISSING_NAMES[$i]}" "${MISSING_CMDS[$i]}" ;;
    esac
  done
  # 3) 再装 npm 包（lark-cli）
  for i in "${!MISSING_NAMES[@]}"; do
    case "${MISSING_CMDS[$i]}" in
      npm\ *) run_install "${MISSING_NAMES[$i]}" "${MISSING_CMDS[$i]}" ;;
    esac
  done
}

# ---------- ③ Claude Code（大脑）----------
check_claude() {
  step "③ 检测 Claude Code（大脑）"
  if ! command -v claude >/dev/null 2>&1; then
    bad "claude 不在，跳过认证检测（先装上再说）"
    MISSING=1
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

# ---------- ④ 飞书智能体（手脚）：检测 → 复用 / 引导创建 ----------
check_lark_agent() {
  step "④ 检测飞书智能体（手脚）"
  if ! command -v lark-cli >/dev/null 2>&1; then
    bad "lark-cli 不在，跳过（先装上再说）"
    MISSING=1
    return
  fi

  local status_json verdict
  status_json="$(lark-cli auth status 2>/dev/null || true)"

  # 用本机 python3 解析（数据走环境变量，避免与 heredoc stdin 冲突）
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
      if confirm "现在用 lark-cli 创建一个吗？（会打开浏览器引导，按提示完成即可）"; then
        work "运行 lark-cli config init --new …"
        if lark-cli config init --new; then
          ok "智能体创建完成"
          hint "接着按 ONBOARDING.md 在飞书后台：开机器人能力、订阅 im.message.receive_v1、选长连接、发布、拉机器人进群"
        else
          bad "创建未完成"
          hint "可重试：lark-cli config init --new  （或绑定已有：lark-cli config init）"
          MISSING=1
        fi
      else
        hint "稍后手动创建：lark-cli config init --new"
        hint "或绑定已有 app：lark-cli config init  （填 App ID / App Secret）"
        MISSING=1
      fi
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
  maybe_install
  check_claude
  check_lark_agent

  step "小结"
  if [ "$MISSING" -eq 0 ]; then
    ok "全部就绪！下一步：python3 run.py（或 ./start.sh）起 Emmy"
  else
    warn "上面有 ✗ 项——按提示处理后，重跑 ./init.sh（幂等，已就绪的会跳过）"
  fi
}

main "$@"
