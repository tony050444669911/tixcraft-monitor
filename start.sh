#!/bin/bash
# 自動重啟版啟動腳本
# 無論 monitor.py 因為任何原因停掉，都會自動重新啟動

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="/tmp/monitor_output.log"

echo "$(date '+%Y-%m-%d %H:%M:%S') 監控守護程序啟動" >> "$LOG"

while true; do
    echo "$(date '+%Y-%m-%d %H:%M:%S') [start.sh] 啟動 monitor.py" >> "$LOG"
    python3 "$SCRIPT_DIR/monitor.py" >> "$LOG" 2>&1

    EXIT_CODE=$?
    if [ $EXIT_CODE -eq 0 ]; then
        # 正常結束（使用者按 Ctrl+C）→ 不重啟
        echo "$(date '+%Y-%m-%d %H:%M:%S') [start.sh] 正常結束，停止守護" >> "$LOG"
        break
    fi

    echo "$(date '+%Y-%m-%d %H:%M:%S') [start.sh] 程序異常退出（code $EXIT_CODE），10 秒後重啟..." >> "$LOG"
    sleep 10
done
