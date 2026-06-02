"""音频质检 pipeline：批量任务 → ISE+judge → 增量写 result/[<workflow>/]<code>.json。

多工作流：传 --workflow A3244 则结果存 result/A3244/<code>.json，音频缓存也按工作流隔离
（不同工作流的「编号」可能重复，必须隔离，否则结果文件 / 音频缓存会互相覆盖）。

结果文件 schema:
{
  "code": "6",
  "taskId": "...",
  "flowId": "...",
  "totalQuestions": 203,
  "summary": {
    "totalQuestions": 203,
    "failedCount": N,
    "failReasons": "单词错读, 漏读, 重音不正确"
  },
  "questions": [
    {
      "seq": 5,
      "lang": "en",
      "content": "...",
      "pass": true,
      "reasons": [],          # 类别标签去重列表
      "marked": "pass",       # 页面状态：pass / reject / pending
      "comment": ""           # 拒绝时写入备注
    }
  ]
}

用法：
    python qa_pipeline.py --code 6 --workflow A3244 --from-result   # 评判 result/A3244/6.json
    cat tasks.json | python qa_pipeline.py --code 6                  # 经典模式（flat 路径）
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from ise_client import evaluate
from judge import judge

DEFAULT_CONCURRENCY = 10

AUDIO_CACHE = "audio_cache"
RESULT_DIR = "result"
os.makedirs(AUDIO_CACHE, exist_ok=True)
os.makedirs(RESULT_DIR, exist_ok=True)


def result_path(code: str, workflow: str = "") -> str:
    """结果文件路径。指定 workflow 时按工作流分目录隔离（编号可能跨工作流重复）。"""
    if workflow:
        d = os.path.join(RESULT_DIR, workflow)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{code}.json")
    return os.path.join(RESULT_DIR, f"{code}.json")


def cache_key(code: str, workflow: str = "") -> str:
    """audio_cache 文件名前缀，工作流隔离避免编号重复时音频串味。"""
    return f"{workflow}_{code}" if workflow else str(code)


# AI 给出的原始原因标签 → 备注用的类别（去重时按此分类）
REASON_CATEGORY = {
    "重音异常": "重音不正确",
    "读错": "单词错读",
    "发音错": "单词错读",
    "漏读": "漏读",
    "多读": "多读",
    "环境": "背景噪音",
    "乱读": "乱读",
    "异常声音": "异常杂音",
    "总分低": "总分过低",
    "发音差": "中式口音",
    "中式口音": "中式口音",
    "流利度低": "流利度低",
    "解析": "数据异常",
    "行为": "异常杂音",
}


def detect_lang(text: str) -> str:
    return "zh" if any("一" <= c <= "鿿" for c in text) else "en"


def clean_text(content: str) -> str:
    s = re.sub(r"<[^>]+>", " ", content)
    s = re.sub(r"&lt;[中长短]&gt;", " ", s)
    s = re.sub(r"&[a-zA-Z]+;|&#\d+;", " ", s)
    # ISE 对 ¥ € © ™ 等全角/特殊符号不接受（实测会返回 code=48195）。
    # 只保留 ASCII 可打印 + CJK 汉字 + CJK 中英标点。
    s = re.sub(r"[^\x20-\x7e一-鿿　-〿]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_reasons(raw_reasons: list[tuple[str, str]]) -> list[str]:
    """把 judge 返回的 [(tag, detail), ...] 归一化成去重类别标签列表。"""
    cats = []
    for tag, _detail in raw_reasons:
        cat = REASON_CATEGORY.get(tag, tag)
        if cat not in cats:
            cats.append(cat)
    return cats


def download(url: str, dest: str):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
        f.write(r.read())


def to_pcm(wav: str, pcm: str):
    if os.path.exists(pcm) and os.path.getsize(pcm) > 0:
        return
    subprocess.run(
        ["ffmpeg", "-y", "-i", wav, "-ar", "16000", "-ac", "1", "-f", "s16le", pcm],
        check=True, capture_output=True,
    )


def _is_wav_url(url: str) -> bool:
    """dataUrl 是 .wav 才是音频题；.zip 身份证 / .json 表单等跳过。"""
    return bool(url) and ".wav" in url.split("?")[0]


def _build_detailed_comment(raw_reasons: list[tuple[str, str]]) -> str:
    """把 judge 返回的 [(tag, detail), ...] 拼成详细 comment，
    供 mark-tasks 在页面备注框使用。
    示例：'漏读: tourists Japan; 多读: and was; 读错: photovoltaic'
    """
    parts = []
    for tag, detail in raw_reasons:
        cat = REASON_CATEGORY.get(tag, tag)
        if detail and detail.strip():
            parts.append(f"{cat}: {detail.strip()}")
        else:
            parts.append(cat)
    return "; ".join(parts)


def process_one(item: dict, code: str, workflow: str = "", retry: int = 1) -> dict:
    """统一用 status 字段表示题目状态：
      - pending: 待评判（初始态）
      - pass: 评判通过
      - reject: 评判不通过（reasons + comment 含详细原因）
      - skip: 非录音题（引导/签名/表单），不评判
    """
    import time
    seq = item["seq"]
    data_url = item.get("dataUrl") or ""
    text = clean_text(item["content"])
    lang = detect_lang(text)

    out = {
        "seq": seq,
        "lang": lang,
        "content": text,
        "status": "pending",
        "reasons": [],
        "comment": "",
    }

    if not _is_wav_url(data_url):
        out["status"] = "skip"
        out["comment"] = "非录音题（引导/签名/表单）"
        return out

    last_err = None
    for attempt in range(retry + 1):
        try:
            key = cache_key(code, workflow)
            wav = f"{AUDIO_CACHE}/{key}_q{seq}.wav"
            pcm = f"{AUDIO_CACHE}/{key}_q{seq}.pcm"
            download(data_url, wav)
            to_pcm(wav, pcm)
            root = evaluate(pcm, text, language=lang)
            result = judge(root, language=lang)
            out["reasons"] = normalize_reasons(result["reasons"])
            if result["pass"]:
                out["status"] = "pass"
            else:
                out["status"] = "reject"
                out["comment"] = _build_detailed_comment(result["reasons"])
            return out
        except Exception as e:
            last_err = e
            msg = str(e)
            transient = any(h in msg for h in (
                "time out", "60114", "Connection is already closed"
            ))
            if attempt < retry and transient:
                time.sleep(2)
                continue
            break

    out["status"] = "reject"
    out["reasons"] = ["数据异常"]
    out["comment"] = f"数据异常: {last_err}"
    return out


def build_summary(questions: list[dict], total: int) -> dict:
    failed = [q for q in questions
              if q.get("status") == "reject" and q["seq"] >= 4]
    cats: list[str] = []
    for q in failed:
        for r in q["reasons"]:
            if r not in cats:
                cats.append(r)
    return {
        "totalQuestions": total,
        "failedCount": len(failed),
        "failReasons": ", ".join(cats),
    }


def load_result(path: str) -> dict:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"questions": []}


def save_result(path: str, doc: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)


def _evaluate_pending(doc: dict, code: str, workflow: str, concurrency: int):
    """对 doc.questions 中 status=='pending' 的题跑 ISE / 非音频题直接 skip。"""
    # 1. 非音频题直接标 skip（兼容历史数据：status=skip 但 comment 空也补上）
    skip_count = 0
    for q in doc["questions"]:
        cur = q.get("status", "pending")
        if not _is_wav_url(q.get("dataUrl") or ""):
            q["status"] = "skip"
            q["reasons"] = []
            if not q.get("comment"):
                q["comment"] = "非录音题（引导/签名/表单）"
            if cur == "pending":
                skip_count += 1
    if skip_count:
        print(f"自动 skip {skip_count} 道非音频题")

    pending = [q for q in doc["questions"]
               if q.get("status", "pending") == "pending"]
    if not pending:
        print("没有 pending 题（所有题已评价）")
        return

    print(f"code={code} 并发 {concurrency} 跑 {len(pending)} 道...")
    futs = {}
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        for q in pending:
            futs[ex.submit(process_one, {"seq": q["seq"],
                                          "dataUrl": q["dataUrl"],
                                          "content": q["content"]}, code, workflow)] = q["seq"]
        for fut in as_completed(futs):
            r = fut.result()
            target = next(q for q in doc["questions"] if q["seq"] == r["seq"])
            target["lang"] = r["lang"]
            target["status"] = r["status"]
            target["reasons"] = r["reasons"]
            target["comment"] = r["comment"]
            sym = {"pass": "PASS", "reject": "FAIL", "skip": "SKIP"}.get(r["status"], r["status"])
            detail = r["comment"][:60] if r["status"] == "reject" else ""
            print(f"题 {r['seq']} [{r['lang']}]: {sym}  {detail}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True, help="页面编号，用作结果文件名")
    ap.add_argument("--workflow", default="",
                    help="工作流 code（如 A3244）。指定后结果存 result/<workflow>/<code>.json，"
                         "音频缓存也按工作流隔离（编号跨工作流可能重复）")
    ap.add_argument("--total", type=int, default=203, help="总题数")
    ap.add_argument("--task-id", default="2059511317494829071")
    ap.add_argument("--flow-id", default="2058728701937496080")
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                    help="ISE 并发数")
    ap.add_argument("--from-result", action="store_true",
                    help="直接读 result/[<workflow>/]<code>.json 中所有 pending 的题跑 ISE。"
                         "不读 stdin。")
    args = ap.parse_args()

    rpath = result_path(args.code, args.workflow)
    doc = load_result(rpath)
    doc.setdefault("code", args.code)
    if args.workflow:
        doc.setdefault("workflow", args.workflow)
    doc.setdefault("taskId", args.task_id)
    doc.setdefault("flowId", args.flow_id)
    doc.setdefault("totalQuestions", args.total)
    doc.setdefault("questions", [])

    if args.from_result:
        _evaluate_pending(doc, args.code, args.workflow, args.concurrency)
    else:
        # 经典模式：stdin 喂 (seq, dataUrl, content) 列表
        items = json.load(sys.stdin)
        existing_seqs = {q["seq"] for q in doc["questions"]}
        pending = [it for it in items if it["seq"] not in existing_seqs]
        for it in items:
            if it["seq"] in existing_seqs:
                print(f"题 {it['seq']} 已存在结果，跳过")
        if pending:
            print(f"并发 {args.concurrency} 跑 {len(pending)} 道...")
            results: list[dict] = []
            with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
                futures = {ex.submit(process_one, it, args.code, args.workflow): it["seq"]
                           for it in pending}
                for fut in as_completed(futures):
                    r = fut.result()
                    r["dataUrl"] = next((it["dataUrl"] for it in pending if it["seq"] == r["seq"]), "")
                    results.append(r)
                    sym = {"pass": "PASS", "reject": "FAIL", "skip": "SKIP"}.get(r["status"], r["status"])
                    print(f"题 {r['seq']} [{r['lang']}]: {sym}")
            doc["questions"].extend(results)

    doc["questions"].sort(key=lambda q: q["seq"])
    doc["summary"] = build_summary(doc["questions"], args.total)
    save_result(rpath, doc)

    print(f"\n写入 {rpath}")
    print(f"  totalQuestions={doc['summary']['totalQuestions']}  "
          f"failedCount={doc['summary']['failedCount']}  "
          f"failReasons={doc['summary']['failReasons'] or '-'}")


if __name__ == "__main__":
    main()
