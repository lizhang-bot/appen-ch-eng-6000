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

抢到任务 / cookie 失效时，除本机通知外还推送钉钉群机器人（.env 的 DINGTALK_WEBHOOK，
content 含关键字「报告」以过群机器人校验）——服务器无 GUI 时这是触达手机的主要方式。

依赖：playwright + python-dotenv（cookie 复用 watch_jobs.sh 里的 COOKIE 字段）

多工作流：脚本内置 WORKFLOWS 列表（A3189/A3244/A3245…）。不传 --url 时默认轮询全部——
每个工作流占一个常驻 tab，各 tab 错峰起跑，全局形成 A→B→C→A… 轮换：interval=0.2、
3 个工作流时 0.2s 试 A、0.4s 试 B、0.6s 试 C、0.8s 回 A，每个工作流每 0.6s 试一次。
execute 只认 body 里的 jobId/flowId，与页面 URL 无关。新增工作流：在 WORKFLOWS 里加一行。

用法：
    source venv/bin/activate
    python tools/grab_task.py                          # 轮询所有预置工作流（默认）
    python tools/grab_task.py --only A3244,A3245        # 只轮询指定工作流
    python tools/grab_task.py --url "https://.../qa?flowId=...&jobId=..."  # 临时单个

参数：
    --url       临时工作流 URL（含 jobId/flowId），可重复；不填则用内置 WORKFLOWS 全部
    --only      只轮询指定预置工作流，逗号分隔 code（如 A3244,A3245）
    --tabs      总 tab 数，默认 max(2, 工作流数)；多于工作流数时每工作流多开 tab
    --interval  全局尝试间隔秒，默认 0.2（每隔此时长轮换到下一个工作流）
    --headless  无头模式（默认有头，方便抢到后直接在该窗口质检）
"""

import argparse
import asyncio
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from playwright.async_api import async_playwright

PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env")          # 读 DINGTALK_WEBHOOK 等配置
COOKIE_DOMAIN = "collect-web.appen.com.cn"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

# 预置工作流（领任务页 URL，含 jobId/flowId）。不传 --url 时默认轮询全部。
# 新增工作流：复制一行、改 code/name/url 即可。
WORKFLOWS = [
    {"code": "A3189", "name": "中英信实",
     "url": "https://collect-web.appen.com.cn/collect/qa?flowId=2058728701937496080&jobId=2059525664826957849&locale=zh-CN"},
    {"code": "A3244", "name": "中英中哈",
     "url": "https://collect-web.appen.com.cn/collect/qa?flowId=2061734459464458251&jobId=2061735070294781978&locale=zh-CN"},
    {"code": "A3245", "name": "中英莓树",
     "url": "https://collect-web.appen.com.cn/collect/qa?flowId=2061734682458824727&jobId=2061735186987458587&locale=zh-CN"},
]

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


def build_workflow(url: str, name: str = "", code: str = "") -> dict:
    """从 URL 解析 jobId/flowId，组装工作流字典。"""
    q = parse_qs(urlparse(url).query)
    job_id = (q.get("jobId") or [None])[0]
    flow_id = (q.get("flowId") or [None])[0]
    if not job_id or not flow_id:
        raise SystemExit(f"URL 缺 jobId 或 flowId：{url}")
    return {"name": name or code or job_id, "code": code, "url": url,
            "job_id": job_id, "flow_id": flow_id}


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
    """macOS 通知 + 响铃 + 语音兜底；在没有这些命令的系统（如 Linux 服务器）上静默跳过。"""
    def _try(cmd):
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    _try(["osascript", "-e",
          f'display notification "{msg}" with title "{title}" sound name "Glass"'])
    _try(["afplay", "/System/Library/Sounds/Glass.aiff"])
    if say_text:
        _try(["say", say_text])


# 钉钉群机器人「关键字」安全设置：content 必须含此词，否则被服务端拒收
DINGTALK_KEYWORD = "报告"


def dingtalk(content: str) -> None:
    """推送钉钉群机器人。webhook 从 .env 的 DINGTALK_WEBHOOK 读，未配置则跳过。
    服务器无 GUI，这是触达手机的主要通知方式。"""
    url = os.environ.get("DINGTALK_WEBHOOK", "").strip()
    if not url:
        return
    if DINGTALK_KEYWORD not in content:        # 兜底：保证含关键字，否则钉钉拒收
        content = f"{DINGTALK_KEYWORD}：{content}"
    data = json.dumps({"msgtype": "text", "text": {"content": content}}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode("utf-8", "ignore")
        if '"errcode":0' not in body:
            print(f"\n[钉钉] 发送可能失败：{body[:200]}")
    except Exception as e:
        print(f"\n[钉钉] 发送异常：{e}")


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


async def worker(idx: int, page, wf: dict, interval: float,
                 n_tabs: int, state: State, t0: float) -> None:
    await asyncio.sleep(idx * interval)  # 错峰起跑：tab i 在 i*interval 首发，全局形成工作流轮换
    period = interval * n_tabs
    label = f"{wf['code']} {wf['name']}".strip()
    while not state.done.is_set():
        start = time.time()
        res = await page.evaluate(EXEC_JS, {"jobId": wf["job_id"], "flowId": wf["flow_id"]})
        state.attempts += 1
        n = state.attempts

        if res.get("fetchErr"):
            print(f"\n[tab{idx} {label} #{n}] fetch 错误：{res['fetchErr']}（继续）")
        elif res.get("status") in (401, 403):
            async with state.lock:
                if not state.handled:
                    state.handled = True
                    state.reason = "auth"
                    notify("Appen cookie 失效", f"HTTP {res['status']}，重新 copy as cURL")
                    dingtalk(f"Appen 抢任务报告\ncookie 失效（HTTP {res['status']}），"
                             f"抢任务脚本已停止，请更新 cookie 后重启。")
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
                    print(f"\n🎉 抢到任务！[{label}] tab{idx} 第 {n} 次尝试，用时 {elapsed:.1f}s。刷新进入质检……")
                    notify("Appen 抢到任务", f"{label} 已领取，快去质检！",
                           say_text="抢到任务了，快去质检")
                    dingtalk(f"Appen 抢任务报告\n[{label}] 已抢到任务（第 {n} 次尝试，"
                             f"用时 {elapsed:.1f}s），请尽快进入质检。\n{wf['url']}")
                    await page.goto(wf["url"], wait_until="domcontentloaded", timeout=30000)
                    await print_next_steps(page, wf["url"])
            state.done.set()
            break
        else:
            sys.stdout.write(f"\r尝试 {n} 次… 暂无任务（{n_tabs} tab 轮询）   ")
            sys.stdout.flush()

        elapsed = time.time() - start
        sleep_s = max(0.0, period - elapsed) * random.uniform(0.85, 1.15)
        await asyncio.sleep(sleep_s)


async def grab(workflows: list[dict], n_tabs: int, interval: float, headless: bool) -> None:
    # tab i 绑定 workflows[i % W]，各自只轮询自己工作流；按 i*interval 错峰，全局即轮换
    assign = [workflows[i % len(workflows)] for i in range(n_tabs)]

    cookies = parse_cookies(get_cookie_str())
    print(f"加载 {len(cookies)} 个 cookie；轮询 {len(workflows)} 个工作流："
          + "、".join(f"{w['code']}{w['name']}" for w in workflows))

    if not headless and sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        headless = True
        print("（Linux 无 DISPLAY，自动切 headless 模式）")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        ctx = await browser.new_context(user_agent=USER_AGENT)
        await ctx.add_cookies(cookies)

        pages = [await ctx.new_page() for _ in range(n_tabs)]
        labels = ", ".join(f"tab{i}={assign[i]['code']}" for i in range(n_tabs))
        print(f"navigate {n_tabs} 个 tab：{labels}")
        await asyncio.gather(*(
            pages[i].goto(assign[i]["url"], wait_until="domcontentloaded", timeout=30000)
            for i in range(n_tabs)
        ))

        state = State()
        t0 = time.time()
        per_wf = interval * len(workflows)
        print(f"开始抢任务，{n_tabs} tab 错峰轮询，全局 ~{interval}s/次"
              f"（每个工作流每 ~{per_wf:.1f}s 一次）。Ctrl+C 停止。")
        await asyncio.gather(*(
            worker(i, pages[i], assign[i], interval, n_tabs, state, t0)
            for i in range(n_tabs)
        ))

        if state.reason == "claimed":
            print("\n浏览器停在任务页，可直接在此窗口质检。任务已锁定到账号，也可在常用 Chrome "
                  "打开同一 URL 跑后续流水线。")
            print("Ctrl+C 退出脚本（会关闭这个浏览器窗口）。")
            await asyncio.Event().wait()  # 阻塞，保持浏览器开着


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", action="append",
                    help="临时工作流 URL（含 jobId/flowId），可重复；不填则轮询所有预置工作流")
    ap.add_argument("--only",
                    help="只轮询指定预置工作流，逗号分隔 code（如 A3244,A3245）")
    ap.add_argument("--tabs", type=int, default=0,
                    help="总 tab 数，默认 max(2, 工作流数)；多于工作流数时每工作流多开 tab")
    ap.add_argument("--interval", type=float, default=0.2,
                    help="全局尝试间隔秒，默认 0.2（每隔此时长轮换到下一个工作流）")
    ap.add_argument("--headless", action="store_true", default=False)
    args = ap.parse_args()

    if args.url:
        workflows = [build_workflow(u) for u in args.url]
    else:
        wfs = WORKFLOWS
        if args.only:
            want = {c.strip().upper() for c in args.only.split(",")}
            wfs = [w for w in WORKFLOWS if w["code"].upper() in want]
            if not wfs:
                raise SystemExit(f"--only {args.only} 没匹配到任何预置工作流")
        workflows = [build_workflow(w["url"], w["name"], w["code"]) for w in wfs]

    n_tabs = args.tabs or max(2, len(workflows))
    if n_tabs < len(workflows):
        print(f"[warn] tabs={n_tabs} < 工作流数={len(workflows)}，会漏掉部分工作流，"
              f"自动提升到 {len(workflows)}")
        n_tabs = len(workflows)

    try:
        asyncio.run(grab(workflows, n_tabs, args.interval, args.headless))
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
