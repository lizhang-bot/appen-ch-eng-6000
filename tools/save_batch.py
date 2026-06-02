"""把一批 collector 抓到的 contents + wav URLs 合并到 result/<code>.json。

用法：
    python tools/save_batch.py <code> <contents_json_file> <wavs_txt_file>

contents_json_file: JSON array of {seq, content}
wavs_txt_file: 每行一个 OSS .wav URL (任意顺序)

文件名后缀 _M.wav 对应 sequence = M + 1。
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from qa_pipeline import load_result, save_result, RESULT_DIR, clean_text, detect_lang  # noqa


def url_to_seq(url: str):
    m = re.search(r"_(\d+)\.wav", url)
    return int(m.group(1)) + 1 if m else None


def main():
    code, contents_file, wavs_file = sys.argv[1], sys.argv[2], sys.argv[3]

    with open(contents_file, encoding="utf-8") as f:
        contents = json.load(f)
    with open(wavs_file, encoding="utf-8") as f:
        wavs = [line.strip() for line in f if line.strip()]

    wav_by_seq = {}
    for u in wavs:
        s = url_to_seq(u)
        if s is not None:
            wav_by_seq[s] = u

    doc = load_result(os.path.join(RESULT_DIR, f"{code}.json"))
    doc.setdefault("code", code)
    doc.setdefault("totalQuestions", 203)
    doc.setdefault("questions", [])
    existing = {q["seq"]: q for q in doc["questions"]}

    added = 0
    updated = 0
    for c in contents:
        seq = c["seq"]
        text = clean_text(c.get("content", ""))
        lang = detect_lang(text) if text else "unknown"
        wav = wav_by_seq.get(seq, "")
        if seq in existing:
            # 已有：只补缺漏字段，不覆盖已评价的判定
            q = existing[seq]
            if not q.get("content") and text:
                q["content"] = text
                q["lang"] = lang
                updated += 1
            if not q.get("dataUrl") and wav:
                q["dataUrl"] = wav
                updated += 1
        else:
            doc["questions"].append({
                "seq": seq,
                "lang": lang,
                "content": text,
                "dataUrl": wav,
                "marked": "pending",
            })
            added += 1

    doc["questions"].sort(key=lambda q: q["seq"])
    save_result(os.path.join(RESULT_DIR, f"{code}.json"), doc)

    print(f"added={added} updated={updated} total={len(doc['questions'])}")


if __name__ == "__main__":
    main()
