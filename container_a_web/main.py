import io
import logging
import os

import httpx
import qrcode
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from common.config import config_meta, load_config
from common.http_client import ResilientClient
from common.logging_setup import Timer, setup_logging
from common.schemas import GestureResult, HealthState

# ---- 설정 (Pillar 1) + 공통 구조화 로깅 (Pillar 4) ----
# 기존에는 print() 4곳이 전부였고, IP/포트/타임아웃이 코드에 박혀 있었다.
cfg = load_config()
log = setup_logging("A", settings=cfg.get("logging"))

app = FastAPI(title="Air Canvas - Container A (Web & Security Server)")

static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

rooms = {}

# 고도화 이전:
#   CONTAINER_B_URL = os.getenv("CONTAINER_B_URL", "http://container_b:8001/analyze")
#   DEFAULT_HOST_IP = os.getenv("HOST_IP", "192.168.55.208")   ← 하드코딩. 다른 망으로 가면 QR이 죽었다.
# 이제 host_ip: auto 면 런타임에 LAN IP를 탐지한다.
CONTAINER_B_URL = cfg.get("network.vision_url")
DEFAULT_HOST_IP = cfg.get("network.host_ip")
WEB_PORT = cfg.get("network.web_port")

# 헬스체크 전용 타임아웃. 프레임 경로보다 관대해야 한다.
READINESS_TIMEOUT_S = 10.0

HTTP_TIMEOUT = httpx.Timeout(
    connect=cfg.get("http.connect_timeout_s"),
    read=cfg.get("http.read_timeout_s"),
    write=cfg.get("http.read_timeout_s"),
    pool=cfg.get("http.connect_timeout_s"),
)

# 기동 시 유효 설정을 남긴다. 장애 분석의 출발점은 "그때 무슨 값으로 떠 있었는가"다.
log.info(
    "service_starting",
    detail={
        "service": "web_gateway",
        "config": config_meta(),
        "container_b_url": CONTAINER_B_URL,
        "host_ip": DEFAULT_HOST_IP,
        "web_port": WEB_PORT,
        "http_timeout": {
            "connect_s": cfg.get("http.connect_timeout_s"),
            "read_s": cfg.get("http.read_timeout_s"),
        },
        "static_dir": static_dir,
    },
)

def _detach_session(session_id: str, slot: str) -> None:
    """
    WebSocket 하나를 방에서 떼어내고, 양쪽이 모두 비면 방 자체를 삭제한다.

    고도화 이전에는 값만 None 으로 바꾸고 키는 영구히 남겼다.
        rooms[session_id]["pc_ws"] = None      # 키는 지워지지 않음
    세션 ID는 pc.html 에서 Math.random() 으로 매번 새로 생성되므로,
    페이지를 새로고침할 때마다 죽은 엔트리가 하나씩 영구 누적되는 구조였다.
    """
    room = rooms.get(session_id)
    if room is None:
        return
    room[slot] = None
    if room["pc_ws"] is None and room["mobile_ws"] is None:
        rooms.pop(session_id, None)
        log.info(
            "session_room_released",
            session_id=session_id,
            detail={"active_rooms": len(rooms)},
        )


def _is_container_internal_ip(ip: str) -> bool:
    """
    Docker 브리지 대역(172.16~172.31)인지 판별한다.
    컨테이너 안에서 LAN IP를 자동 탐지하면 이 대역이 잡히는데,
    이 주소를 QR에 넣으면 폰이 접속할 수 없다.
    """
    parts = ip.split(".")
    if len(parts) != 4 or parts[0] != "172":
        return False
    try:
        return 16 <= int(parts[1]) <= 31
    except ValueError:
        return False


def get_server_ip(request: Request, client_override: str = None):
    """
    QR/접속 안내에 쓸 서버 주소를 결정한다.
    우선순위: 클라이언트 지정 → Host 헤더 → 설정값(HOST_IP 또는 자동탐지)
    """
    if client_override and client_override not in ["localhost", "127.0.0.1", ""]:
        return client_override

    host_header = request.headers.get("host", "").split(":")[0]
    if host_header and host_header not in ["localhost", "127.0.0.1"]:
        return host_header

    # localhost로 접속한 경우 Host 헤더로는 LAN IP를 알 수 없다.
    # 설정값이 컨테이너 내부 주소라면 QR이 죽으므로 명시적으로 경고를 남긴다.
    if _is_container_internal_ip(DEFAULT_HOST_IP):
        log.warning(
            "host_ip_unusable",
            detail={
                "resolved_ip": DEFAULT_HOST_IP,
                "reason": "컨테이너 내부 주소라 폰에서 접속 불가",
                "fix": "scripts/compose_up.py 로 기동하거나 HOST_IP 환경변수를 지정하세요",
            },
        )
    return DEFAULT_HOST_IP

@app.get("/", response_class=HTMLResponse)
async def get_pc_page():
    with open(os.path.join(static_dir, "pc.html"), encoding="utf-8") as f:
        return f.read()

@app.get("/mobile", response_class=HTMLResponse)
async def get_mobile_page():
    with open(os.path.join(static_dir, "mobile.html"), encoding="utf-8") as f:
        return f.read()

@app.get("/api/info")
async def get_server_info(request: Request):
    ip = get_server_ip(request)
    return {"ip": ip, "port": WEB_PORT}


@app.get("/api/config")
async def get_client_config():
    """
    프론트엔드가 소비하는 런타임 설정 (Pillar 1).
    고도화 이전에는 해상도/품질/전송주기가 mobile.html에 고정되어 있어
    네트워크 상황에 맞춰 조정할 방법이 없었다.
    """
    return {"stream": cfg.get("stream").to_dict()}


@app.get("/health")
async def health():
    """
    liveness — 이 프로세스가 살아 있는가.
    고도화 이전에는 A에만 /health가 없어 compose healthcheck를 걸 수 없었다.
    """
    return {"status": "ok", "service": "web_gateway"}


@app.get("/health/ready")
async def readiness():
    """
    readiness — 의존 서비스(B, C)까지 응답 가능한가.
    depends_on 만으로는 "프로세스 시작"만 보장되고 준비 완료는 보장되지 않는다.
    """
    # B의 /health/ready 를 호출하면 B가 다시 C를 확인하므로 체인 전체가 전이적으로 검증된다.
    # /health(자기 자신만 확인)를 부르면 C가 죽어도 ready로 보고되는 문제가 있다.
    vision_ready = CONTAINER_B_URL.rsplit("/", 1)[0] + "/health/ready"
    checks = {}
    # 헬스체크는 프레임 경로가 아니다. B가 C까지 확인하고 재시도할 시간을 넉넉히 준다.
    # 프레임용 타임아웃(1.5s)을 쓰면 정상 상황에서도 오탐이 난다.
    probe_timeout = httpx.Timeout(READINESS_TIMEOUT_S)
    async with httpx.AsyncClient(timeout=probe_timeout) as probe:
        try:
            resp = await probe.get(vision_ready)
            body = {}
            try:
                body = resp.json()
            except Exception:
                body = {"raw": resp.text[:120]}
            checks["vision_chain"] = {
                "ok": resp.status_code == 200,
                "status_code": resp.status_code,
                "upstream": body,
            }
        except Exception as exc:
            checks["vision_chain"] = {"ok": False, "error": type(exc).__name__}

    ready = all(item["ok"] for item in checks.values())
    if not ready:
        log.warning("readiness_failed", detail=checks)
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "degraded", "checks": checks},
    )

@app.get("/api/qr/{session_id}")
async def get_qr_code(session_id: str, request: Request, host: str = None):
    target_ip = get_server_ip(request, host)
    mobile_url = f"https://{target_ip}:{WEB_PORT}/mobile?session={session_id}"

    # QR에 실제로 어떤 주소가 들어갔는지 남긴다.
    # 폰이 접속되지 않을 때 "QR이 어디를 가리켰는가"가 1차 진단 정보다.
    log.info(
        "qr_issued",
        session_id=session_id,
        detail={
            "target_ip": target_ip,
            "mobile_url": mobile_url,
            "host_header": request.headers.get("host"),
            "host_override": host,
            "used_fallback_ip": target_ip == DEFAULT_HOST_IP,
        },
    )

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
    log.info(
        "pc_connected",
        session_id=session_id,
        detail={
            "mobile_already_connected": rooms[session_id]["mobile_ws"] is not None,
            "active_rooms": len(rooms),
        },
    )

    if rooms[session_id]["mobile_ws"] is not None:
        await websocket.send_json({"type": "STATUS", "mobile_connected": True})

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        _detach_session(session_id, "pc_ws")
        log.info("pc_disconnected", session_id=session_id, detail={"active_rooms": len(rooms)})
    except Exception:
        _detach_session(session_id, "pc_ws")
        log.exception("pc_ws_unexpected_error", session_id=session_id)

@app.websocket("/ws/mobile/{session_id}")
async def websocket_mobile(websocket: WebSocket, session_id: str):
    await websocket.accept()
    if session_id not in rooms:
        rooms[session_id] = {"pc_ws": None, "mobile_ws": None}
    rooms[session_id]["mobile_ws"] = websocket
    log.info(
        "mobile_connected",
        session_id=session_id,
        detail={"pc_already_connected": rooms[session_id]["pc_ws"] is not None},
    )

    pc_ws = rooms[session_id]["pc_ws"]
    if pc_ws:
        await pc_ws.send_json({"type": "STATUS", "mobile_connected": True})

    # 재시도 + 서킷 브레이커 (Pillar 2). 이전에는 단발 요청 + except: pass 였다.
    client = ResilientClient(cfg, log, peer="vision")

    # 프레임 단위 추적 컨텍스트. 장애 로그에 "몇 번째 프레임에서 터졌는지"를 남긴다.
    frame_seq = 0
    failed_frames = 0
    last_health = HealthState.OK
    # 프레임 처리 예산. 이 시간을 넘기면 재시도를 포기하고 프레임을 버린다.
    frame_budget_s = (cfg.get("stream.interval_ms") / 1000.0) * 2

    async def notify_pc(payload: dict) -> None:
        target = rooms.get(session_id, {}).get("pc_ws")
        if target:
            try:
                await target.send_json(payload)
            except Exception:
                log.exception("pc_notify_failed", session_id=session_id)

    async def set_health(new_health: str, reason: str = "") -> None:
        """상태가 바뀔 때만 통지한다. 매 프레임 보내면 그 자체가 트래픽이 된다."""
        nonlocal last_health
        if new_health == last_health:
            return
        last_health = new_health
        log.info(
            "pipeline_health_changed",
            session_id=session_id,
            detail={"health": new_health, "reason": reason, "circuit": client.circuit_state},
        )
        await notify_pc({"type": "STATUS", "health": new_health, "reason": reason})
        try:
            await websocket.send_json({"type": "STATUS", "health": new_health, "reason": reason})
        except Exception as exc:
            # 모바일 소켓이 이미 닫혔을 수 있다. 치명적이지 않지만 흔적은 남긴다.
            log.debug(
                "mobile_status_notify_failed",
                session_id=session_id,
                detail={"exception": type(exc).__name__},
            )

    try:
        while True:
            frame_data = await websocket.receive_text()
            frame_seq += 1
            trace_id = f"{session_id}-{frame_seq}"

            with Timer() as t:
                result = await client.post_json(
                    CONTAINER_B_URL,
                    {"session_id": session_id, "image": frame_data},
                    session_id=session_id,
                    trace_id=trace_id,
                    budget_s=frame_budget_s,
                )

            if result.ok:
                data = result.data or {}
                landmarks = data.get("landmarks", [])
                gesture = GestureResult.from_upstream(data, session_id=session_id)
                upstream_health = data.get("health", HealthState.OK)

                await set_health(upstream_health, data.get("error", "") or "")

                # 좌표는 스키마가 기본값을 보장하므로 null 이 흘러가지 않는다 (P2-6).
                await notify_pc({
                    "type": "GESTURE",
                    "action": gesture.action,
                    "x": gesture.x,
                    "y": gesture.y,
                    "delta": gesture.delta,
                    "pan_dx": gesture.pan_dx,
                    "pan_dy": gesture.pan_dy,
                    "landmarks": landmarks,
                    "health": upstream_health,
                })

                await websocket.send_json({
                    "type": "FEEDBACK",
                    "action": gesture.action,
                    "landmarks": landmarks,
                })

                log.sampled(
                    "frame_relayed",
                    session_id=session_id,
                    trace_id=trace_id,
                    detail={
                        "vision_rtt_ms": t.ms,
                        "action": gesture.action,
                        "detected": bool(landmarks),
                        "frame_bytes": len(frame_data),
                        "attempts": result.attempts,
                        "health": upstream_health,
                    },
                )
            else:
                # ----------------------------------------------------------
                # Graceful Degradation (Pillar 2)
                # 이전에는 `except Exception: pass` 로 전부 무음 처리되어
                # 사용자는 "그림이 안 그려진다"만 알고 원인을 알 수 없었다.
                # 이제 상태를 통지하고, 모바일에는 FEEDBACK 을 계속 돌려주어
                # 백프레셔(in-flight 카운터)가 잠기지 않게 한다.
                # ----------------------------------------------------------
                failed_frames += 1
                degraded = HealthState.DOWN if result.reason in (
                    "UNREACHABLE", "CIRCUIT_OPEN"
                ) else HealthState.DEGRADED
                await set_health(degraded, result.reason)

                log.sampled(
                    "frame_relay_failed",
                    session_id=session_id,
                    trace_id=trace_id,
                    level=logging.WARNING,
                    detail={
                        "reason": result.reason,
                        "attempts": result.attempts,
                        "failed_frames": failed_frames,
                        "circuit": client.circuit_state,
                        "elapsed_ms": t.ms,
                        **(result.detail or {}),
                    },
                )

                try:
                    await websocket.send_json({
                        "type": "FEEDBACK",
                        "action": "NONE",
                        "landmarks": [],
                        "health": degraded,
                    })
                except Exception as exc:
                    log.debug(
                        "mobile_feedback_notify_failed",
                        session_id=session_id,
                        trace_id=trace_id,
                        detail={"exception": type(exc).__name__},
                    )

    except WebSocketDisconnect:
        _detach_session(session_id, "mobile_ws")
        await notify_pc({"type": "STATUS", "mobile_connected": False})
        log.info(
            "mobile_disconnected",
            session_id=session_id,
            detail={
                "frames_received": frame_seq,
                "failed_frames": failed_frames,
                "failure_rate": round(failed_frames / frame_seq, 4) if frame_seq else 0.0,
                "final_health": last_health,
            },
        )
    except Exception:
        _detach_session(session_id, "mobile_ws")
        log.exception(
            "mobile_ws_unexpected_error",
            session_id=session_id,
            detail={"frames_received": frame_seq},
        )
    finally:
        await client.aclose()
