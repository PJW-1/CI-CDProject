import os
import io
import socket
import asyncio
import qrcode
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Air Canvas - Container A (Web & Security Server)")

static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

rooms = {}

CONTAINER_B_URL = os.getenv("CONTAINER_B_URL", "http://container_b:8001/analyze")
DEFAULT_HOST_IP = os.getenv("HOST_IP", "192.168.55.208")

def get_server_ip(request: Request, client_override: str = None):
    if client_override and client_override not in ["localhost", "127.0.0.1", ""]:
        return client_override
    
    host_header = request.headers.get("host", "").split(":")[0]
    if host_header and host_header not in ["localhost", "127.0.0.1"]:
        return host_header
        
    return DEFAULT_HOST_IP

@app.get("/", response_class=HTMLResponse)
async def get_pc_page():
    with open(os.path.join(static_dir, "pc.html"), "r", encoding="utf-8") as f:
        return f.read()

@app.get("/mobile", response_class=HTMLResponse)
async def get_mobile_page():
    with open(os.path.join(static_dir, "mobile.html"), "r", encoding="utf-8") as f:
        return f.read()

@app.get("/api/info")
async def get_server_info(request: Request):
    ip = get_server_ip(request)
    return {"ip": ip, "port": 8443}

@app.get("/api/qr/{session_id}")
async def get_qr_code(session_id: str, request: Request, host: str = None):
    target_ip = get_server_ip(request, host)
    mobile_url = f"https://{target_ip}:8443/mobile?session={session_id}"
    
    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(mobile_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")

@app.websocket("/ws/pc/{session_id}")
async def websocket_pc(websocket: WebSocket, session_id: str):
    await websocket.accept()
    if session_id not in rooms:
        rooms[session_id] = {"pc_ws": None, "mobile_ws": None}
    rooms[session_id]["pc_ws"] = websocket
    print(f"[Session {session_id}] PC 캔버스 연결됨!")

    if rooms[session_id]["mobile_ws"] is not None:
        await websocket.send_json({"type": "STATUS", "mobile_connected": True})

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if session_id in rooms:
            rooms[session_id]["pc_ws"] = None
        print(f"[Session {session_id}] PC 연결 해제됨")

@app.websocket("/ws/mobile/{session_id}")
async def websocket_mobile(websocket: WebSocket, session_id: str):
    await websocket.accept()
    if session_id not in rooms:
        rooms[session_id] = {"pc_ws": None, "mobile_ws": None}
    rooms[session_id]["mobile_ws"] = websocket
    print(f"[Session {session_id}] 📱 모바일 카메라 연결됨!")

    pc_ws = rooms[session_id]["pc_ws"]
    if pc_ws:
        await pc_ws.send_json({"type": "STATUS", "mobile_connected": True})

    client = httpx.AsyncClient(timeout=0.2)

    try:
        while True:
            frame_data = await websocket.receive_text()
            try:
                resp = await client.post(CONTAINER_B_URL, json={
                    "session_id": session_id,
                    "image": frame_data
                })
                
                if resp.status_code == 200:
                    result = resp.json()
                    landmarks = result.get("landmarks", [])
                    
                    pc_ws = rooms.get(session_id, {}).get("pc_ws")
                    if pc_ws and result.get("success"):
                        await pc_ws.send_json({
                            "type": "GESTURE",
                            "action": result.get("action"),
                            "x": result.get("x"),
                            "y": result.get("y"),
                            "delta": result.get("delta", 0),
                            "pan_dx": result.get("pan_dx", 0),
                            "pan_dy": result.get("pan_dy", 0),
                            "landmarks": landmarks
                        })
                    
                    await websocket.send_json({
                        "type": "FEEDBACK",
                        "action": result.get("action", "NONE"),
                        "landmarks": landmarks
                    })
            except Exception:
                pass

    except WebSocketDisconnect:
        if session_id in rooms:
            rooms[session_id]["mobile_ws"] = None
        pc_ws = rooms.get(session_id, {}).get("pc_ws")
        if pc_ws:
            await pc_ws.send_json({"type": "STATUS", "mobile_connected": False})
        print(f"[Session {session_id}] 📱 모바일 연결 해제됨")
    finally:
        await client.aclose()
