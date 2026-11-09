"""最小 CDP 探针：用 headless Edge 真实点击指挥台，验证「一句话任务」从输入到看板。

仅开发/验收用；不进入运行时依赖。示例：
    .\\.venv\\Scripts\\python.exe scripts\\ui_probe.py --url http://127.0.0.1:8899/ --text "测试任务：写一份说明" --out .e2e/probe.png
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
PORT = 9333


def edge_path() -> str:
    for p in EDGE_CANDIDATES:
        if Path(p).exists():
            return p
    raise SystemExit("找不到 Edge")


async def cdp(ws_url: str, calls: list[tuple[str, dict]]) -> list[dict]:
    out = []
    async with websockets.connect(ws_url, max_size=64 << 20) as ws:
        for method, params in calls:
            await ws.send(json.dumps({"id": len(out) + 1, "method": method, "params": params}))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
                msg = json.loads(raw)
                if msg.get("id") == len(out) + 1:
                    out.append(msg)
                    break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8899/")
    ap.add_argument("--text", default="探针任务：用一句话验证指挥台能创建任务")
    ap.add_argument("--out", default=".e2e/probe.png")
    ap.add_argument("--wait", type=float, default=3.0)
    args = ap.parse_args()

    profile = tempfile.mkdtemp(prefix="multiagent-edge-")
    proc = subprocess.Popen(
        [
            edge_path(), "--headless=new", "--disable-gpu", "--hide-scrollbars",
            f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}",
            "--window-size=1440,900", "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 20
        version = None
        while time.time() < deadline:
            try:
                version = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1))
                break
            except Exception:
                time.sleep(0.3)
        if not version:
            print("Edge CDP 未就绪", file=sys.stderr)
            return 2
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/json/new?{urllib.request.quote(args.url, safe='')}",
            method="PUT",
        )
        target = json.load(urllib.request.urlopen(req, timeout=5))
        ws_url = target["webSocketDebuggerUrl"]

        js = f"""
        (async () => {{
          const text = {json.dumps(args.text, ensure_ascii=False)};
          const el = document.getElementById('mission-text');
          if (!el) return {{ok:false, error:'no mission-text'}};
          el.value = text;
          el.dispatchEvent(new Event('input', {{bubbles:true}}));
          const btn = document.getElementById('mission-submit');
          if (!btn) return {{ok:false, error:'no mission-submit'}};
          btn.click();
          await new Promise(r => setTimeout(r, {int(args.wait*1000)}));
          const toast = document.getElementById('toast');
          const cards = [...document.querySelectorAll('#kanban [data-task], #home-kanban [data-task]')]
            .map(x => x.textContent.replace(/\\s+/g,' ').trim()).slice(0,5);
          return {{
            ok: true,
            toast: toast ? toast.textContent : '',
            hash: location.hash,
            missionValue: el.value,
            cards,
            bodyHasText: document.body.innerText.includes(text.slice(0, 10)),
          }};
        }})()
        """
        result = asyncio.run(cdp(ws_url, [
            ("Page.enable", {}),
            ("Runtime.enable", {}),
            ("Page.navigate", {"url": args.url}),
            ("Runtime.evaluate", {"expression": "new Promise(r=>setTimeout(r,3500))", "awaitPromise": True}),
            ("Runtime.evaluate", {"expression": js, "awaitPromise": True, "returnByValue": True}),
            ("Page.captureScreenshot", {"format": "png"}),
        ]))
        eval_msg = result[4].get("result", {}).get("result", {})
        print(json.dumps(eval_msg.get("value", eval_msg), ensure_ascii=False, indent=2))
        shot = result[5].get("result", {}).get("data")
        if shot:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(base64.b64decode(shot))
            print(f"screenshot: {out.resolve()}")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
