"""
Container C — 6대 제스처 규칙 회귀 테스트 (CI-2)

목적: 고도화 이전에 수동으로 확인했던 §1.4 베이스라인 동작을 자동화한다.
이 테스트가 통과하는 한, 어떤 리팩터링을 해도 제스처 판별 결과는 보존된다.
"""

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 6대 제스처 정상 케이스 (§1.3 표와 1:1 대응)
# ---------------------------------------------------------------------------
GESTURE_CASES = [
    ("DRAW",     dict(index=True),                                     "검지만 폄"),
    ("ERASE",    dict(index=True, middle=True),                        "검지+중지 (브이)"),
    ("ZOOM_IN",  dict(thumb_x=0.80, thumb_y=0.20),                     "주먹+엄지만 폄"),
    ("ZOOM_OUT", dict(pinky=True),                                     "주먹+새끼만 폄"),
    ("PAN",      dict(thumb_x=0.50, thumb_y=0.55),                     "완전한 주먹"),
    ("HOVER",    dict(index=True, middle=True, ring=True, pinky=True), "다섯 손가락 폄"),
]


@pytest.mark.parametrize("expected,kwargs,label", GESTURE_CASES)
def test_six_gestures(gesture_module, hand, stabilize_fn, expected, kwargs, label):
    """6대 제스처가 설계대로 판별되는지."""
    result = stabilize_fn(gesture_module, f"sess_{expected}", hand(**kwargs))
    assert result["action"] == expected, f"{label} → {expected} 이어야 함"
    assert result["detected"] is True


def test_zoom_delta_sign(gesture_module, hand, stabilize_fn):
    """확대는 양수, 축소는 음수 delta 여야 한다."""
    zoom_in = stabilize_fn(gesture_module, "z1", hand(thumb_x=0.80, thumb_y=0.20))
    zoom_out = stabilize_fn(gesture_module, "z2", hand(pinky=True))
    assert zoom_in["delta"] > 0
    assert zoom_out["delta"] < 0
    assert zoom_in["delta"] == -zoom_out["delta"], "확대/축소 폭은 대칭이어야 한다"


# ---------------------------------------------------------------------------
# 경계값 — 설정으로 뺀 임계값이 실제로 판정을 가르는지
# ---------------------------------------------------------------------------
def test_erase_height_diff_boundary(gesture_module, hand, stabilize_fn):
    """
    ERASE는 검지·중지 높이차가 임계값 미만일 때만 성립한다.
    (두 손가락이 크게 벌어진 경우를 지우개로 오인하지 않기 위함)
    """
    threshold = gesture_module.ERASE_HEIGHT_DIFF

    tight = stabilize_fn(
        gesture_module, "e_tight",
        hand(index=True, middle=True, index_y=0.30, middle_y=0.30),
    )
    assert tight["action"] == "ERASE", "높이차 0 → ERASE 여야 한다"

    loose = stabilize_fn(
        gesture_module, "e_loose",
        hand(index=True, middle=True, index_y=0.20, middle_y=0.20 + threshold + 0.05),
    )
    assert loose["action"] != "ERASE", "임계값 초과 → ERASE 가 아니어야 한다"


def test_thumb_open_threshold_governs_zoom_in(gesture_module, hand, stabilize_fn):
    """엄지 폄 판정 거리가 ZOOM_IN 성립을 가른다."""
    far = stabilize_fn(gesture_module, "t_far", hand(thumb_x=0.85, thumb_y=0.15))
    assert far["action"] == "ZOOM_IN", "손바닥에서 먼 엄지 → 폄 → ZOOM_IN"

    near = stabilize_fn(gesture_module, "t_near", hand(thumb_x=0.50, thumb_y=0.52))
    assert near["action"] == "PAN", "손바닥에 붙은 엄지 → 접힘 → PAN(주먹)"


# ---------------------------------------------------------------------------
# Pillar 1 — 설정 주입 검증
# ---------------------------------------------------------------------------
CONFIG_CONSTANTS = [
    "THUMB_OPEN_PALM_DIST", "THUMB_OPEN_INDEX_BASE_DIST",
    "THUMB_FOLDED_DIST", "ERASE_HEIGHT_DIFF", "ZOOM_STEP",
    "EMA_DEADZONE_DIST", "EMA_SLOW_DIST",
    "EMA_ALPHA_MICRO", "EMA_ALPHA_PRECISE", "EMA_ALPHA_FAST",
    "DEBOUNCE_WINDOW", "DEBOUNCE_MAJORITY", "INSTANT_PEN_UP", "SESSION_TTL_S",
]


@pytest.mark.parametrize("name", CONFIG_CONSTANTS)
def test_thresholds_come_from_config(gesture_module, name):
    """임계값이 설정에서 주입되는지. 코드에 리터럴이면 이 상수가 없다."""
    assert hasattr(gesture_module, name), f"{name} 이 설정에서 주입되지 않았다"
    assert getattr(gesture_module, name) is not None


BASELINE_VALUES = {
    "THUMB_OPEN_PALM_DIST": 0.15,
    "THUMB_OPEN_INDEX_BASE_DIST": 0.12,
    "THUMB_FOLDED_DIST": 0.13,
    "ERASE_HEIGHT_DIFF": 0.12,
    "ZOOM_STEP": 0.008,
    "EMA_DEADZONE_DIST": 0.001,
    "EMA_SLOW_DIST": 0.05,
    "EMA_ALPHA_MICRO": 0.35,
    "EMA_ALPHA_PRECISE": 0.50,
    "EMA_ALPHA_FAST": 0.85,
    "DEBOUNCE_WINDOW": 3,
    "DEBOUNCE_MAJORITY": 2,
    "SESSION_TTL_S": 600,
}


@pytest.mark.parametrize("name,expected", sorted(BASELINE_VALUES.items()))
def test_baseline_default_values_preserved(gesture_module, name, expected):
    """
    설정 기본값이 고도화 이전 하드코딩 값과 정확히 동일한지.
    이 값이 바뀌면 사용자 체감 동작이 달라진다 (베이스라인 보존 원칙).
    """
    assert getattr(gesture_module, name) == expected


# ---------------------------------------------------------------------------
# 엣지케이스 — 방어 로직
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad_input,label", [
    ([], "빈 배열"),
    ([{"x": 0.5, "y": 0.5}] * 5, "랜드마크 5개 (부족)"),
    ([{"x": 0.5, "y": 0.5}] * 20, "랜드마크 20개 (1개 부족)"),
])
def test_insufficient_landmarks_returns_neutral(gesture_module, bad_input, label):
    """불완전한 입력에도 예외 없이 중립 결과를 돌려줘야 한다."""
    result = gesture_module.compute_gesture_logic("edge", bad_input)
    assert result["action"] == "NONE", label
    assert result["detected"] is False
    assert result["x"] == 0.5 and result["y"] == 0.5, "중립 좌표가 보장되어야 한다"


def test_never_returns_null_coordinates(gesture_module, hand):
    """
    좌표가 None으로 새지 않는지 (P2-6 회귀 방지).
    수정 전에는 PC가 {"x": null, "y": null} 을 수신했다.
    """
    for landmarks in ([], hand(index=True), [[0.5, 0.5]] * 21):
        result = gesture_module.compute_gesture_logic("null_check", landmarks)
        assert result["x"] is not None and result["y"] is not None
        assert isinstance(result["x"], float) and isinstance(result["y"], float)


# ---------------------------------------------------------------------------
# 입력 포맷 호환 — parse_landmarks 의 3가지 분기
# ---------------------------------------------------------------------------
def test_accepts_dict_list_and_object_formats(gesture_module):
    """dict / list / 속성 객체 세 가지 입력 포맷을 모두 받아야 한다."""
    class LM:
        def __init__(self, x, y, z=0.0):
            self.x, self.y, self.z = x, y, z

    payloads = {
        "dict": [{"x": 0.5, "y": 0.5, "z": 0.0}] * 21,
        "list": [[0.5, 0.5]] * 21,
        "object": [LM(0.5, 0.5)] * 21,
    }
    for label, payload in payloads.items():
        parsed = gesture_module.parse_landmarks(payload)
        assert len(parsed) == 21, f"{label} 포맷 파싱 실패"
        assert parsed[0].x == 0.5


def test_session_isolation(gesture_module, hand, stabilize_fn):
    """한 세션의 상태가 다른 세션에 영향을 주면 안 된다."""
    stabilize_fn(gesture_module, "user_a", hand(index=True))
    result_b = stabilize_fn(gesture_module, "user_b", hand(pinky=True))

    assert result_b["action"] == "ZOOM_OUT"
    assert gesture_module.sessions["user_a"].current_stable_action == "DRAW"
    assert gesture_module.sessions["user_b"].current_stable_action == "ZOOM_OUT"
