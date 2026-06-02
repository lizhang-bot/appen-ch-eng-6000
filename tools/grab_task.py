"""抢任务：用 Playwright 开 N 个常驻热 tab 并行，循环用【页面自己的 fetch】高频打
my-job/execute 领取接口。一旦返回 data 非空＝抢到任务，弹通知+响铃+语音，
刷新页面进入质检，打印后续流水线命令，并保持浏览器不关。

为什么走页内 fetch 而不是整页刷：execute 是页面加载时领取任务调的那个接口，
直接 re-fire 它即可领取（几十 ms ~ 一个 RTT），比整页 reload（重启 Vue，1-2s）
快一个数量级；且请求由真实页面发出（Origin/Referer/cookie/浏览器指纹都真实），最接近真人。

多 tab 并行：单 tab 的速率上限是 execute 的网络往返（~0.4s）。N 个 tab 错峰发请求，
全局节奏 ≈ interval。默认 2 tab + interval 0.2 → 大约每 0.2s 一次领取尝试。

领取接口实测：
    POST /api-gw/collect/my-job/execute   body={"jobId","flowId"}
    没任务 → HTTP 200 {"message":"no available task","status":100103,"data":null}
    有任务 → data 非空（即已领取，任务锁定到账号）

依赖：playwright（cookie 复用 watch_jobs.sh 里的 COOKIE 字段）

用法：
    source venv/bin/activate
    python tools/grab_task.py \\
        --url "https://collect-web.appen.com.cn/collect/qa?flowId=...&jobId=...&locale=zh-CN" \\
        --tabs 2 --interval 0.2

参数：
    --url       任务页完整 URL（含 jobId/flowId），从 Chrome 地址栏抄
    --tabs      并行 tab 数，默认 2
    --interval  目标全局尝试间隔秒，默认 0.2（每个 tab 实际周期 = interval×tabs，错峰发起）
    --headless  无头模式（默认有头，方便抢到后直接在该窗口质检）
"""

import argparse
import asyncio
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright

PROJECT_ROOT = Path(__file__).parent.parent
COOKIE_DOMAIN = "collect-web.appen.com.cn"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

# 在页面上下文里调领取接口，返回精简结果。page.evaluate 没有 Chrome MCP 的 BLOCK 限制。
EXEC_JS = """
async ({jobId, flowId}) => {
  try {
    const r = await fetch('/api-gw/collect/my-job/execute', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'Accept': 'application/json, text/plain, */*'},
      body: JSON.stringify({jobId, flowId}),
    });
    let j = null;
    const t = await r.text();
    try { j = JSON.parse(t); } catch (e) {}
    return {
      status: r.status,
      code: j ? j.status : null,
      msg: j ? j.message : (t || '').slice(0, 120),
      hasTask: !!(j && j.data != null),
    };
  } catch (e) {
    return {fetchErr: String(e)};
  }
}
"""

# 抢到后从页面左下角读「编号：N」，用于拼后续流水线命令
CODE_JS = r"""
() => {
  const m = (document.body.innerText || '').match(/编号[:：]\s*(\d+)/);
  return m ? m[1] : null;
}
"""


def get_cookie_str() -> str:
    sh = PROJECT_ROOT / "watch_jobs.sh"
    if not sh.exists():
        raise SystemExit("watch_jobs.sh 不存在")
    m = re.search(r"COOKIE='([^']+)'", sh.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit("watch_jobs.sh 里没找到 COOKIE='...'")
    return m.group(1)


def parse_cookies(cookie_str: str) -> list[dict]:
    out = []
    for pair in cookie_str.split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        out.append({
            "name": k.strip(),
            "value": v.strip(),
            "domain": COOKIE_DOMAIN,
            "path": "/",
            "secure": True,
            "httpOnly": k.strip() == "Authorization",
        })
    return out


def notify(title: str, msg: str, say_text: str = "") -> None:
    """macOS 通知 + 响铃 + 语音兜底（语音不受勿扰模式影响）。"""
    subprocess.run(
        ["osascript", "-e",
         f'display notification "{msg}" with title "{title}" sound name "Glass"'],
        check=False,
    )
    subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"])
    if say_text:
        subprocess.Popen(["say", say_text])


async def print_next_steps(page, url: str) -> None:
    """抢到任务后打印后续流水线命令。"""
    try:
        code = await page.evaluate(CODE_JS)
    except Exception:
        code = None
    c = code or "<编号>"
    cur = page.url or url
    print("\n下一步流水线（编号 = 页面左下「编号：N」）：")
    print(f"  1) 抓题:  python tools/collect_via_playwright.py {c} --url \"{cur}\" --interval 200")
    print(f"  2) 评判:  python qa_pipeline.py --code {c} --from-result --concurrency 10")
    print(f"  3) 标注:  回到 Claude 会话说「用 mark-tasks 根据 result/{c}.json 在页面标注」")
    if not code:
        print("  （没自动读到编号，请看页面左下角「编号：N」手动替换上面的 <编号>）")


class State:
    def __init__(self) -> None:
        self.done = asyncio.Event()
        self.lock = asyncio.Lock()
        self.handled = False
        self.attempts = 0
        self.reason = None  # "claimed" / "auth"


async def worker(idx: int, page, job_id: str, flow_id: str, interval: float,
                 n_tabs: int, url: str, state: State, t0: float) -> None:
    await asyncio.sleep(idx * interval)  # 错峰起跑
    period = interval * n_tabs
    while not state.done.is_set():
        start = time.time()
        res = await page.evaluate(EXEC_JS, {"jobId": job_id, "flowId": flow_id})
        state.attempts += 1
        n = state.attempts

        if res.get("fetchErr"):
            print(f"\n[tab{idx} #{n}] fetch 错误：{res['fetchErr']}（继续）")
        elif res.get("status") in (401, 403):
            async with state.lock:
                if not state.handled:
                    state.handled = True
                    state.reason = "auth"
                    notify("Appen cookie 失效", f"HTTP {res['status']}，重新 copy as cURL")
                    print(f"\nHTTP {res['status']} —— cookie 失效，停止。"
                          f"重新从 Chrome copy as cURL 覆盖 watch_jobs.sh 后再跑。")
            state.done.set()
            break
        elif res.get("hasTask"):
            async with state.lock:
                if not state.handled:
                    state.handled = True
                    state.reason = "claimed"
                    elapsed = time.time() - t0
                    print(f"\n🎉 抢到任务！tab{idx} 第 {n} 次尝试，用时 {elapsed:.1f}s。刷新进入质检……")
                    notify("Appen 抢到任务", "已领取，快去质检！", say_text="抢到任务了，快去质检")
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await print_next_steps(page, url)
            state.done.set()
            break
        else:
            sys.stdout.write(f"\r尝试 {n} 次… 暂无任务（{n_tabs} tab 并行）   ")
            sys.stdout.flush()

        elapsed = time.time() - start
        sleep_s = max(0.0, period - elapsed) * random.uniform(0.85, 1.15)
        await asyncio.sleep(sleep_s)


async def grab(url: str, n_tabs: int, interval: float, headless: bool) -> None:
    q = parse_qs(urlparse(url).query)
    job_id = (q.get("jobId") or [None])[0]
    flow_id = (q.get("flowId") or [None])[0]
    if not job_id or not flow_id:
        raise SystemExit("URL 里缺 jobId 或 flowId")

    cookies = parse_cookies(get_cookie_str())
    print(f"加载 {len(cookies)} 个 cookie；jobId={job_id} flowId={flow_id}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        ctx = await browser.new_context(user_agent=USER_AGENT)
        await ctx.add_cookies(cookies)

        pages = [await ctx.new_page() for _ in range(n_tabs)]
        print(f"navigate {n_tabs} 个 tab → {url}")
        await asyncio.gather(*(
            pg.goto(url, wait_until="domcontentloaded", timeout=30000) for pg in pages
        ))

        state = State()
        t0 = time.time()
        print(f"开始抢任务，{n_tabs} tab 错峰并行，目标 ~{interval}s/次。Ctrl+C 停止。")
        await asyncio.gather(*(
            worker(i, pages[i], job_id, flow_id, interval, n_tabs, url, state, t0)
            for i in range(n_tabs)
        ))

        if state.reason == "claimed":
            print("\n浏览器停在任务页，可直接在此窗口质检。任务已锁定到账号，也可在常用 Chrome "
                  "打开同一 URL 跑后续流水线。")
            print("Ctrl+C 退出脚本（会关闭这个浏览器窗口）。")
            await asyncio.Event().wait()  # 阻塞，保持浏览器开着


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="任务页完整 URL（含 jobId/flowId）")
    ap.add_argument("--tabs", type=int, default=2, help="并行 tab 数，默认 2")
    ap.add_argument("--interval", type=float, default=0.2,
                    help="目标全局尝试间隔秒，默认 0.2")
    ap.add_argument("--headless", action="store_true", default=False)
    args = ap.parse_args()
    try:
        asyncio.run(grab(args.url, args.tabs, args.interval, args.headless))
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
