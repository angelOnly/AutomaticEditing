#!/usr/bin/env bash
# 杀掉正在运行的 Web 工作台（run_web.py / uvicorn），释放 7860-7879 端口，
# 确保重启后加载到最新代码。
#
# 用法（在 Git Bash 里）：
#   bash kill_web.sh
# 然后再运行你的启动命令：
#   start "web7861" cmd /k "python run_web.py"
#
# 说明：Windows 下 taskkill 的参数要用 // 前缀，避免 Git Bash 把 /PID 当成路径。

set -u

echo "[1/3] 结束监听 7860-7879 端口的进程（含进程树）..."
netstat -ano \
  | awk '$4=="LISTENING" && $2 ~ /:(786[0-9]|787[0-9])$/ && $5+0>4 && !seen[$5]++ {print $5}' \
  | while read -r pid; do
      [ -n "$pid" ] || continue
      echo "  taskkill PID=$pid"
      taskkill //PID "$pid" //T //F >/dev/null 2>&1 || true
    done

echo "[2/3] 兜底：结束还没监听端口的 run_web.py（例如卡在启动前预检）..."
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { \$_.CommandLine -match 'run_web\.py' } | ForEach-Object { Write-Host ('  taskkill PID=' + \$_.ProcessId); Stop-Process -Id \$_.ProcessId -Force -ErrorAction SilentlyContinue }" 2>/dev/null || true

echo "[3/3] 关闭标题为 web7861 的旧 cmd 窗口（cmd /k 不会自动关）..."
taskkill //F //T //FI "WINDOWTITLE eq web7861" >/dev/null 2>&1 || true

# 复查端口是否已释放
left=$(netstat -ano | awk '$4=="LISTENING" && $2 ~ /:(786[0-9]|787[0-9])$/ {print $5}' | sort -u | tr '\n' ' ')
if [ -n "${left// /}" ]; then
  echo "[!] 仍有进程占用端口（PID: $left）。若是权限问题，用管理员身份的 Git Bash 再跑一次。"
else
  echo "[OK] 端口 7860-7879 已释放，可以重新启动了。"
fi
