#!/usr/bin/env bash
# 一键初始化 Appen 质检自动化环境。
# 目标系统：Rocky Linux 9.6 / RHEL 9 系（华为云竞价实例）。
# 幂等：可重复运行。需要 root 或有 sudo 权限的用户。
#
# 用法：
#   git clone git@github.com:lizhang-bot/appen-ch-eng-6000.git
#   cd appen-ch-eng-6000
#   bash bootstrap.sh
#
# 做的事：系统依赖（python3.11 / chromium 运行库 / ffmpeg）→ venv → pip 依赖
#         → Playwright chromium → 初始化 .env（交互填讯飞凭据）。

set -euo pipefail
cd "$(dirname "$0")"

PY=python3.11
VENV=venv

log()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[warn] %s\033[0m\n' "$*"; }

if ! command -v dnf >/dev/null 2>&1; then
    echo "本脚本面向 RHEL/Rocky 系（需要 dnf）。其它发行版请手动装依赖。" >&2
    exit 1
fi
if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

# ---------------------------------------------------------------- 1. 系统依赖
log "启用 EPEL + CRB 软件源"
$SUDO dnf -y install dnf-plugins-core epel-release || true
$SUDO dnf config-manager --set-enabled crb 2>/dev/null \
  || $SUDO dnf config-manager --set-enabled powertools 2>/dev/null || true

log "安装 Python $PY 与基础工具"
$SUDO dnf -y install python3.11 python3.11-pip git curl tar xz || true
if ! command -v "$PY" >/dev/null 2>&1; then
    warn "$PY 不可用，回退到系统 python3（$(python3 --version 2>&1)）"
    PY=python3
fi

log "安装 Playwright chromium 运行时库"
# --setopt=strict=0：个别包名在本版本缺失时跳过，不让整条事务失败
$SUDO dnf -y --setopt=strict=0 install \
    nss nspr atk at-spi2-atk at-spi2-core cups-libs \
    libdrm libxkbcommon libXcomposite libXdamage libXext libXfixes \
    libXrandr libXScrnSaver libXtst libxcb libxshmfence mesa-libgbm \
    pango cairo alsa-lib gtk3 expat glib2 dbus-libs liberation-fonts \
  || warn "部分 chromium 依赖未装上；若启动报缺 .so，按报错 dnf install 对应库"

# ---------------------------------------------------------------- 2. ffmpeg
log "准备 ffmpeg（qa_pipeline 转 wav→pcm 用）"
if command -v ffmpeg >/dev/null 2>&1; then
    echo "已有 ffmpeg：$(command -v ffmpeg)"
elif $SUDO dnf -y install ffmpeg-free >/dev/null 2>&1 && command -v ffmpeg >/dev/null 2>&1; then
    echo "已装 ffmpeg-free"
else
    warn "dnf 装不上 ffmpeg，改用静态构建"
    tmp=$(mktemp -d)
    curl -fsSL https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz \
        -o "$tmp/ff.tar.xz"
    tar -xJf "$tmp/ff.tar.xz" -C "$tmp"
    d=$(find "$tmp" -maxdepth 1 -type d -name 'ffmpeg-*-static' | head -1)
    $SUDO install -m 755 "$d/ffmpeg" "$d/ffprobe" /usr/local/bin/
    rm -rf "$tmp"
    echo "静态 ffmpeg → /usr/local/bin/ffmpeg"
fi

# ---------------------------------------------------------------- 3. venv + pip
log "创建虚拟环境 $VENV（$PY）"
[ -d "$VENV" ] || "$PY" -m venv "$VENV"
set +u; source "$VENV/bin/activate"; set -u   # activate 脚本对 set -u 不友好，临时关掉
python -m pip install --upgrade pip
pip install -r requirements.txt

# ---------------------------------------------------------------- 4. 浏览器
log "下载 Playwright chromium（依赖已在上面手动装，故不用 --with-deps）"
python -m playwright install chromium

# ---------------------------------------------------------------- 5. .env
log "初始化 .env"
if [ -f .env ]; then
    echo ".env 已存在，跳过"
else
    cp .env.example .env
    if [ -t 0 ]; then
        echo "填入凭据（直接回车则保留占位符，稍后手动改 .env）："
        read -rp "  XF_APPID: "      v_appid || true
        read -rp "  XF_API_SECRET: " v_secret || true
        read -rp "  XF_API_KEY: "    v_key || true
        read -rp "  DINGTALK_WEBHOOK（可空）: " v_ding || true
        [ -n "${v_appid:-}" ]  && sed -i "s|^XF_APPID=.*|XF_APPID=${v_appid}|"          .env
        [ -n "${v_secret:-}" ] && sed -i "s|^XF_API_SECRET=.*|XF_API_SECRET=${v_secret}|" .env
        [ -n "${v_key:-}" ]    && sed -i "s|^XF_API_KEY=.*|XF_API_KEY=${v_key}|"        .env
        if [ -n "${v_ding:-}" ]; then
            ding_esc=$(printf '%s' "$v_ding" | sed 's/[&|]/\\&/g')   # 转义 & | 供 sed 安全替换
            sed -i "s|^DINGTALK_WEBHOOK=.*|DINGTALK_WEBHOOK=${ding_esc}|" .env
        fi
        echo "已写入 .env"
    else
        warn "非交互模式，.env 仍是占位符，请手动编辑填入讯飞凭据"
    fi
fi

# ---------------------------------------------------------------- 完成
log "完成 ✅"
cat <<'EOF'
后续：
  source venv/bin/activate

  # ① watch_jobs.sh 里的 COOKIE 是过期占位，跑前先贴一份新的：
  #    Chrome F12 → Network → 任一 detail 请求 → Copy as cURL
  #    → 用 -b '...' 里那整段替换 watch_jobs.sh 的 COOKIE='...'
  #    验证有效性（200=有效 / 401=过期）见 CLAUDE.md「常用命令速查」

  # ② 抢任务（服务器无显示器会自动切 headless）：
  python tools/grab_task.py --url "<任务页URL>" --tabs 2 --interval 0.2

  # ③ 抢到后：collect → evaluate（命令见 CLAUDE.md「用户手动操作步骤」）
EOF
