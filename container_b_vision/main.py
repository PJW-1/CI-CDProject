"""
Container B - 2번 비전 분석 엔진 (Vision Engine - Tasks API VIDEO 모드)
본 프로젝트 Video_Engine의 고성능 최신 구현을 그대로 이식
"""

import asyncio
import base64
import json
import logging
import os
import sys
import time
import urllib.request

import cv2
import numpy as np
import httpx
import mediapipe as mp
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from mediapipe.tasks import python as mp_tasks
from mediapipe.tasks.python import vision as mp_vision

CONTAINER = "B"
CONTAINER_C_URL = os.environ.get("CONTAINER_C_URL", "http://container_c:8002/gesture")

app = FastAPI(title="Air Canvas - Container B (Vision Engine)")

# ---- MediaPipe Hands Tasks API 초기화 (VIDEO 모드) ----
MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
MODEL_PATH = os.environ.get("HAND_LANDMARKER_MODEL_PATH", os.path.join(MODEL_DIR, "hand_landmarker.task"))
MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
    "hand_landmarker/float16/latest/hand_landmarker.task"
)

def ensure_model() -> bytes:
    """Hand Landmarker 모델 파일이 로컬에 없으면 최초 1회 다운로드"""
    if not os.path.exists(MODEL_PATH):
        os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
        urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
    with open(MODEL_PATH, "rb") as f:
        return f.read()

# 실시간 비디오 연속 추적 모드 (이전 프레임 기반 60FPS 부드러운 트래킹)
_hand_landmarker_options = mp_vision.HandLandmarkerOptions(
    base_options=mp_tasks.BaseOptions(model_asset_buffer=ensure_model()),
    num_hands=1,
    min_hand_detection_confidence=0.6,
    min_hand_presence_confidence=0.5,
    min_tracking_confidence=0.5,
    running_mode=mp_vision.RunningMode.VIDEO,
)
hands_detector = mp_vision.HandLandmarker.create_from_options(_hand_landmarker_options)

def decode_base64_frame(frame_b64: str):
    """base64 이미지를 OpenCV BGR 이미지로 디코딩"""
    if "," in frame_b64:
        frame_b64 = frame_b64.split(",")[1]
    jpg_bytes = base64.b64decode(frame_b64)
    np_arr = np.frombuffer(jpg_bytes, dtype=np.uint8)
    return cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

def extract_landmarks(image_bgr):
    """이미지에서 손 21개 랜드마크의 (x, y, z) 정규화(0~1) 좌표 추출"""
    if image_bgr is None:
        return False, []

    image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=image_rgb)
    
    timestamp_ms = int(time.time() * 1000)
    result = hands_detector.detect_for_video(mp_image, timestamp_ms)

    if not result.hand_landmarks:
        return False, []

    first_hand = result.hand_landmarks[0]
    landmarks = [{"x": lm.x, "y": lm.y, "z": lm.z} for lm in first_hand]
    return True, landmarks

class FramePayload(BaseModel):
    session_id: str
    image: str

http_client = httpx.AsyncClient(timeout=0.3)

@app.get("/health")
def health():
    return {"status": "ok", "service": "video_engine"}

@app.post("/analyze")
async def analyze_frame(payload: FramePayload):
    try:
        image = decode_base64_frame(payload.image)
        detected, landmarks_list = extract_landmarks(image)

        if not detected:
            return {"success": True, "action": "NONE", "landmarks": []}

        # 21개 관절 좌표를 Container C (모션 엔진)로 전달
        resp = await http_client.post(CONTAINER_C_URL, json={
            "session_id": payload.session_id,
            "landmarks": landmarks_list
        })

        if resp.status_code == 200:
            c_result = resp.json()
            c_result["landmarks"] = landmarks_list
            c_result["success"] = True
            return c_result
        else:
            return {"success": False, "error": "Container C response error"}

    except Exception as e:
        return {"success": False, "error": str(e)}

@app.on_event("shutdown")
async def shutdown():
    await http_client.aclose()
    hands_detector.close()
