# -*- coding: utf-8 -*-
"""
YuriLive 一起听/看 服务端
- WS 信令: host 广播播放状态, client 被动跟随
- /api/videos  视频清单
- /api/upload  host 上传视频 (X-Host-Key 鉴权)
- /media/*     Range 视频流 (渐进播放)
- 生产模式自动托管 dist/ (vite build 产物)

.env:
  YL_HOST_KEY=你的host密码
  YL_MEDIA_ROOT=/home/minds/yuristream/downloads   # 视频根目录, 默认 ./media
  YL_PORT=8003
"""

import asyncio
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Header, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).resolve().parent

# ---- .env ----
_ENV = BASE / ".env"
if _ENV.exists():
    for line in _ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip("'").strip('"'))

HOST_KEY = os.environ.get("YL_HOST_KEY", "change-me")
MEDIA_ROOT = Path(os.environ.get("YL_MEDIA_ROOT", str(BASE / "media"))).resolve()
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
VIDEO_EXT = {".mp4", ".m4v", ".webm", ".mov"}

YTDLP = os.environ.get("YL_YTDLP", "yt-dlp")
# Cookie 三级回落: YL_COOKIE(直接粘贴cookie字符串) > YL_COOKIES_FILE(cookies.txt) > YL_COOKIES_FROM(读本机浏览器)
COOKIE_STR = os.environ.get("YL_COOKIE", "")
COOKIES_FILE = os.environ.get("YL_COOKIES_FILE", "")
COOKIES_FROM = os.environ.get("YL_COOKIES_FROM", "")
PORT = int(os.environ.get("YL_PORT", "8003"))

app = FastAPI(title="YuriLive", version="0.1.0")

# ---------------- 同步状态 ----------------
state = {"v": "", "playing": False, "pos": 0.0, "srv_ts": 0.0}
clients: set = set()


async def broadcast(msg: dict):
    dead = []
    for c in list(clients):
        try:
            await c.send_json(msg)
        except Exception:
            dead.append(c)
    for c in dead:
        clients.discard(c)


async def broadcast_count():
    await broadcast({"t": "count", "n": len(clients)})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, key: str = ""):
    role = "host" if key and key == HOST_KEY else "guest"
    await ws.accept()
    clients.add(ws)
    name = "主持人" if role == "host" else "观众-" + uuid.uuid4().hex[:4]
    await ws.send_json({"t": "hello", "role": role, "name": name, "state": dict(state)})
    await broadcast_count()
    try:
        while True:
            msg = await ws.receive_json()
            t = msg.get("t")
            if t == "ping":
                await ws.send_json(
                    {"t": "pong", "cts": msg.get("cts", 0), "sts": time.time()}
                )
            elif t == "state" and role == "host":
                state["v"] = msg.get("v", state["v"])
                state["playing"] = bool(msg.get("playing"))
                state["pos"] = float(msg.get("pos", 0))
                state["srv_ts"] = time.time()
                await broadcast({"t": "state", **state})
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)
        await broadcast_count()


# ---------------- 视频清单与上传 ----------------


@app.get("/api/videos")
def list_videos():
    files = []
    if MEDIA_ROOT.exists():
        for p in sorted(
            MEDIA_ROOT.rglob("*"), key=lambda x: x.stat().st_mtime, reverse=True
        ):
            if p.is_file() and p.suffix.lower() in VIDEO_EXT:
                st = p.stat()
                files.append(
                    {
                        "path": str(p.relative_to(MEDIA_ROOT)),
                        "name": p.name,
                        "size": st.st_size,
                    }
                )
    return {
        "files": files,
        "state": {"v": state["v"], "playing": state["playing"], "pos": state["pos"]},
    }


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), x_host_key: str = Header(default="")):
    if x_host_key != HOST_KEY:
        return JSONResponse({"error": "host key 无效"}, status_code=403)
    safe = re.sub(r'[\\/:*?"<>|]', "_", file.filename or "video.mp4")
    dest = MEDIA_ROOT / safe
    if dest.exists():
        dest = MEDIA_ROOT / f"{int(time.time())}_{safe}"
    n = 0
    with open(dest, "wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
            n += len(chunk)
    print(f"[yurilive] host 上传: {dest.name} ({n / 1048576:.1f} MB)", flush=True)
    return {"ok": True, "path": str(dest.relative_to(MEDIA_ROOT)), "name": dest.name}


# ---------------- B站视频爬取(yt-dlp, 后台任务+轮询) ----------------


class FetchIn(BaseModel):
    url: str


jobs: dict = {}  # job_id -> {status, url, pct, name, path, msg}


async def _run_fetch(job_id: str, url: str):
    j = jobs[job_id]
    before = (
        {p.name for p in MEDIA_ROOT.iterdir() if p.is_file()}
        if MEDIA_ROOT.exists()
        else set()
    )
    cmd = [
        YTDLP,
        "--no-playlist",
        "--windows-filenames",
        "--newline",
        "-f",
        "bv*+ba/b",
        "--merge-output-format",
        "mp4",
    ]
    if COOKIE_STR:
        cmd += ["--add-header", "Cookie:" + COOKIE_STR]
    elif COOKIES_FILE:
        cmd += ["--cookies", COOKIES_FILE]
    elif COOKIES_FROM:
        cmd += ["--cookies-from-browser", COOKIES_FROM]
    cmd += ["-o", str(MEDIA_ROOT / "%(title)s.%(ext)s"), url]

    print(f"[yurilive] 开始下载[{job_id}]: {url}", flush=True)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
    except FileNotFoundError:
        j.update(status="failed", msg="未安装 yt-dlp (pip install yt-dlp)")
        return
    tail_lines = []
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        text = line.decode("utf-8", "ignore")
        m = re.search(r"\[download\]\s+(\d+\.\d+)%", text)
        if m:
            j["pct"] = float(m.group(1))
        tail_lines.append(text)
        if len(tail_lines) > 40:
            tail_lines.pop(0)
    await proc.wait()
    new_files = [
        p
        for p in MEDIA_ROOT.iterdir()
        if p.is_file() and p.name not in before and p.suffix.lower() in VIDEO_EXT
    ]
    if proc.returncode == 0 and new_files:
        p = max(new_files, key=lambda x: x.stat().st_mtime)
        print(f"[yurilive] 下载完成[{job_id}]: {p.name}", flush=True)
        j.update(status="done", pct=100.0, name=p.name, path=p.name)
    else:
        tail = "".join(tail_lines).strip()[-300:] or f"退出码 {proc.returncode}"
        print(f"[yurilive] 下载失败[{job_id}]: {tail}", flush=True)
        j.update(status="failed", msg=tail)


@app.post("/api/fetch")
async def fetch_video(item: FetchIn, x_host_key: str = Header(default="")):
    """host 粘贴B站链接 -> 后台下载 -> 前端轮询 /api/fetch/{job_id} 看进度"""
    if x_host_key != HOST_KEY:
        return JSONResponse({"error": "host key 无效"}, status_code=403)
    url = item.url.strip()
    if not any(h in url for h in ("bilibili.com", "b23.tv")):
        return JSONResponse(
            {"error": "仅支持 bilibili.com / b23.tv 链接"}, status_code=400
        )
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {
        "status": "running",
        "url": url,
        "pct": 0.0,
        "name": "",
        "path": "",
        "msg": "",
    }
    asyncio.create_task(_run_fetch(job_id, url))
    return {"ok": True, "job": job_id}


@app.get("/api/fetch/{job_id}")
def fetch_status(job_id: str):
    j = jobs.get(job_id)
    if not j:
        return JSONResponse({"error": "任务不存在"}, status_code=404)
    return j


# ---------------- Range 视频流 ----------------


@app.get("/media/{full_path:path}")
def media(full_path: str, range: str = Header(default="")):
    root = MEDIA_ROOT
    target = (root / full_path).resolve()
    if not str(target).startswith(str(root)) or not target.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    size = target.stat().st_size
    start, end = 0, size - 1
    if range.startswith("bytes="):
        try:
            rng = range[6:].split(",")[0].strip()
            if rng.startswith("-"):
                start = max(size - int(rng[1:]), 0)
            else:
                a, _, b = rng.partition("-")
                start = int(a)
                end = int(b) if b else size - 1
        except ValueError:
            start, end = 0, size - 1
    end = min(end, size - 1)
    if start > end:
        return JSONResponse({"error": "bad range"}, status_code=416)
    length = end - start + 1
    ctype = (
        "video/mp4"
        if target.suffix.lower() in (".mp4", ".m4v", ".mov")
        else "video/webm"
    )
    headers = {
        "Content-Type": ctype,
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Range": f"bytes {start}-{end}/{size}",
    }
    status = 206 if range else 200

    def iterfile():
        with open(target, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1 << 20, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    from fastapi.responses import StreamingResponse

    return StreamingResponse(iterfile(), status_code=status, headers=headers)


# ---------------- 生产: 托管前端 dist ----------------
if (BASE / "dist").exists():
    app.mount("/", StaticFiles(directory=str(BASE / "dist"), html=True), name="static")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
