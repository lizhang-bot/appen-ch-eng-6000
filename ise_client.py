"""讯飞 ISE WebSocket 客户端。

用法：
    root = evaluate("a.pcm", "How are you", category="read_sentence", language="en")
    # root 是 xml.etree.ElementTree.Element，交给 judge.py 处理

音频要求：16kHz / 16bit / 单声道 PCM（aue=raw）。
其它格式（mp3/wav）请先用 ffmpeg 转：
    ffmpeg -i in.mp3 -ar 16000 -ac 1 -f s16le out.pcm
"""

import base64
import hashlib
import hmac
import json
import os
import ssl
import threading
import time
from datetime import datetime
from time import mktime
from urllib.parse import urlencode
from wsgiref.handlers import format_date_time
from xml.etree import ElementTree as ET

import websocket
from dotenv import load_dotenv

load_dotenv()

APPID = os.getenv("XF_APPID")
API_KEY = os.getenv("XF_API_KEY")
API_SECRET = os.getenv("XF_API_SECRET")

HOST = "ise-api.xfyun.cn"
PATH = "/v2/open-ise"


def _build_url() -> str:
    date = format_date_time(mktime(datetime.now().timetuple()))
    signature_origin = f"host: {HOST}\ndate: {date}\nGET {PATH} HTTP/1.1"
    signature = base64.b64encode(
        hmac.new(
            API_SECRET.encode("utf-8"),
            signature_origin.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
    ).decode()
    authorization = base64.b64encode(
        (
            f'api_key="{API_KEY}", algorithm="hmac-sha256", '
            f'headers="host date request-line", signature="{signature}"'
        ).encode("utf-8")
    ).decode()
    qs = urlencode({"authorization": authorization, "date": date, "host": HOST})
    return f"wss://{HOST}{PATH}?{qs}"


def evaluate(
    audio_path: str,
    text: str,
    category: str = "read_sentence",
    language: str = "en",
    timeout: int = 60,
) -> ET.Element:
    """对一段 PCM 音频 + 参考文本调用 ISE，返回解析后的 XML 根节点。"""
    url = _build_url()
    chunks: list[str] = []
    done = threading.Event()
    err: dict[str, str] = {}

    def on_open(ws):
        def run():
            ent = "en_vip" if language == "en" else "cn_vip"
            ws.send(json.dumps({
                "common": {"app_id": APPID},
                "business": {
                    "category": category,
                    "rstcd": "utf8",
                    "group": "adult",
                    "sub": "ise",
                    "ent": ent,
                    "tte": "utf-8",
                    "cmd": "ssb",
                    "auf": "audio/L16;rate=16000",
                    "aue": "raw",
                    "text": "﻿" + text,
                },
                "data": {"status": 0, "data": ""},
            }))

            frame_size = 1280
            interval = 0.04
            with open(audio_path, "rb") as f:
                first = True
                while True:
                    buf = f.read(frame_size)
                    if not buf:
                        ws.send(json.dumps({
                            "business": {"cmd": "auw", "aus": 4, "aue": "raw"},
                            "data": {"status": 2, "data": ""},
                        }))
                        break
                    last = len(buf) < frame_size
                    aus = 4 if last else (1 if first else 2)
                    status = 2 if last else 1
                    ws.send(json.dumps({
                        "business": {"cmd": "auw", "aus": aus, "aue": "raw"},
                        "data": {"status": status,
                                 "data": base64.b64encode(buf).decode()},
                    }))
                    first = False
                    if last:
                        break
                    time.sleep(interval)

        threading.Thread(target=run, daemon=True).start()

    def on_message(_ws, message):
        msg = json.loads(message)
        if msg.get("code") != 0:
            err["msg"] = (
                f"ISE code={msg.get('code')} sid={msg.get('sid')} "
                f"message={msg.get('message')}"
            )
            done.set()
            return
        data = msg.get("data") or {}
        if data.get("data"):
            chunks.append(base64.b64decode(data["data"]).decode("utf-8"))
        if data.get("status") == 2:
            done.set()

    def on_error(_ws, e):
        err["msg"] = str(e)
        done.set()

    def on_close(_ws, *_):
        done.set()

    ws = websocket.WebSocketApp(
        url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )
    threading.Thread(
        target=lambda: ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE}),
        daemon=True,
    ).start()
    if not done.wait(timeout=timeout):
        raise RuntimeError("ISE timeout")
    ws.close()

    if err:
        raise RuntimeError(err["msg"])
    return ET.fromstring("".join(chunks))


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: python ise_client.py <audio.pcm> <reference text>")
        sys.exit(1)
    root = evaluate(sys.argv[1], sys.argv[2])
    print(ET.tostring(root, encoding="unicode"))
