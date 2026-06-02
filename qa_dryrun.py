"""音频质检 dry-run。

输入（stdin）：JSON 数组，每个元素 {seq, dataUrl, content}。
对每条：
  1. 下载 OSS 音频 → audio_cache/q{seq}.wav
  2. ffmpeg 转 16k 单声道 PCM
  3. 检测语言（CJK→中文，否则英文）
  4. 清洗参考文本（去 HTML、去 <中>/<长>/<短> 标签）
  5. 调 ISE
  6. 跑 judge
  7. 打印判定 + 原因（不点页面）

用法：
    cat tasks.json | python qa_dryrun.py
"""

import json
import os
import re
import subprocess
import sys
import urllib.request

from ise_client import evaluate
from judge import judge

AUDIO_CACHE = "audio_cache"
os.makedirs(AUDIO_CACHE, exist_ok=True)


def detect_lang(text: str) -> str:
    """含中日韩统一表意文字 → 中文，否则英文。"""
    return "zh" if any("一" <= c <= "鿿" for c in text) else "en"


def clean_text(content: str) -> str:
    """去 HTML 标签、去 <中>/<长>/<短>，压缩空白。
    注意：不能用宽松正则去裸字"中"，否则会误删句子里的"中国"等。
    """
    s = re.sub(r"<[^>]+>", " ", content)        # 含 <中>/<长>/<短> 在内的 HTML tag
    s = re.sub(r"&lt;[中长短]&gt;", " ", s)      # HTML 转义后的 <中>
    s = re.sub(r"&[a-zA-Z]+;|&#\d+;", " ", s)    # 其它 HTML 实体
    s = re.sub(r"\s+", " ", s).strip()
    return s


def download(url: str, dest: str):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
        f.write(r.read())


def to_pcm(wav: str, pcm: str):
    subprocess.run(
        ["ffmpeg", "-y", "-i", wav, "-ar", "16000", "-ac", "1", "-f", "s16le", pcm],
        check=True, capture_output=True,
    )


def process_one(item: dict) -> dict:
    seq = item["seq"]
    data_url = item["dataUrl"]
    text = clean_text(item["content"])
    lang = detect_lang(text)

    print(f"\n=== 题 {seq} [{lang}] ===")
    print(f"  文本: {text[:90]}")

    wav = f"{AUDIO_CACHE}/q{seq}.wav"
    pcm = f"{AUDIO_CACHE}/q{seq}.pcm"
    download(data_url, wav)
    to_pcm(wav, pcm)

    try:
        root = evaluate(pcm, text, language=lang)
    except Exception as e:
        print(f"  ❌ ISE 失败: {e}")
        return {"seq": seq, "error": str(e)}

    result = judge(root, language=lang)

    status = "PASS" if result["pass"] else "FAIL"
    print(f"  判定: {status}  total={result['total_score']:.2f} "
          f"accuracy={result['accuracy_score']:.2f} "
          f"fluency={result['fluency_score']:.2f}")
    for tag, detail in result["reasons"]:
        # 截短词列表
        d = detail if len(detail) <= 80 else detail[:77] + "…"
        print(f"    [{tag}] {d}")
    return {"seq": seq, "lang": lang, "pass": result["pass"],
            "reasons": result["reasons"], "scores": {
                "total": result["total_score"],
                "accuracy": result["accuracy_score"],
                "fluency": result["fluency_score"],
            }}


def main():
    items = json.load(sys.stdin)
    results = []
    for it in items:
        try:
            results.append(process_one(it))
        except Exception as e:
            print(f"  ❌ 处理失败: {e}")
            results.append({"seq": it.get("seq"), "error": str(e)})

    # 汇总
    print("\n" + "=" * 50)
    print("汇总:")
    for r in results:
        if "error" in r:
            print(f"  题 {r['seq']}: ERROR {r['error']}")
        else:
            s = "PASS" if r["pass"] else "FAIL"
            tags = " / ".join(t for t, _ in r["reasons"]) or "-"
            print(f"  题 {r['seq']} [{r['lang']}]: {s}  ({tags})")


if __name__ == "__main__":
    main()
