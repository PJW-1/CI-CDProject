import math
import time
import json
from collections import deque
from typing import Dict, List, Optional, Union
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from common.config import load_config, config_meta
from common.logging_setup import setup_logging

app = FastAPI(
    title="Air Canvas - Motion Engine (Container C)",
    description="21개 랜드마크 분석, EMA 손떨림 보정, 6대 제어 규칙 엔진 (WebSocket & HTTP 하이브리드 지원)"
)

# ---- 설정 (Pillar 1) + 공통 구조화 로깅 (Pillar 4) ----
cfg = load_config()
log = setup_logging("C", settings=cfg.get("logging"))

# 아래 상수들은 고도화 이전 코드에 직접 박혀 있던 값이다.
# 기본값은 원본과 동일하므로 베이스라인 동작은 변하지 않는다.
_TH = cfg.get("gesture.thresholds")
THUMB_OPEN_PALM_DIST = _TH.get("thumb_open_palm_dist")             # 이전: 0.15
THUMB_OPEN_INDEX_BASE_DIST = _TH.get("thumb_open_index_base_dist") # 이전: 0.12
THUMB_FOLDED_DIST = _TH.get("thumb_folded_dist")                   # 이전: 0.13
ERASE_HEIGHT_DIFF = _TH.get("erase_height_diff")                   # 이전: 0.12

ZOOM_STEP = cfg.get("gesture.zoom.step")                           # 이전: 0.008

_EMA = cfg.get("gesture.ema")
EMA_DEADZONE_DIST = _EMA.get("deadzone_dist")                      # 이전: 0.001
EMA_SLOW_DIST = _EMA.get("slow_dist")                              # 이전: 0.05
EMA_ALPHA_MICRO = _EMA.get("alpha_micro")                          # 이전: 0.35
EMA_ALPHA_PRECISE = _EMA.get("alpha_precise")                      # 이전: 0.50
EMA_ALPHA_FAST = _EMA.get("alpha_fast")                            # 이전: 0.85

_DEB = cfg.get("gesture.debounce")
DEBOUNCE_WINDOW = _DEB.get("window")                               # 이전: maxlen=3
DEBOUNCE_MAJORITY = _DEB.get("majority")                           # 이전: >= 2
INSTANT_PEN_UP = _DEB.get("instant_pen_up")                        # 이전: 항상 켜짐(하드코딩)

SESSION_TTL_S = cfg.get("gesture.session.ttl_s")                   # 이전: 600

log.info(
    "service_starting",
    detail={
        "service": "motion_engine",
        "config": config_meta(),
        "thresholds": _TH.to_dict(),
        "ema": _EMA.to_dict(),
        "debounce": _DEB.to_dict(),
        "session_ttl_s": SESSION_TTL_S,
    },
)

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
        
        self.action_queue: deque = deque(maxlen=DEBOUNCE_WINDOW)
        self.current_stable_action: str = "HOVER"
        self.last_updated: float = time.time()

sessions: Dict[str, SessionState] = {}

def get_or_create_session(session_id: str) -> SessionState:
    now = time.time()
    expired = [sid for sid, s in sessions.items() if now - s.last_updated > SESSION_TTL_S]
    for sid in expired:
        del sessions[sid]
    if expired:
        log.info(
            "sessions_expired",
            detail={"count": len(expired), "session_ids": expired, "remaining": len(sessions)},
        )

    if session_id not in sessions:
        sessions[session_id] = SessionState()
        log.info("session_created", session_id=session_id, detail={"active_sessions": len(sessions)})

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
        # 손 미검출은 정상 상황이므로 WARNING이 아니다. 다만 비율 추적을 위해 샘플링 기록한다.
        log.sampled(
            "landmarks_insufficient",
            session_id=session_id,
            detail={"received": len(lm), "required": 21},
        )
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
    thumb_open = (dist_thumb_to_palm > THUMB_OPEN_PALM_DIST) and (
        dist_thumb_to_index_base > THUMB_OPEN_INDEX_BASE_DIST
    )
    thumb_folded = dist_thumb_to_palm < THUMB_FOLDED_DIST

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
        if height_diff < ERASE_HEIGHT_DIFF:
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
        delta = ZOOM_STEP
        state.prev_pan_x = None
        state.prev_pan_y = None

    # 🤙 [규칙 4: ZOOM_OUT (화면 축소)] - 주먹 쥐고 새끼손가락만 폄
    elif pinky_open and not thumb_open and not index_open and not middle_open and not ring_open:
        raw_action = "ZOOM_OUT"
        raw_x = lm[20].x
        raw_y = lm[20].y
        delta = -ZOOM_STEP
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
    previous_action = state.current_stable_action
    instant_cutoff = False

    if INSTANT_PEN_UP and state.current_stable_action == "DRAW" and raw_action != "DRAW":
        state.action_queue.clear()
        state.action_queue.append(raw_action)
        state.current_stable_action = raw_action
        instant_cutoff = True
    else:
        state.action_queue.append(raw_action)
        action_counts = {}
        for act in state.action_queue:
            action_counts[act] = action_counts.get(act, 0) + 1

        most_common_action = max(action_counts, key=action_counts.get)
        if action_counts[most_common_action] >= DEBOUNCE_MAJORITY:
            state.current_stable_action = most_common_action

    final_action = state.current_stable_action

    # 상태 전이는 저빈도 · 고가치 이벤트다. 프레임 로그와 달리 샘플링하지 않고 전량 남긴다.
    # (프레임 단위 로그는 아래 gesture_frame 에서 샘플링 처리)
    if final_action != previous_action:
        log.info(
            "action_changed",
            session_id=session_id,
            detail={
                "from": previous_action,
                "to": final_action,
                "raw_action": raw_action,
                "instant_pen_up": instant_cutoff,
            },
        )

    # 4. 🌟 속도 적응형 최적화 EMA 손떨림 보정 (끊김 없는 고속 반응 튜닝)
    # alpha_used / move_dist_used 는 로깅 전용 관측값이다. 보정 로직 자체는 원본과 동일하다.
    if state.smooth_x is None or state.smooth_y is None:
        state.smooth_x = raw_x
        state.smooth_y = raw_y
        alpha_used = None          # 첫 프레임은 EMA를 적용하지 않는다
        move_dist_used = 0.0
    else:
        # 손가락 이동 거리(속도) 계산
        move_dist = math.sqrt((raw_x - state.smooth_x) ** 2 + (raw_y - state.smooth_y) ** 2)

        # 1) 초미세 진동 필터 (0.001 이하 미세 떨림만 부드럽게 흡수)
        if move_dist < EMA_DEADZONE_DIST:
            alpha = EMA_ALPHA_MICRO
        # 2) 정밀 글씨 쓰기 / 드로잉 구간 (alpha = 0.50)
        elif move_dist < EMA_SLOW_DIST:
            alpha = EMA_ALPHA_PRECISE
        # 3) 빠른 이동 구간 (alpha = 0.85, 렉/지연 0%)
        else:
            alpha = EMA_ALPHA_FAST

        state.smooth_x = alpha * raw_x + (1.0 - alpha) * state.smooth_x
        state.smooth_y = alpha * raw_y + (1.0 - alpha) * state.smooth_y
        alpha_used = alpha
        move_dist_used = move_dist

    result = {
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

    # 프레임 단위 로그는 30fps × 세션수 만큼 발생하므로 반드시 샘플링한다.
    # (기존 코드는 이것을 INFO로 전량 기록해 로그가 폭주하는 구조였다)
    log.sampled(
        "gesture_frame",
        session_id=session_id,
        detail={
            "action": final_action,
            "x": result["x"],
            "y": result["y"],
            "alpha": alpha_used,
            "move_dist": round(move_dist_used, 5),
        },
    )
    return result

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
    log.info("ws_connected", session_id=session_id, detail={"peer": "video_engine"})

    try:
        while True:
            raw_text = await websocket.receive_text()
            try:
                msg = json.loads(raw_text)
            except json.JSONDecodeError:
                # 기존에는 조용히 continue 하여 잘못된 페이로드가 흔적 없이 사라졌다.
                log.warning(
                    "ws_invalid_json",
                    session_id=session_id,
                    detail={"payload_bytes": len(raw_text), "preview": raw_text[:120]},
                )
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
                
                # 프레임 단위 결과 로그는 compute_gesture_logic 내부에서 샘플링 기록한다.
                # 여기서 다시 INFO로 남기면 30fps 전량이 쌓이므로 중복 기록하지 않는다.
                await websocket.send_text(json.dumps(result, ensure_ascii=False))

    except WebSocketDisconnect:
        log.info("ws_disconnected", session_id=session_id, detail={"peer": "video_engine"})
    except Exception:
        log.exception("ws_unexpected_error", session_id=session_id)
        raise

# ==========================================================
# 🚀 2. HTTP POST 엔드포인트 (기존 규격 및 단독 테스트 호환)
# ==========================================================
@app.post("/gesture")
async def process_gesture_http(payload: LandmarkPayload):
    # 실제 운영 경로는 이 HTTP 엔드포인트다(B → C). 기존에는 여기에 로그가 전혀 없어
    # WebSocket 경로만 관측 가능하고 정작 쓰이는 경로는 깜깜이였다.
    try:
        return compute_gesture_logic(payload.session_id, payload.landmarks)
    except Exception:
        log.exception(
            "gesture_compute_failed",
            session_id=payload.session_id,
            detail={"landmark_count": len(payload.landmarks)},
        )
        raise

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=cfg.get("network.gesture_port"))
