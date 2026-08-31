import math
import time
import json
import logging
import sys
from collections import deque
from typing import List, Dict, Any, Optional, Union
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

app = FastAPI(
    title="Air Canvas - Motion Engine (Container C)",
    description="21개 랜드마크 분석, EMA 손떨림 보정, 6대 제어 규칙 엔진 (WebSocket & HTTP 하이브리드 지원)"
)

# ---- 공통 로깅 설정 ----
logger = logging.getLogger("motion_engine")
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(logging.StreamHandler(sys.stdout))
logger.propagate = False

def log_event(level: str, session_id: str, event: str, detail: dict = None) -> None:
    record = {
        "ts": int(time.time() * 1000),
        "container": "C",
        "session_id": session_id,
        "level": level,
        "event": event,
        "detail": detail or {},
    }
    logger.info(json.dumps(record, ensure_ascii=False))

# ==========================================================
# 📋 Pydantic 데이터 모델 (HTTP POST용)
# ==========================================================
class LandmarkItem(BaseModel):
    x: float
    y: float
    z: float = 0.0

class LandmarkPayload(BaseModel):
    session_id: str
    landmarks: List[Union[LandmarkItem, List[float], Dict[str, float]]]

# ==========================================================
# 🌟 세션별 상태 관리 클래스 (EMA + 3프레임 디바운스)
# ==========================================================
class SessionState:
    def __init__(self):
        self.smooth_x: Optional[float] = None
        self.smooth_y: Optional[float] = None
        
        self.prev_pan_x: Optional[float] = None
        self.prev_pan_y: Optional[float] = None
        
        self.action_queue: deque = deque(maxlen=3)
        self.current_stable_action: str = "HOVER"
        self.last_updated: float = time.time()

sessions: Dict[str, SessionState] = {}

def get_or_create_session(session_id: str) -> SessionState:
    now = time.time()
    expired = [sid for sid, s in sessions.items() if now - s.last_updated > 600]
    for sid in expired:
        del sessions[sid]

    if session_id not in sessions:
        sessions[session_id] = SessionState()
    
    session = sessions[session_id]
    session.last_updated = now
    return session

class Point:
    def __init__(self, x: float, y: float, z: float = 0.0):
        self.x = x
        self.y = y
        self.z = z

def parse_landmarks(raw_landmarks: list) -> List[Point]:
    """2차원 리스트 [[x, y], ...] 또는 딕셔너리 [{'x':x, 'y':y}, ...]를 Point 객체 배열로 변환"""
    points = []
    for item in raw_landmarks:
        if isinstance(item, (list, tuple)):
            x = float(item[0])
            y = float(item[1])
            z = float(item[2]) if len(item) > 2 else 0.0
            points.append(Point(x, y, z))
        elif isinstance(item, dict):
            points.append(Point(float(item.get("x", 0.0)), float(item.get("y", 0.0)), float(item.get("z", 0.0))))
        elif hasattr(item, "x") and hasattr(item, "y"):
            points.append(Point(float(item.x), float(item.y), float(getattr(item, "z", 0.0))))
    return points

def calculate_distance(p1: Point, p2: Point) -> float:
    return math.sqrt((p1.x - p2.x) ** 2 + (p1.y - p2.y) ** 2)

def compute_gesture_logic(session_id: str, raw_landmarks: list) -> dict:
    """핵심 제스처 연산, EMA 보정, 디바운스 로직 (HTTP 및 WebSocket 공통 실행)"""
    lm = parse_landmarks(raw_landmarks)

    if not lm or len(lm) < 21:
        return {
            "session_id": session_id,
            "action": "NONE",
            "x": 0.5,
            "y": 0.5,
            "delta": 0.0,
            "pan_dx": 0.0,
            "pan_dy": 0.0,
            "detected": False
        }

    state = get_or_create_session(session_id)

    # 1. 5개 손가락 상태 정밀 분석
    index_open = lm[8].y < lm[6].y
    middle_open = lm[12].y < lm[10].y
    ring_open = lm[16].y < lm[14].y
    pinky_open = lm[20].y < lm[18].y

    dist_thumb_to_palm = calculate_distance(lm[4], lm[9])
    dist_thumb_to_index_base = calculate_distance(lm[4], lm[5])
    thumb_open = (dist_thumb_to_palm > 0.15) and (dist_thumb_to_index_base > 0.12)
    thumb_folded = dist_thumb_to_palm < 0.13

    palm_center_x = (lm[0].x + lm[9].x) / 2.0
    palm_center_y = (lm[0].y + lm[9].y) / 2.0

    # 2. 6대 제어 규칙 판별
    raw_action = "HOVER"
    raw_x = lm[8].x
    raw_y = lm[8].y
    delta = 0.0
    pan_dx = 0.0
    pan_dy = 0.0

    # 🖊️ [규칙 1: DRAW (펜 모드)] - 검지만 펴지고 나머지 모두 접힘
    if index_open and not middle_open and not ring_open and not pinky_open and not thumb_open:
        raw_action = "DRAW"
        raw_x = lm[8].x
        raw_y = lm[8].y
        state.prev_pan_x = None
        state.prev_pan_y = None

    # 🧹 [규칙 2: ERASE (지우개)] - 의도된 브이(✌️) 제스처
    elif index_open and middle_open and not ring_open and not pinky_open and not thumb_open:
        height_diff = abs(lm[8].y - lm[12].y)
        if height_diff < 0.12:
            raw_action = "ERASE"
            raw_x = (lm[8].x + lm[12].x) / 2.0
            raw_y = (lm[8].y + lm[12].y) / 2.0
        else:
            raw_action = "HOVER"
        state.prev_pan_x = None
        state.prev_pan_y = None

    # 👍 [규칙 3: ZOOM_IN (화면 확대)] - 주먹 쥐고 엄지만 폄
    elif thumb_open and not index_open and not middle_open and not ring_open and not pinky_open:
        raw_action = "ZOOM_IN"
        raw_x = lm[4].x
        raw_y = lm[4].y
        delta = 0.008
        state.prev_pan_x = None
        state.prev_pan_y = None

    # 🤙 [규칙 4: ZOOM_OUT (화면 축소)] - 주먹 쥐고 새끼손가락만 폄
    elif pinky_open and not thumb_open and not index_open and not middle_open and not ring_open:
        raw_action = "ZOOM_OUT"
        raw_x = lm[20].x
        raw_y = lm[20].y
        delta = -0.008
        state.prev_pan_x = None
        state.prev_pan_y = None

    # ✊ [규칙 5: PAN (화면 드래그)] - 5개 손가락 모두 쥔 주먹
    elif not index_open and not middle_open and not ring_open and not pinky_open and thumb_folded:
        raw_action = "PAN"
        raw_x = palm_center_x
        raw_y = palm_center_y

        if state.prev_pan_x is not None and state.prev_pan_y is not None:
            pan_dx = - (palm_center_x - state.prev_pan_x)
            pan_dy = (palm_center_y - state.prev_pan_y)
        
        state.prev_pan_x = palm_center_x
        state.prev_pan_y = palm_center_y

    # 🖐️ [규칙 6: HOVER (대기 모드)]
    else:
        raw_action = "HOVER"
        raw_x = lm[8].x
        raw_y = lm[8].y
        state.prev_pan_x = None
        state.prev_pan_y = None

    # 3. 🌟 비대칭 무지연 펜-업 디바운스 필터 (Asymmetric Instant Cutoff)
    # 손을 펴는 순간(DRAW -> 비DRAW)에는 다수결 지연 없이 0초 만에 칼같이 펜-업하여 한자 삐침 100% 차단!
    if state.current_stable_action == "DRAW" and raw_action != "DRAW":
        state.action_queue.clear()
        state.action_queue.append(raw_action)
        state.current_stable_action = raw_action
    else:
        state.action_queue.append(raw_action)
        action_counts = {}
        for act in state.action_queue:
            action_counts[act] = action_counts.get(act, 0) + 1
        
        most_common_action = max(action_counts, key=action_counts.get)
        if action_counts[most_common_action] >= 2:
            state.current_stable_action = most_common_action

    final_action = state.current_stable_action

    # 4. 🌟 속도 적응형 최적화 EMA 손떨림 보정 (끊김 없는 고속 반응 튜닝)
    if state.smooth_x is None or state.smooth_y is None:
        state.smooth_x = raw_x
        state.smooth_y = raw_y
    else:
        # 손가락 이동 거리(속도) 계산
        move_dist = math.sqrt((raw_x - state.smooth_x) ** 2 + (raw_y - state.smooth_y) ** 2)
        
        # 1) 초미세 진동 필터 (0.001 이하 미세 떨림만 부드럽게 흡수)
        if move_dist < 0.001:
            alpha = 0.35
        # 2) 정밀 글씨 쓰기 / 드로잉 구간 (alpha = 0.50)
        elif move_dist < 0.05:
            alpha = 0.50
        # 3) 빠른 이동 구간 (alpha = 0.85, 렉/지연 0%)
        else:
            alpha = 0.85

        state.smooth_x = alpha * raw_x + (1.0 - alpha) * state.smooth_x
        state.smooth_y = alpha * raw_y + (1.0 - alpha) * state.smooth_y

    return {
        "session_id": session_id,
        "type": "gesture",
        "action": final_action,
        "x": round(float(state.smooth_x), 4),
        "y": round(float(state.smooth_y), 4),
        "delta": round(float(delta), 4),
        "pan_dx": round(float(pan_dx), 4),
        "pan_dy": round(float(pan_dy), 4),
        "detected": True
    }

# ==========================================================
# 🩺 헬스체크 엔드포인트
# ==========================================================
@app.get("/health")
def health():
    return {"status": "ok", "service": "motion_engine"}

# ==========================================================
# 🌐 1. WebSocket 엔드포인트 (2번 Video_Engine 전용 초고속 연결)
# ==========================================================
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    session_id = "default"
    log_event("INFO", session_id, "ws_connected", {"peer": "video_engine"})

    try:
        while True:
            raw_text = await websocket.receive_text()
            try:
                msg = json.loads(raw_text)
            except json.JSONDecodeError:
                continue

            session_id = msg.get("session_id", session_id)
            msg_type = msg.get("type", "")
            
            if msg_type == "landmarks":
                detected = msg.get("detected", False)
                landmarks = msg.get("landmarks", [])
                
                if detected and landmarks:
                    result = compute_gesture_logic(session_id, landmarks)
                else:
                    result = {
                        "session_id": session_id,
                        "type": "gesture",
                        "action": "HOVER",
                        "x": 0.5,
                        "y": 0.5,
                        "delta": 0.0,
                        "pan_dx": 0.0,
                        "pan_dy": 0.0,
                        "detected": False
                    }
                
                log_event("INFO", session_id, "gesture_computed", {"action": result["action"]})
                await websocket.send_text(json.dumps(result, ensure_ascii=False))

    except WebSocketDisconnect:
        log_event("INFO", session_id, "ws_disconnected", {"peer": "video_engine"})

# ==========================================================
# 🚀 2. HTTP POST 엔드포인트 (기존 규격 및 단독 테스트 호환)
# ==========================================================
@app.post("/gesture")
async def process_gesture_http(payload: LandmarkPayload):
    return compute_gesture_logic(payload.session_id, payload.landmarks)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
