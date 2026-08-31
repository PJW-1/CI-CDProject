"""
공통 테스트 픽스처.

핵심은 `hand` 팩토리다. 실제 카메라 없이 21개 랜드마크를 합성해
6대 제스처를 결정론적으로 재현한다. 이것이 Container C 회귀 테스트의 토대다.
"""

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# 테스트는 항상 기본 설정으로 돈다. 개발자 로컬 환경변수의 영향을 받으면 안 된다.
os.environ.pop("APP_ENV", None)
os.environ.setdefault("LOG_LEVEL", "CRITICAL")   # 테스트 출력에 로그 노이즈 방지
os.environ.setdefault("LOG_DIR", "")


# MediaPipe 랜드마크 인덱스 (손가락별 끝/중간 관절)
TIP_PIP = {
    "index": (8, 6),
    "middle": (12, 10),
    "ring": (16, 14),
    "pinky": (20, 18),
}


def make_hand(
    index=False,
    middle=False,
    ring=False,
    pinky=False,
    thumb_x=0.5,
    thumb_y=0.5,
    index_y=None,
    middle_y=None,
    wrist=(0.5, 0.6),
    palm=(0.5, 0.5),
):
    """
    21개 랜드마크를 합성한다.

    MediaPipe 좌표계에서 y는 아래로 갈수록 커진다.
    따라서 "손가락을 폈다" = 끝(tip)이 중간관절(pip)보다 y가 작다.

    Args:
        index/middle/ring/pinky: 해당 손가락을 폈는지
        thumb_x, thumb_y: 엄지 끝(4번) 위치. 손바닥(9번)과의 거리로 폄/접힘이 판정된다
        index_y, middle_y: ERASE 높이차 경계 테스트를 위한 미세 조정
    """
    points = [{"x": 0.5, "y": 0.5, "z": 0.0} for _ in range(21)]

    for name, (tip, pip) in TIP_PIP.items():
        opened = {"index": index, "middle": middle, "ring": ring, "pinky": pinky}[name]
        points[pip] = {"x": 0.5, "y": 0.5, "z": 0.0}
        points[tip] = {"x": 0.5, "y": 0.3 if opened else 0.7, "z": 0.0}

    if index_y is not None:
        points[8]["y"] = index_y
    if middle_y is not None:
        points[12]["y"] = middle_y

    points[4] = {"x": thumb_x, "y": thumb_y, "z": 0.0}      # 엄지 끝
    points[5] = {"x": 0.5, "y": 0.5, "z": 0.0}              # 검지 밑동
    points[9] = {"x": palm[0], "y": palm[1], "z": 0.0}      # 손바닥 중심
    points[0] = {"x": wrist[0], "y": wrist[1], "z": 0.0}    # 손목
    return points


@pytest.fixture
def hand():
    return make_hand


@pytest.fixture
def gesture_module():
    """
    Container C 모듈을 매 테스트마다 깨끗한 세션 상태로 제공한다.
    세션 상태(EMA, 디바운스)가 테스트 간에 새면 결과가 순서 의존적이 된다.
    """
    sys.path.insert(0, os.path.join(REPO_ROOT, "container_c_gesture"))
    import importlib

    import container_c_gesture.main as motion

    importlib.reload(motion)
    motion.sessions.clear()
    yield motion
    motion.sessions.clear()


def stabilize(motion, session_id, landmarks, frames=3):
    """
    디바운스를 통과시켜 최종 확정 action을 얻는다.
    3프레임 다수결이므로 1프레임만으로는 상태가 전이되지 않는다(설계대로).
    """
    result = None
    for _ in range(frames):
        result = motion.compute_gesture_logic(session_id, landmarks)
    return result


@pytest.fixture
def stabilize_fn():
    """디바운스를 통과시키는 헬퍼를 픽스처로 제공한다."""
    return stabilize
