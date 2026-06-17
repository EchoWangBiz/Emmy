#!/usr/bin/env bash
#
# Emmy start.sh —— launchd 后台守护管理
#
#   ./start.sh [start]      安装 LaunchAgent 并启动（登录即自动运行、崩溃自愈）
#   ./start.sh stop         停止并卸载
#   ./start.sh restart      重启
#   ./start.sh status       看运行状态
#   ./start.sh logs         跟踪日志（tail -f）
#   ./start.sh fg           前台运行（调试，不走 launchd）
#   ./start.sh print-plist  打印将生成的 plist（不安装，便于检查）
#
# launchd 关键点（踩过的坑都写死在 plist 里）：
#   - ProgramArguments / WorkingDirectory 用绝对路径（launchd 不继承登录 shell）
#   - EnvironmentVariables.PATH 必含 $(brew --prefix)/bin，否则找不到 claude/lark-cli/node
#   - KeepAlive {Crashed:true, SuccessfulExit:false}：崩溃才拉起，手动 stop 不反复重启
#   - 用 LaunchAgent（非 LaunchDaemon）：跑在用户 GUI 会话，才能读 Keychain / claude 登录态

set -uo pipefail

# ---------- 输出辅助 ----------
BOLD=$'\033[1m'; GREEN=$'\033[32m'; RED=$'\033[31m'; DIM=$'\033[2m'; RESET=$'\033[0m'
ok()   { printf "  ${GREEN}✓${RESET} %s\n" "$1"; }
err()  { printf "  ${RED}✗${RESET} %s\n" "$1"; }
info() { printf "    ${DIM}↳ %s${RESET}\n" "$1"; }

# ---------- 路径与常量 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.emmy.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/emmy"
OUT_LOG="$LOG_DIR/out.log"
ERR_LOG="$LOG_DIR/err.log"
DOMAIN="gui/$(id -u)"

# python 绝对路径（优先 3.12；launchd 不继承 PATH，必须写死绝对路径）
PYTHON="$(command -v python3.12 2>/dev/null || command -v python3 2>/dev/null || true)"
# brew prefix，用于 plist 的 PATH（Apple Silicon /opt/homebrew，Intel /usr/local）
if command -v brew >/dev/null 2>&1; then
  BREW_BIN="$(brew --prefix)/bin"
else
  BREW_BIN="/usr/local/bin"
fi

# ---------- 生成 plist 文本 ----------
emit_plist() {
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$SCRIPT_DIR/run.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>Crashed</key>
        <true/>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>$OUT_LOG</string>
    <key>StandardErrorPath</key>
    <string>$ERR_LOG</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$BREW_BIN:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
EOF
}

install_plist() {
  mkdir -p "$LOG_DIR" "$(dirname "$PLIST")"
  emit_plist > "$PLIST"
}

# ---------- 命令 ----------
cmd_start() {
  [ -n "$PYTHON" ] || { err "找不到 python3，请先 ./init.sh"; exit 1; }
  [ -f "$SCRIPT_DIR/run.py" ] || { err "找不到 $SCRIPT_DIR/run.py"; exit 1; }
  install_plist
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true   # 幂等：先卸旧再装
  if launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null; then
    ok "Emmy 已安装并启动（登录即自动运行、崩溃自愈）"
  else
    launchctl kickstart -k "$DOMAIN/$LABEL" 2>/dev/null || true
    ok "Emmy 已（重新）启动"
  fi
  info "状态：./start.sh status    日志：./start.sh logs    停止：./start.sh stop"
}

cmd_stop() {
  if launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null; then
    ok "Emmy 已停止并卸载"
  else
    info "Emmy 未在运行"
  fi
}

cmd_status() {
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    ok "Emmy 运行中（$LABEL）"
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state =|pid =|last exit" | sed 's/^/    /'
  else
    err "Emmy 未运行（用 ./start.sh start 启动）"
  fi
}

cmd_logs() {
  [ -f "$OUT_LOG" ] || { info "还没有日志（$OUT_LOG）"; exit 0; }
  tail -n 50 -f "$OUT_LOG" "$ERR_LOG"
}

cmd_fg() {
  [ -n "$PYTHON" ] || { err "找不到 python3，请先 ./init.sh"; exit 1; }
  exec "$PYTHON" "$SCRIPT_DIR/run.py"
}

usage() {
  printf "用法: %s [start|stop|restart|status|logs|fg|print-plist]\n" "$(basename "$0")"
}

case "${1:-start}" in
  start | "")    cmd_start ;;
  stop)          cmd_stop ;;
  restart)       cmd_stop; sleep 1; cmd_start ;;
  status)        cmd_status ;;
  logs)          cmd_logs ;;
  fg | --foreground) cmd_fg ;;
  print-plist)   emit_plist ;;
  -h | --help | help) usage ;;
  *)             usage; exit 1 ;;
esac
