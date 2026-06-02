#!/usr/bin/env bash
# 轮询 Appen collect-web 的 my-job/get。
# remainQuantity 由 0 变 >0 时：弹 macOS 通知 + 响铃 + Chrome 打开任务页（仅在跳变瞬间触发一次，避免开一堆 tab）。
# Cookie 过期时（HTTP 非 200）请到 Chrome DevTools 重新 copy as cURL 覆盖下方变量。
#
# 用法：
#   ./watch_jobs.sh          # 默认每 5s 轮询
#   ./watch_jobs.sh 10       # 每 10s 轮询
#   ./watch_jobs.sh 0.5      # 每 0.5s 轮询（高频）

set -u

URL='https://collect-web.appen.com.cn/api-gw/collect/my-job/get'
TASK_URL='https://collect-web.appen.com.cn/collect/qa?flowId=2058728701937496080&jobId=2059525664826957849&locale=zh-CN'
REFERER="$TASK_URL"
PAYLOAD='{"jobId":"2059525664826957849","flowId":"2058728701937496080"}'
COOKIE='_ga=GA1.1.0.0; Authorization=PASTE_YOUR_AUTHORIZATION_FROM_COPY_AS_CURL; _ga_0N0HGNC38M=GS2.1.0'
INTERVAL=${1:-5}           # 第一个参数：轮询间隔（秒），默认 5
EXPIRY_WARN_SECONDS=1800   # 剩余 < 30 分钟时弹通知提醒换 cookie
prev_remain=0
warned_expiry=0

# 从 COOKIE 里抽出 Authorization JWT
extract_jwt() {
    printf '%s' "$COOKIE" | tr ';' '\n' | sed -n 's/^[[:space:]]*Authorization=\(.*\)$/\1/p'
}

check_token() {
    # 返回 "<remaining_seconds>\t<expires_human>"，失败时返回空
    extract_jwt | python3 "$(dirname "$0")/decode_token.py" 2>/dev/null
}

echo "开始轮询，每 ${INTERVAL}s 一次。Ctrl+C 停止。"

# 启动时打印 token 剩余有效期
token_info=$(check_token)
if [ -n "$token_info" ]; then
    rem_sec=$(echo "$token_info" | cut -f1)
    exp_human=$(echo "$token_info" | cut -f2)
    if [ "$rem_sec" -gt 0 ]; then
        hours=$(( rem_sec / 3600 ))
        mins=$(( (rem_sec % 3600) / 60 ))
        echo "Token 过期: $exp_human  (还剩 ${hours}h ${mins}m)"
    else
        echo "⚠️  Token 已过期 $((-rem_sec / 60)) 分钟，请立即更新 cookie"
    fi
else
    echo "⚠️  无法解析 Authorization token，跳过有效期检查"
fi

# 启动自检：弹一次通知 + 响铃，确认通知通道工作
# 注意：osascript 不要放 background（&），不然 macOS 通知中心会丢弃
osascript -e "display notification \"watch 已启动，每 ${INTERVAL}s 轮询一次\" with title \"Appen 有任务\" sound name \"Glass\""
afplay /System/Library/Sounds/Glass.aiff 2>/dev/null &

while true; do
    resp=$(curl -sS -w '\n%{http_code}' "$URL" \
        -H 'Accept: application/json, text/plain, */*' \
        -H 'Content-Type: application/json' \
        -b "$COOKIE" \
        -H 'Origin: https://collect-web.appen.com.cn' \
        -H "Referer: $REFERER" \
        -H 'User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/148.0.0.0 Safari/537.36' \
        --data-raw "$PAYLOAD" 2>&1)

    http_code=$(printf '%s' "$resp" | tail -n1)
    body=$(printf '%s' "$resp" | sed '$d')
    ts=$(date +'%H:%M:%S')

    if [ "$http_code" != "200" ]; then
        echo "[$ts] HTTP $http_code — 大概率 Cookie 过期，重新从 Chrome copy as cURL 后更新本脚本"
        sleep $INTERVAL
        continue
    fi

    remain=$(printf '%s' "$body" | python3 -c \
        'import sys,json; d=json.load(sys.stdin); print(d.get("data",{}).get("remainQuantity",""))' \
        2>/dev/null)

    if [ -z "$remain" ]; then
        echo "[$ts] 解析失败：${body:0:200}"
        sleep $INTERVAL
        continue
    fi

    if [ "$remain" -gt 0 ]; then
        if [ "$prev_remain" -eq 0 ]; then
            echo "[$ts] 🔔 有任务！remainQuantity=$remain — 打开 Chrome"
            osascript -e "display notification \"remainQuantity=${remain}，快去抢！\" with title \"Appen 有任务\" sound name \"Glass\"" &
            afplay /System/Library/Sounds/Glass.aiff 2>/dev/null &
            open -a "Google Chrome" "$TASK_URL"
        else
            echo "[$ts] 仍有任务 remain=${remain}（Chrome 已开过，不再重复）"
        fi
    else
        echo "[$ts] remain=0"
    fi
    prev_remain=$remain

    # Token 临期提醒（仅触发一次）
    if [ "$warned_expiry" -eq 0 ]; then
        token_info=$(check_token)
        if [ -n "$token_info" ]; then
            rem_sec=$(echo "$token_info" | cut -f1)
            if [ "$rem_sec" -lt "$EXPIRY_WARN_SECONDS" ]; then
                echo "[$ts] ⚠️  Token 剩余 ${rem_sec}s，准备换 cookie"
                osascript -e "display notification \"Token 还剩 $((rem_sec / 60)) 分钟，重新 copy as cURL\" with title \"Appen cookie 临期\" sound name \"Sosumi\"" &
                warned_expiry=1
            fi
        fi
    fi

    sleep $INTERVAL
done
