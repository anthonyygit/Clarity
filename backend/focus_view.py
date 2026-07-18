import io
import socket
import threading
import time

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from PIL import Image, ImageFilter, ImageStat

app = FastAPI(title="Focus Viewer")

latest_jpeg: bytes = b""
latest_score: float = 0.0
best_score: float = 0.0
frame_count: int = 0
last_frame_time: float = 0.0
fps: float = 0.0


def process_frame(data: bytes) -> bool:
    global latest_jpeg, latest_score, best_score, frame_count, last_frame_time, fps
    start = data.find(b"\xff\xd8")
    end = data.rfind(b"\xff\xd9")
    if start < 0 or end < 0:
        return False
    data = data[start:end + 2]
    try:
        img = Image.open(io.BytesIO(data)).convert("L")
        edges = img.filter(ImageFilter.FIND_EDGES)
        score = ImageStat.Stat(edges).var[0]
    except Exception:
        return False
    now = time.monotonic()
    if last_frame_time:
        inst = 1.0 / max(now - last_frame_time, 1e-6)
        fps = fps * 0.8 + inst * 0.2 if fps else inst
    last_frame_time = now
    latest_jpeg = data
    latest_score = score
    best_score = max(best_score, score)
    frame_count += 1
    return True


def _recvall(conn: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        part = conn.recv(n - len(buf))
        if not part:
            return None
        buf += part
    return buf


def tcp_listener():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", 8002))
    srv.listen(1)
    print("frame stream listening on :8002")
    while True:
        conn, addr = srv.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        print(f"glasses connected from {addr[0]}")
        try:
            while True:
                hdr = _recvall(conn, 4)
                if hdr is None:
                    break
                length = int.from_bytes(hdr, "big")
                if not 0 < length < 500_000:
                    break
                data = _recvall(conn, length)
                if data is None:
                    break
                process_frame(data)
        except Exception as e:
            print(f"stream error: {e}")
        finally:
            conn.close()
            print("glasses disconnected")


threading.Thread(target=tcp_listener, daemon=True).start()


@app.post("/frame")
async def receive_frame(request: Request):
    data = await request.body()
    ok = bool(data) and process_frame(data)
    return {"ok": ok, "score": round(latest_score)}


@app.get("/frame.jpg")
async def frame_jpg():
    return Response(content=latest_jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.get("/score")
async def score():
    pct = round(latest_score * 100 / best_score) if best_score else 0
    return {"score": round(latest_score), "best": round(best_score),
            "pct": pct, "frames": frame_count, "fps": round(fps, 1)}


@app.get("/", response_class=HTMLResponse)
async def page():
    return """<!doctype html>
<html><head><title>Lens Focus</title><style>
body { background:#111; color:#eee; font-family:-apple-system,sans-serif;
       display:flex; flex-direction:column; align-items:center; margin:0; padding:20px; }
#score { font-size:120px; font-weight:800; line-height:1; margin:10px 0; }
#pct { font-size:28px; color:#888; }
img { max-width:90vw; border-radius:8px; margin-top:16px; }
.peak { color:#4ade80; } .close { color:#facc15; } .far { color:#f87171; }
</style></head><body>
<div id="score">--</div>
<div id="pct">waiting for frames from the glasses...</div>
<img id="view" alt="">
<script>
setInterval(async () => {
  try {
    const s = await (await fetch('/score')).json();
    if (!s.frames) return;
    const el = document.getElementById('score');
    el.textContent = s.score;
    el.className = s.pct >= 97 ? 'peak' : (s.pct >= 85 ? 'close' : 'far');
    document.getElementById('pct').textContent =
      s.pct + '% of best (' + s.best + ') — ' +
      (s.pct >= 97 ? 'AT PEAK, stop here' : s.pct >= 85 ? 'close, tiny twists' : 'keep adjusting') +
      ' — ' + s.fps + ' fps';
    document.getElementById('view').src = '/frame.jpg?t=' + Date.now();
  } catch (e) {}
}, 100);
</script></body></html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="192.168.1.2", port=8001)
