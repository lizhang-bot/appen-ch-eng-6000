"""根据 ISE 返回的 XML 综合判定三条质检规则：
1. 背景音 / 异常行为
2. 发音（中式口音 / 塑料英语 / 读错 / 重音错误）
3. 多读 / 漏读

中英文 ISE 输出格式完全不同：
- 英文：根 <read_chapter>，分数 0-5 制
- 中文：根 <read_sentence>，分数 0-100 制，有 tone_score
"""

import os
from xml.etree import ElementTree as ET

EXCEPT_INFO_MAP = {
    28680: "背景噪声过大（信噪比低）",
    28673: "音量太小或无人声",
    28690: "音量过大爆音",
}

# 重音判定忽略名单（地名拼音 / 非英语词 / ISE 拼读不准的）
_IGNORE_STRESS_PATH = os.path.join(os.path.dirname(__file__), "data", "ignore_stress_words.txt")
IGNORE_STRESS_WORDS: set[str] = set()
if os.path.exists(_IGNORE_STRESS_PATH):
    with open(_IGNORE_STRESS_PATH, encoding="utf-8") as f:
        for line in f:
            w = line.strip().lower()
            if w and not w.startswith("#"):
                IGNORE_STRESS_WORDS.add(w)


def _should_ignore_stress(content: str) -> bool:
    """重音判定应否忽略：纯数字 / 含数字 / 白名单里的词。"""
    w = content.strip().lower()
    if not w:
        return True
    if any(c.isdigit() for c in w):
        return True
    if w in IGNORE_STRESS_WORDS:
        return True
    return False

# 英文阈值（0-5 制）
EN_TOTAL_MIN = 3.5
EN_ACCURACY_MIN = 3.5
EN_REJECT_GARBLE_ACCURACY_MAX = 2.0

# 中文阈值（0-100 制）
ZH_TOTAL_MIN = 70.0
ZH_FLUENCY_MIN = 60.0
ZH_PHONE_MIN = 70.0
ZH_REJECT_GARBLE_TOTAL_MAX = 40.0


def judge(root: ET.Element, language: str = "en") -> dict:
    if language == "zh":
        return _judge_zh(root)
    return _judge_en(root)


def _judge_en(root: ET.Element) -> dict:
    reasons: list[tuple[str, str]] = []

    chapter = root.find(".//read_chapter")
    if chapter is None:
        return _err("未找到 read_chapter 节点")

    total = _f(chapter.get("total_score"))
    accuracy = _f(chapter.get("accuracy_score"))
    fluency = _f(chapter.get("fluency_score"))
    integrity = _f(chapter.get("integrity_score"))

    except_info = int(chapter.get("except_info", "0") or "0")
    is_rejected = chapter.get("is_rejected", "false") == "true"
    if except_info in EXCEPT_INFO_MAP:
        reasons.append(("环境", EXCEPT_INFO_MAP[except_info]))
    elif is_rejected:
        if accuracy and accuracy < EN_REJECT_GARBLE_ACCURACY_MAX:
            reasons.append(("乱读", f"内容与参考不符 accuracy={accuracy:.2f}"))
        else:
            reasons.append(("异常声音", f"咳嗽/敲桌/唱歌等 accuracy={accuracy:.2f}"))

    if total and total < EN_TOTAL_MIN:
        reasons.append(("总分低", f"total={total:.2f}"))
    if accuracy and accuracy < EN_ACCURACY_MIN:
        reasons.append(("中式口音", f"accuracy={accuracy:.2f}"))

    # 词级多读/漏读/读错
    # 注：曾经在这里做过"重音异常"判定（serr_msg!=0），
    # 但实测在真实数据上误判率太高（officially / technologies / certification
    # 这类多音节英文词常被报但人耳听不出问题），整体放弃这条规则。
    # 如要回滚：查 git log judge.py + data/ignore_stress_words.txt
    add_w, miss_w, replace_w = [], [], []
    for word in root.iter("word"):
        content = word.get("content", "")
        dp = word.get("dp_message", "0")
        if dp == "16":
            miss_w.append(content)
        elif dp == "32":
            add_w.append(content)
        elif dp == "64":
            replace_w.append(content)
    if replace_w:
        reasons.append(("读错", " ".join(replace_w)))
    if miss_w:
        reasons.append(("漏读", " ".join(miss_w)))
    if add_w:
        reasons.append(("多读", " ".join(add_w)))

    return {
        "pass": len(reasons) == 0,
        "reasons": reasons,
        "total_score": total,
        "accuracy_score": accuracy,
        "fluency_score": fluency,
        "integrity_score": integrity,
    }


def _judge_zh(root: ET.Element) -> dict:
    reasons: list[tuple[str, str]] = []

    # 中文：根是 read_sentence，内部还有一层 read_sentence 承载分数
    inner = root.find(".//rec_paper/read_sentence")
    if inner is None:
        return _err("未找到中文 read_sentence 节点")

    total = _f(inner.get("total_score"))
    fluency = _f(inner.get("fluency_score"))
    phone = _f(inner.get("phone_score"))
    integrity = _f(inner.get("integrity_score"))
    tone = _f(inner.get("tone_score"))

    except_info = int(inner.get("except_info", "0") or "0")
    is_rejected = inner.get("is_rejected", "false") == "true"
    if except_info in EXCEPT_INFO_MAP:
        reasons.append(("环境", EXCEPT_INFO_MAP[except_info]))
    elif is_rejected:
        if total and total < ZH_REJECT_GARBLE_TOTAL_MAX:
            reasons.append(("乱读", f"内容与参考不符 total={total:.1f}"))
        else:
            reasons.append(("异常声音", f"咳嗽/敲桌/唱歌等 total={total:.1f}"))

    if total and total < ZH_TOTAL_MIN:
        reasons.append(("总分低", f"total={total:.1f}"))
    if fluency and fluency < ZH_FLUENCY_MIN:
        reasons.append(("流利度低", f"fluency={fluency:.1f}"))
    if phone and phone < ZH_PHONE_MIN:
        reasons.append(("发音错", f"phone={phone:.1f}"))

    # 字级多读/漏读/读错
    add_w, miss_w, replace_w = [], [], []
    for word in root.iter("word"):
        content = word.get("content", "")
        dp = word.get("dp_message", "0")
        if dp == "16":
            miss_w.append(content)
        elif dp == "32":
            add_w.append(content)
        elif dp == "64":
            replace_w.append(content)
    if replace_w:
        reasons.append(("读错", "".join(replace_w)))
    if miss_w:
        reasons.append(("漏读", "".join(miss_w)))
    if add_w:
        reasons.append(("多读", "".join(add_w)))

    return {
        "pass": len(reasons) == 0,
        "reasons": reasons,
        "total_score": total,
        "accuracy_score": 0.0,  # 中文无此概念
        "fluency_score": fluency,
        "integrity_score": integrity,
        "tone_score": tone,
        "phone_score": phone,
    }


def _err(msg: str) -> dict:
    return {
        "pass": False,
        "reasons": [("解析", msg)],
        "total_score": 0.0,
        "accuracy_score": 0.0,
        "fluency_score": 0.0,
        "integrity_score": 0.0,
    }


def _f(s) -> float:
    if not s:
        return 0.0
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


if __name__ == "__main__":
    import sys
    from ise_client import evaluate
    if len(sys.argv) < 3:
        print("usage: python judge.py <audio.pcm> <reference text> [zh|en]")
        sys.exit(1)
    lang = sys.argv[3] if len(sys.argv) >= 4 else "en"
    root = evaluate(sys.argv[1], sys.argv[2], language=lang)
    result = judge(root, language=lang)
    print("PASS" if result["pass"] else "FAIL")
    print(f"  total={result['total_score']:.2f}  "
          f"accuracy={result['accuracy_score']:.2f}  "
          f"fluency={result['fluency_score']:.2f}")
    for tag, detail in result["reasons"]:
        print(f"  [{tag}] {detail}")
