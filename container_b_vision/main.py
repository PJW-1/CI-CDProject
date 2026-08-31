"""
Container B - 2번 비전 분석 엔진 (Vision Engine - Tasks API VIDEO 모드)
본 프로젝트 Video_Engine의 고성능 최신 구현을 그대로 이식
"""

import base64
import binascii
import logging
import os
import urllib.request
from contextlib import asynccontextmanager

import cv2
import mediapipe as mp
import numpy as np
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mediapipe.tasks import python as mp_tasks
from mediapipe.tasks.python import vision as mp_vision
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from common.config import config_meta, load_config
from common.detector_pool import DetectorPool
from common.http_client import ResilientClient
from common.logging_setup import Timer, setup_logging
from common.schemas import AnalyzeResponse, GestureResult, HealthState

CONTAINER = "B"

# ---- 설정 (Pillar 1) + 공통 구조화 로깅 (Pillar 4) ----
# 기존 B는 logging을 import만 하고 단 한 줄도 사용하지 않아 완전 무로깅 상태였다.
# 파이프라인 한가운데(가장 무거운 구간)가 관측 불가였다는 뜻이다.
# 미사용 import(asyncio, json, sys)도 함께 정리했다.
cfg = load_config()
log = setup_logging("B", settings=cfg.get("logging"))

CONTAINER_C_URL = cfg.get("network.gesture_url")
MAX_FRAME_BYTES = cfg.get("vision.max_frame_bytes")

app = FastAPI(title="Air Canvas - Container B (Vision Engine)")

# ---- MediaPipe Hands Tasks API 초기화 (VIDEO 모드) ----
MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
_configured_model_path = cfg.get("vision.model_path")
MODEL_PATH = (
    _configured_model_path
    if os.path.isabs(_configured_model_path)
    else os.path.join(MODEL_DIR, cfg.get("vision.model_filename"))
)
MODEL_URL = cfg.get("vision.model_url")

def ensure_model() -> bytes:
    """Hand Landmarker 모델 파일이 로컬에 없으면 최초 1회 다운로드"""
    if not os.path.exists(MODEL_PATH):
        log.info("model_download_started", detail={"url": MODEL_URL, "path": MODEL_PATH})
        os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
        try:
            with Timer() as t:
                urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
            log.info(
                "model_download_completed",
                detail={"duration_ms": t.ms, "size_bytes": os.path.getsize(MODEL_PATH)},
            )
        except Exception:
            # 이 실패는 컨테이너 기동 자체를 막는다. 원인을 반드시 남겨야 한다.
            # (타임아웃·재시도·체크섬 검증은 Pillar 2에서 추가한다)
            log.exception("model_download_failed", detail={"url": MODEL_URL, "path": MODEL_PATH})
            raise
    else:
        log.info(
            "model_cache_hit",
            detail={"path": MODEL_PATH, "size_bytes": os.path.getsize(MODEL_PATH)},
        )

    with open(MODEL_PATH, "rb") as f:
        return f.read()

# 실시간 비디오 연속 추적 모드 (이전 프레임 기반 60FPS 부드러운 트래킹)
_hand_landmarker_options = mp_vision.HandLandmarkerOptions(
    base_options=mp_tasks.BaseOptions(model_asset_buffer=ensure_model()),
    num_hands=cfg.get("vision.num_hands"),
    min_hand_detection_confidence=cfg.get("vision.min_hand_detection_confidence"),
    min_hand_presence_confidence=cfg.get("vision.min_hand_presence_confidence"),
    min_tracking_confidence=cfg.get("vision.min_tracking_confidence"),
    running_mode=mp_vision.RunningMode.VIDEO,
)
def _create_detector():
    """세션마다 독립된 detector 인스턴스를 만든다 (Pillar 3)."""
    return mp_vision.HandLandmarker.create_from_options(_hand_landmarker_options)


with Timer() as _init_timer:
    # 기동 시 1개를 미리 만들어 모델 로딩 비용과 정상 동작을 확인한다.
    _warmup = _create_detector()
    _warmup.close()

detector_pool = DetectorPool(
    _create_detector,
    max_size=cfg.get("vision.detector_pool_size"),
    idle_ttl_s=cfg.get("vision.detector_idle_ttl_s"),
    log=log,
)

log.info(
    "service_starting",
    detail={
        "service": "vision_engine",
        "container_c_url": CONTAINER_C_URL,
        "config": config_meta(),
        "detector_init_ms": _init_timer.ms,
        "running_mode": "VIDEO",
        "num_hands": cfg.get("vision.num_hands"),
        "detector_scope": "per_session_pool",
        "detector_pool_size": cfg.get("vision.detector_pool_size"),
    },
)

class FrameValidationError(ValueError):
    """입력 프레임이 규격을 벗어났다. 내부 오류가 아니라 클라이언트 오류다."""


def decode_base64_frame(frame_b64: str):
    """
    base64 이미지를 OpenCV BGR 이미지로 디코딩한다.

    고도화 이전에는 검증이 전혀 없어서
      - 잘못된 base64 → binascii.Error 가 그대로 터졌고
      - 크기 상한이 없어 대용량 페이로드로 메모리를 압박할 수 있었으며
      - imdecode 가 None을 반환해도 암묵적으로만 처리됐다.
    """
    if not frame_b64:
        raise FrameValidationError("빈 프레임")

    if len(frame_b64) > MAX_FRAME_BYTES:
        raise FrameValidationError(
            f"프레임이 상한을 초과함 ({len(frame_b64)} > {MAX_FRAME_BYTES})"
        )

    if "," in frame_b64:
        frame_b64 = frame_b64.split(",", 1)[1]

    try:
        jpg_bytes = base64.b64decode(frame_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise FrameValidationError(f"base64 디코딩 실패: {exc}") from exc

    np_arr = np.frombuffer(jpg_bytes, dtype=np.uint8)
    image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    if image is None:
        raise FrameValidationError("JPEG 디코딩 실패 (손상된 이미지)")
    return image

def extract_landmarks(image_bgr, session_id: str):
    """
    이미지에서 손 21개 랜드마크의 (x, y, z) 정규화(0~1) 좌표를 추출한다.

    수정 전: 전역 detector 1개 + int(time.time()*1000)
      → 동시 세션 간 추적 상태 오염 + 타임스탬프 단조 증가 위반
    수정 후: 세션별 detector + 세션별 단조 증가 카운터
    """
    if image_bgr is None:
        return False, []

    image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=image_rgb)

    with detector_pool.acquire(session_id) as (detector, timestamp_ms):
        result = detector.detect_for_video(mp_image, timestamp_ms)

    if not result.hand_landmarks:
        return False, []

    first_hand = result.hand_landmarks[0]
    landmarks = [{"x": lm.x, "y": lm.y, "z": lm.z} for lm in first_hand]
    return True, landmarks

class FramePayload(BaseModel):
    session_id: str
    image: str

# 재시도 + 서킷 브레이커를 갖춘 클라이언트 (Pillar 2)
gesture_client = ResilientClient(cfg, log, peer="gesture")


@app.get("/health")
def health():
    return {"status": "ok", "service": "video_engine", "detector_pool": detector_pool.stats()}


@app.get("/health/ready")
async def readiness():
    """의존 서비스(C)까지 확인하는 준비 상태 체크."""
    result = await gesture_client.post_json(
        CONTAINER_C_URL, {"session_id": "_healthcheck", "landmarks": []}
    )
    ready = result.ok
    if not ready:
        log.warning("readiness_failed", detail={"reason": result.reason, "peer": "gesture"})
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "degraded",
            "circuit": gesture_client.circuit_state,
            "reason": None if ready else result.reason,
        },
    )


@app.post("/analyze")
async def analyze_frame(payload: FramePayload):
    sid = payload.session_id
    landmarks_list: list = []

    try:
        # 구간별 소요시간을 나눠 측정한다. "전처리가 모델보다 느린가"(Pillar 3)를
        # 판단하려면 디코딩과 추론을 반드시 분리해서 봐야 한다.
        try:
            with Timer() as t_decode:
                image = decode_base64_frame(payload.image)
        except FrameValidationError as exc:
            # 클라이언트 입력 오류다. 서버 장애처럼 ERROR로 남기지 않는다.
            log.warning(
                "frame_rejected",
                session_id=sid,
                detail={"reason": str(exc), "payload_chars": len(payload.image or "")},
            )
            return AnalyzeResponse(
                success=False, health=HealthState.OK, error="invalid_frame"
            ).model_dump()

        # CPU 바운드 작업을 스레드풀로 오프로딩한다 (Pillar 3).
        # 이전에는 async 핸들러 안에서 직접 호출해 이벤트 루프 전체를 블로킹했다.
        with Timer() as t_infer:
            detected, landmarks_list = await run_in_threadpool(extract_landmarks, image, sid)

        if not detected:
            log.sampled(
                "hand_not_detected",
                session_id=sid,
                detail={
                    "decode_ms": t_decode.ms,
                    "inference_ms": t_infer.ms,
                    "frame_shape": list(image.shape),
                },
            )
            return AnalyzeResponse(success=True, detected=False).model_dump()

        # 21개 관절 좌표를 Container C (모션 엔진)로 전달
        with Timer() as t_gesture:
            result = await gesture_client.post_json(
                CONTAINER_C_URL,
                {"session_id": sid, "landmarks": landmarks_list},
                session_id=sid,
            )

        if result.ok:
            gesture = GestureResult.from_upstream(result.data, session_id=sid)
            log.sampled(
                "frame_analyzed",
                session_id=sid,
                detail={
                    "decode_ms": t_decode.ms,
                    "inference_ms": t_infer.ms,
                    "gesture_rtt_ms": t_gesture.ms,
                    "total_ms": round(t_decode.ms + t_infer.ms + t_gesture.ms, 2),
                    "action": gesture.action,
                    "attempts": result.attempts,
                },
            )
            return AnalyzeResponse.from_gesture(gesture, landmarks_list).model_dump()

        # ------------------------------------------------------------------
        # Graceful Degradation (Pillar 2)
        # C가 죽어도 파이프라인 전체를 멈추지 않는다.
        # 랜드마크는 이미 확보했으므로 PC는 손 스켈레톤과 커서를 계속 그릴 수 있다.
        # 이전에는 {"success": false} 만 반환해 드로잉이 통째로 정지했고
        # 사용자에게는 아무 안내도 없었다.
        # ------------------------------------------------------------------
        level = logging.ERROR if result.reason in ("UNREACHABLE", "CIRCUIT_OPEN") else logging.WARNING
        log.sampled(
            "gesture_degraded",
            session_id=sid,
            level=level,
            detail={
                "reason": result.reason,
                "circuit": gesture_client.circuit_state,
                "attempts": result.attempts,
                **(result.detail or {}),
            },
        )
        health = HealthState.DOWN if result.reason in ("UNREACHABLE", "CIRCUIT_OPEN") else HealthState.DEGRADED
        return AnalyzeResponse.degraded(result.reason, landmarks_list, health).model_dump()

    except Exception as exc:
        # 기존에는 str(e)만 응답에 담고 traceback을 버렸다.
        # 내부 오류 메시지를 외부로 노출하는 문제도 함께 있었다.
        log.exception("analyze_failed", session_id=sid)
        return AnalyzeResponse.degraded(
            type(exc).__name__, landmarks_list, HealthState.DEGRADED
        ).model_dump()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """deprecated 된 @app.on_event 를 대체한다 (Pillar 2)."""
    log.info("service_ready", detail={"service": "vision_engine"})
    try:
        yield
    finally:
        log.info("service_stopping", detail={"service": "vision_engine"})
        await gesture_client.aclose()
        detector_pool.close_all()


app.router.lifespan_context = lifespan
