"""用 Playwright 收集题目数据：注入 XHR/fetch hook 拦截 detail API response，
循环 click 导航 1..N 触发 page 调用，每条 response base64 编码后通过 console.log
传到 Python 端，解析后写入 result/<code>.json。

完全脱离 Authorization JWT 管理——浏览器自动带 HttpOnly cookie。

依赖：
    pip install playwright && playwright install chromium

用法：
    # 先重新从 Chrome DevTools copy as cURL → 更新 watch_jobs.sh 里 COOKIE 字段
    source venv/bin/activate
    python tools/collect_via_playwright.py 6 \\
        --url "https://collect-web.appen.com.cn/collect/qa?flowId=...&taskId=..."

参数：
    --url       质检页面完整 URL
    --total     总题数（默认 203）
    --interval  click 间隔毫秒（默认 200）
    --headless  无头模式（默认有头方便观察）
"""

import argparse
import asyncio
import base64
import json
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from qa_pipeline import (  # noqa: E402
    clean_text,
    detect_lang,
    load_result,
    result_path,
    save_result,
)

from playwright.async_api import async_playwright  # noqa: E402


COOKIE_DOMAIN = "collect-web.appen.com.cn"


HOOK_SCRIPT = """
(() => {
  if (window.__hookInstalled__) return;
  window.__hookInstalled__ = true;
  const emit = (d) => {
    try {
      const p = {
        seq: d.sequence,
        content: d.content,
        dataUrl: d.dataUrl,
        subTaskId: String(d.subTaskId),
      };
      const b64 = btoa(unescape(encodeURIComponent(JSON.stringify(p))));
      console.log('@DATA@' + b64);
    } catch (e) {
      console.log('@DATA@ERR@' + e.message);
    }
  };
  // Hook XMLHttpRequest
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url) {
    this.__url = url;
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function() {
    this.addEventListener('load', () => {
      if (this.__url && this.__url.includes('sub-task/detail')) {
        try {
          const j = JSON.parse(this.responseText);
          if (j.data) emit(j.data);
        } catch (e) {}
      }
    });
    return origSend.apply(this, arguments);
  };
  // Hook fetch（兼容）
  const origFetch = window.fetch;
  window.fetch = async function(input, init) {
    const url = (typeof input === 'string') ? input : input?.url;
    const resp = await origFetch.apply(this, arguments);
    if (url && url.includes('sub-task/detail')) {
      try {
        const j = await resp.clone().json();
        if (j.data) emit(j.data);
      } catch (e) {}
    }
    return resp;
  };
})();
"""


def get_cookie_str() -> str:
    sh = PROJECT_ROOT / "watch_jobs.sh"
    if not sh.exists():
        raise RuntimeError("watch_jobs.sh 不存在")
    content = sh.read_text(encoding="utf-8")
    m = re.search(r"COOKIE='([^']+)'", content)
    if not m:
        raise RuntimeError("watch_jobs.sh 里没找到 COOKIE=...")
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
            "httpOnly": k.strip() == "Authorization",  # Authorization 是 HttpOnly
        })
    return out


async def collect(code: str, url: str, total: int, interval_ms: int, headless: bool,
                  workflow: str = ""):
    cookies = parse_cookies(get_cookie_str())
    print(f"加载 {len(cookies)} 个 cookie: {[c['name'] for c in cookies]}")

    data_lines: list[str] = []
    error_lines: list[str] = []

    if not headless and sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        headless = True
        print("（Linux 无 DISPLAY，自动切 headless 模式）")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.0.0 Safari/537.36"
            ),
        )
        await ctx.add_cookies(cookies)
        await ctx.add_init_script(HOOK_SCRIPT)

        page = await ctx.new_page()

        def on_console(msg):
            t = msg.text
            if t.startswith("@DATA@"):
                if "@DATA@ERR@" in t or "@DATA@EXC@" in t:
                    error_lines.append(t)
                else:
                    data_lines.append(t)

        page.on("console", on_console)

        print(f"navigate → {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # 等导航区渲染（203 个 cell）
        await page.wait_for_function(
            "[...document.querySelectorAll('div.cursor-pointer span')]"
            ".filter(s => /^\\d+$/.test(s.textContent.trim())).length >= " + str(total // 2),
            timeout=15000,
        )
        print(f"导航区已渲染，开始循环 click 1..{total}（间隔 {interval_ms}ms）...")

        # 循环 click：先 1..total，最后再 click 一次 1（page 初始可能在 1，第一次 click 不触发 fetch）
        result = await page.evaluate(
            """async ({total, interval}) => {
                const clickN = (n) => {
                    const span = [...document.querySelectorAll('div.cursor-pointer span')]
                        .find(x => x.textContent.trim() === String(n));
                    if (!span) return false;
                    span.parentElement.scrollIntoView({block: 'nearest'});
                    span.parentElement.click();
                    return true;
                };
                let clicked = 0;
                for (let n = 1; n <= total; n++) {
                    if (clickN(n)) clicked++;
                    await new Promise(r => setTimeout(r, interval));
                }
                // 末尾补 click 1 + 等 detail 完成
                clickN(1);
                await new Promise(r => setTimeout(r, 1500));
                return {clicked};
            }""",
            {"total": total, "interval": interval_ms},
        )
        print(f"完成 click 循环：{result['clicked']}/{total} 次成功")

        # 给最后几个 detail response 一点时间
        await page.wait_for_timeout(2000)

        await browser.close()

    print(f"\n收到 {len(data_lines)} 个 @DATA@ 消息")
    if error_lines:
        print(f"  错误 {len(error_lines)}: {error_lines[:3]}")

    # 解码 + 写 result.json
    entries: dict[int, dict] = {}
    for line in data_lines:
        m = re.search(r"@DATA@(?:OK@)?([A-Za-z0-9+/=]{20,})", line)
        if not m:
            continue
        try:
            data = json.loads(base64.b64decode(m.group(1)).decode("utf-8"))
            entries[int(data["seq"])] = data
        except Exception as e:
            print(f"  decode err: {e}")

    print(f"解码 {len(entries)} 题")

    rpath = result_path(code, workflow)
    doc = load_result(rpath)
    doc.setdefault("code", code)
    if workflow:
        doc.setdefault("workflow", workflow)
    doc.setdefault("totalQuestions", total)
    doc.setdefault("questions", [])
    by_seq = {q["seq"]: q for q in doc["questions"]}

    new_count = updated_count = 0
    for seq, data in entries.items():
        text = clean_text(data.get("content") or "")
        lang = detect_lang(text) if text else "unknown"
        entry = {
            "seq": seq,
            "lang": lang,
            "content": text,
            "dataUrl": data.get("dataUrl") or "",
            "subTaskId": str(data.get("subTaskId") or ""),
        }
        if seq in by_seq:
            for k, v in entry.items():
                by_seq[seq][k] = v
            by_seq[seq].setdefault("status", "pending")
            updated_count += 1
        else:
            entry["status"] = "pending"
            doc["questions"].append(entry)
            by_seq[seq] = entry
            new_count += 1

    doc["questions"].sort(key=lambda q: q["seq"])
    save_result(rpath, doc)

    seqs = {q["seq"] for q in doc["questions"]}
    missing = sorted(set(range(1, total + 1)) - seqs)
    print(f"\n写入 {rpath}: 新增 {new_count}, 更新 {updated_count}")
    print(f"总 {len(doc['questions'])}/{total}")
    if missing:
        preview = missing[:30]
        print(f"缺漏 {len(missing)}: {preview}{'...' if len(missing) > 30 else ''}")
    else:
        print("✓ 全覆盖")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("code", help="页面编号（如 6）；结果存 result/[<workflow>/]<code>.json")
    ap.add_argument("--workflow", default="",
                    help="工作流 code（如 A3244）。指定后结果存 result/<workflow>/<code>.json")
    ap.add_argument("--url", required=True, help="质检页面完整 URL")
    ap.add_argument("--total", type=int, default=203)
    ap.add_argument("--interval", type=int, default=200, help="click 间隔毫秒，默认 200")
    ap.add_argument("--headless", action="store_true", default=False,
                    help="无头模式（默认有头方便看进度）")
    args = ap.parse_args()

    asyncio.run(collect(
        code=args.code, url=args.url, total=args.total,
        interval_ms=args.interval, headless=args.headless, workflow=args.workflow,
    ))


if __name__ == "__main__":
    main()
