"""解码 Appen Authorization JWT，输出剩余秒数和过期时间。

JWT header 里 "zip":"DEF"，payload 是 DEFLATE 压缩 + SYNC_FLUSH 结尾，
所以要用 zlib.decompressobj 的流式解压（zlib.decompress 会报 "truncated stream"）。

用法：
    echo "eyJhbGciOi..." | python3 decode_token.py
输出（tab 分隔）：
    <remaining_seconds>\t<expires_human>
"""

import base64
import json
import sys
import zlib
from datetime import datetime, timezone


def decode(jwt: str) -> dict:
    parts = jwt.strip().split(".")
    if len(parts) != 3:
        raise ValueError(f"非 JWT 格式（分段数={len(parts)}）")
    payload_b64 = parts[1]
    raw = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
    d = zlib.decompressobj()
    out = d.decompress(raw) + d.flush()
    return json.loads(out)


def main():
    jwt = sys.stdin.read().strip()
    if not jwt:
        print("stdin 为空", file=sys.stderr)
        sys.exit(1)
    try:
        payload = decode(jwt)
    except Exception as e:
        print(f"解码失败: {e}", file=sys.stderr)
        sys.exit(1)

    exp = payload.get("exp")
    if not exp:
        print("payload 无 exp 字段", file=sys.stderr)
        sys.exit(1)
    if exp > 1e12:
        exp = exp / 1000
    exp_dt = datetime.fromtimestamp(exp, tz=timezone.utc).astimezone()
    remaining = (exp_dt - datetime.now().astimezone()).total_seconds()

    print(f"{int(remaining)}\t{exp_dt.strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
