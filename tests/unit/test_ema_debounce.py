"""
Container C — EMA 손떨림 보정 및 디바운스 필터 테스트 (CI-3)

이 파일의 핵심은 test_asymmetric_instant_pen_up 이다.
비대칭 컷오프는 "버그처럼 보이는 의도된 설계"이므로, 나중에 누군가
"일관성 있게" 고치려는 시도를 테스트로 차단한다.
"""

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def draw_hand(hand):
    """검지만 편 손 = DRAW."""
    return hand(index=True)


@pytest.fixture
def hover_hand(hand):
    """다섯 손가락 편 손 = HOVER."""
    return hand(index=True, middle=True, ring=True, pinky=True)


# ---------------------------------------------------------------------------
# 디바운스 (슬라이딩 윈도우 다수결)
# ---------------------------------------------------------------------------
def test_single_frame_does_not_flip_state(gesture_module, draw_hand):
    """1프레임만으로는 상태가 전이되지 않는다 (노이즈 억제)."""
    first = gesture_module.compute_gesture_logic("d1", draw_hand)
    assert first["action"] == "HOVER", "초기 상태(HOVER)가 유지되어야 한다"


def test_majority_vote_confirms_state(gesture_module, draw_hand):
    """다수결 임계(2표)에 도달하면 상태가 확정된다."""
    gesture_module.compute_gesture_logic("d2", draw_hand)
    second = gesture_module.compute_gesture_logic("d2", draw_hand)
    assert second["action"] == "DRAW", "2프레임째에 확정되어야 한다"


def test_asymmetric_instant_pen_up(gesture_module, stabilize_fn, draw_hand, hover_hand):
    """
    비대칭 무지연 펜-업 컷오프 — 의도된 설계다.

      DRAW → 비DRAW : 다수결을 건너뛰고 즉시 전환 (획 끝 '삐침' 방지)
      비DRAW → DRAW : 다수결을 정상적으로 거침 (오검출 방지)

    이 비대칭을 '일관성 있게' 고치면 드로잉 품질이 나빠진다.
    """
    sid = "pen_up"
    stabilize_fn(gesture_module, sid, draw_hand)
    assert gesture_module.sessions[sid].current_stable_action == "DRAW"

    instant = gesture_module.compute_gesture_logic(sid, hover_hand)
    assert instant["action"] == "HOVER", "DRAW 이탈은 지연 없이 즉시여야 한다"

    delayed = gesture_module.compute_gesture_logic(sid, draw_hand)
    assert delayed["action"] != "DRAW", "DRAW 진입은 다수결을 거쳐야 한다"


def test_instant_pen_up_is_configurable(gesture_module):
    """비대칭 컷오프가 설정 플래그로 노출되어 있는지 (Pillar 1)."""
    assert gesture_module.INSTANT_PEN_UP is True


# ---------------------------------------------------------------------------
# 속도 적응형 EMA (3단계 alpha)
# ---------------------------------------------------------------------------
def test_ema_alpha_tiers_are_ordered(gesture_module):
    """
    alpha는 '미세 < 정밀 < 고속' 순으로 커져야 한다.
    alpha가 클수록 새 좌표를 더 많이 반영한다 = 반응이 빠르고 덜 매끄럽다.
    """
    assert (
        gesture_module.EMA_ALPHA_MICRO
        < gesture_module.EMA_ALPHA_PRECISE
        < gesture_module.EMA_ALPHA_FAST
    )


def test_ema_deadzone_below_slow_threshold(gesture_module):
    """속도 구간 경계도 순서가 맞아야 한다."""
    assert gesture_module.EMA_DEADZONE_DIST < gesture_module.EMA_SLOW_DIST


def test_first_frame_has_no_smoothing(gesture_module, hand):
    """
    첫 프레임은 이전 값이 없으므로 EMA를 적용하지 않고 원좌표를 그대로 쓴다.
    (여기서 보정을 걸면 커서가 화면 중앙에서 끌려오는 현상이 생긴다)
    """
    landmarks = hand(index=True)
    landmarks[8] = {"x": 0.8, "y": 0.2, "z": 0.0}
    result = gesture_module.compute_gesture_logic("ema_first", landmarks)
    assert result["x"] == pytest.approx(0.8, abs=1e-4)
    assert result["y"] == pytest.approx(0.2, abs=1e-4)


def test_ema_smooths_toward_new_position(gesture_module, hand):
    """EMA는 새 좌표 쪽으로 이동하되 즉시 도달하지는 않는다."""
    sid = "ema_move"
    # y는 0.3 을 유지해야 검지가 '펴짐'으로 판정된다 (tip.y < pip.y = 0.5).
    # 0.5 로 두면 손가락이 접힌 것으로 해석되어 PAN 으로 빠지고,
    # 좌표가 손바닥 중심(0.5)으로 고정되어 테스트가 무의미해진다.
    start = hand(index=True)
    start[8] = {"x": 0.2, "y": 0.3, "z": 0.0}
    gesture_module.compute_gesture_logic(sid, start)

    moved = hand(index=True)
    moved[8] = {"x": 0.9, "y": 0.3, "z": 0.0}
    result = gesture_module.compute_gesture_logic(sid, moved)

    assert 0.2 < result["x"] < 0.9, "순간이동하거나 제자리에 있으면 안 된다"


def test_ema_state_is_per_session(gesture_module, hand):
    """EMA 상태가 세션별로 분리되는지."""
    a = hand(index=True)
    a[8] = {"x": 0.1, "y": 0.3, "z": 0.0}
    b = hand(index=True)
    b[8] = {"x": 0.9, "y": 0.3, "z": 0.0}

    gesture_module.compute_gesture_logic("ema_a", a)
    gesture_module.compute_gesture_logic("ema_b", b)

    assert gesture_module.sessions["ema_a"].smooth_x == pytest.approx(0.1, abs=1e-4)
    assert gesture_module.sessions["ema_b"].smooth_x == pytest.approx(0.9, abs=1e-4)


# ---------------------------------------------------------------------------
# PAN 상태 관리
# ---------------------------------------------------------------------------
def test_pan_delta_requires_previous_frame(gesture_module, hand):
    """PAN 첫 프레임은 이동량이 0이어야 한다 (튀는 현상 방지)."""
    fist = hand(thumb_x=0.50, thumb_y=0.55)
    first = gesture_module.compute_gesture_logic("pan1", fist)
    assert first["pan_dx"] == 0.0 and first["pan_dy"] == 0.0


def test_pan_state_resets_on_other_gestures(gesture_module, hand, stabilize_fn, draw_hand):
    """PAN이 아닌 제스처로 바뀌면 이전 위치가 초기화되어야 한다."""
    sid = "pan2"
    stabilize_fn(gesture_module, sid, hand(thumb_x=0.50, thumb_y=0.55))
    gesture_module.compute_gesture_logic(sid, draw_hand)
    assert gesture_module.sessions[sid].prev_pan_x is None
