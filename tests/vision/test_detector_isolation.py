"""
실제 MediaPipe 를 사용하는 무거운 테스트 (CI의 test-vision 잡)

여기서만 진짜 HandLandmarker 를 만든다. 다른 테스트는 전부 가짜 detector 를 쓴다.
MediaPipe 설치가 수백 MB라 CI에서 별도 잡으로 분리되어 있다.

검증 대상은 P3-2 — 고도화 이전의 다음 두 결함이다.

    hands_detector = mp_vision.HandLandmarker.create_from_options(...)  # 전역 1개
    timestamp_ms = int(time.time() * 1000)                              # 벽시계

여기서 가장 중요한 것은 test_wallclock_timestamp_is_actually_unsafe 다.
"수정 전 코드가 왜 위험했는가"를 실제 MediaPipe 동작으로 증명한다.
"""

import os
import sys
import threading

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

pytestmark = pytest.mark.vision

mp = pytest.importorskip("mediapipe", reason="MediaPipe 미설치 환경에서는 건너뛴다")
cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from mediapipe.tasks import python as mp_tasks  # noqa: E402
from mediapipe.tasks.python import vision as mp_vision  # noqa: E402

from common.config import load_config  # noqa: E402
from common.detector_pool import DetectorPool  # noqa: E402


@pytest.fixture(scope="module")
def model_bytes():
    """모델을 로드한다. CI에서는 actions/cache 로 캐시된다."""
    import urllib.request

    cfg = load_config(reload=True)
    model_dir = os.path.join(REPO_ROOT, "container_b_vision", "models")
    path = os.path.join(model_dir, cfg.get("vision.model_filename"))

    if not os.path.exists(path):
        os.makedirs(model_dir, exist_ok=True)
        urllib.request.urlretrieve(cfg.get("vision.model_url"), path)

    with open(path, "rb") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def detector_factory(model_bytes):
    cfg = load_config()

    def factory():
        options = mp_vision.HandLandmarkerOptions(
            base_options=mp_tasks.BaseOptions(model_asset_buffer=model_bytes),
            num_hands=cfg.get("vision.num_hands"),
            min_hand_detection_confidence=cfg.get("vision.min_hand_detection_confidence"),
            min_hand_presence_confidence=cfg.get("vision.min_hand_presence_confidence"),
            min_tracking_confidence=cfg.get("vision.min_tracking_confidence"),
            running_mode=mp_vision.RunningMode.VIDEO,
        )
        return mp_vision.HandLandmarker.create_from_options(options)

    return factory


@pytest.fixture
def frame():
    """MediaPipe 입력용 합성 프레임."""
    image = np.full((360, 480, 3), 60, dtype=np.uint8)
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    return mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)


# ---------------------------------------------------------------------------
# 수정 전 방식이 왜 위험했는가
# ---------------------------------------------------------------------------
def test_wallclock_timestamp_is_actually_unsafe(detector_factory, frame):
    """
    detect_for_video 는 단조 증가 타임스탬프를 강제한다.

    고도화 이전 코드는 int(time.time() * 1000) 을 썼다.
    30fps × 다중 세션에서 같은 밀리초에 두 프레임이 들어가거나 순서가 뒤집히면
    아래처럼 예외가 발생한다.

    수정 전에는 이벤트 루프 블로킹 덕에 호출이 직렬화되어 드러나지 않았을 뿐이며,
    스레드풀을 도입하는 순간 실제로 터지는 구조였다.
    """
    detector = detector_factory()
    try:
        detector.detect_for_video(frame, 1000)
        with pytest.raises(Exception) as exc_info:
            detector.detect_for_video(frame, 1000)   # 같은 타임스탬프 재사용

        # 메시지 문구는 MediaPipe 버전마다 다르므로 여러 표현을 허용한다.
        # (로컬 1.0.1 / CI 0.10.14)
        message = str(exc_info.value).lower()
        assert any(k in message for k in ("timestamp", "monotonic", "increas")), (
            f"타임스탬프 관련 예외가 아니다: {message}"
        )
    finally:
        detector.close()


def test_pool_timestamps_never_collide(detector_factory, frame):
    """
    풀이 발급하는 타임스탬프는 절대 충돌하지 않는다.
    같은 세션으로 연속 호출해도 예외가 없어야 한다.
    """
    cfg = load_config()
    pool = DetectorPool(
        detector_factory,
        max_size=cfg.get("vision.detector_pool_size"),
        idle_ttl_s=cfg.get("vision.detector_idle_ttl_s"),
    )
    try:
        for _ in range(30):
            with pool.acquire("same_session") as (detector, timestamp_ms):
                detector.detect_for_video(frame, timestamp_ms)
    finally:
        pool.close_all()


# ---------------------------------------------------------------------------
# 세션 격리
# ---------------------------------------------------------------------------
def test_concurrent_sessions_do_not_corrupt_each_other(detector_factory, frame):
    """
    동시 2세션이 서로의 추적 상태를 오염시키지 않아야 한다 (P3-2 회귀 방지).

    고도화 이전에는 전역 detector 1개를 VIDEO 모드로 공유했으므로
    두 사용자의 프레임이 하나의 추적 상태에 섞여 들어갔다.
    """
    cfg = load_config()
    pool = DetectorPool(detector_factory, max_size=4, idle_ttl_s=60)
    errors = []

    def run(session_id):
        try:
            for _ in range(20):
                with pool.acquire(session_id) as (detector, timestamp_ms):
                    detector.detect_for_video(frame, timestamp_ms)
        except Exception as exc:   # noqa: BLE001 - 테스트에서 원인을 그대로 보고한다
            errors.append(f"{session_id}: {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=run, args=(f"user_{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    pool.close_all()
    assert not errors, "동시 세션에서 예외 발생:\n  " + "\n  ".join(errors)
    assert cfg.get("vision.detector_pool_size") >= 2


def test_pool_reuses_instance_within_session(detector_factory):
    """같은 세션은 같은 detector 를 재사용해야 한다 (VIDEO 모드 추적 연속성)."""
    pool = DetectorPool(detector_factory, max_size=4, idle_ttl_s=60)
    try:
        with pool.acquire("stable") as (first, _):
            pass
        with pool.acquire("stable") as (second, _):
            pass
        assert first is second
        assert pool.stats()["created"] == 1
    finally:
        pool.close_all()


# ---------------------------------------------------------------------------
# 파이프라인 연동
# ---------------------------------------------------------------------------
def test_extract_landmarks_handles_blank_frame():
    """
    Container B 의 실제 추출 함수가 손 없는 프레임에서 예외 없이 동작해야 한다.
    (모듈 import 시 모델 로드가 일어나므로 이 테스트만 별도로 무겁다)
    """
    sys.path.insert(0, os.path.join(REPO_ROOT, "container_b_vision"))
    import container_b_vision.main as vision_main

    image = np.full((360, 480, 3), 60, dtype=np.uint8)
    detected, landmarks = vision_main.extract_landmarks(image, "vision_test")

    assert detected is False, "단색 프레임에서 손이 검출되면 안 된다"
    assert landmarks == []


def test_frame_validation_rejects_bad_input():
    """입력 검증이 실제로 동작하는지 (P2-4)."""
    sys.path.insert(0, os.path.join(REPO_ROOT, "container_b_vision"))
    import container_b_vision.main as vision_main

    for bad in ["", "data:image/jpeg;base64,!!!not-base64!!!", "x" * 10]:
        with pytest.raises(vision_main.FrameValidationError):
            vision_main.decode_base64_frame(bad)
