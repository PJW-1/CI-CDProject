"""
Graceful Degradation 통합 테스트 (CI-5)

"단 한 번의 에러로 24시간 도는 공장 생산 라인을 멈추게 할 수는 없다."

상류(B)가 죽거나 느려져도 A가 살아남고, 사용자에게 상태가 통지되는지 검증한다.
실제 컨테이너를 죽이는 대신 HTTP 계층을 모킹해 CI에서도 결정론적으로 재현한다.
"""

import os
import sys

import httpx
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

pytestmark = pytest.mark.integration


@pytest.fixture
def app_module():
    sys.path.insert(0, os.path.join(REPO_ROOT, "container_a_web"))
    import importlib

    import container_a_web.main as web

    importlib.reload(web)
    web.rooms.clear()
    yield web
    web.rooms.clear()


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient

    with TestClient(app_module.app) as test_client:
        yield test_client


BLANK_FRAME = "data:image/jpeg;base64,AAAA"


def _patch_vision(monkeypatch, app_module, behavior):
    """ResilientClient 가 쓰는 httpx post 를 지정한 동작으로 대체한다."""
    async def fake_post(self, url, **kwargs):
        return behavior(url, kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


# ---------------------------------------------------------------------------
# 상류 장애 시나리오
# ---------------------------------------------------------------------------
def test_survives_vision_unreachable(client, app_module, monkeypatch):
    """
    B가 완전히 죽어도 A는 살아남고 DEGRADED/DOWN 을 통지해야 한다.

    고도화 이전: `except Exception: pass` 로 무음 처리되어
    사용자는 "그림이 안 그려진다"만 알고 로그에도 흔적이 없었다.
    """
    def always_fail(url, kwargs):
        raise httpx.ConnectError("simulated: vision engine down")

    _patch_vision(monkeypatch, app_module, always_fail)

    with client.websocket_connect("/ws/mobile/down1") as mobile:
        mobile.send_text(BLANK_FRAME)
        messages = [mobile.receive_json() for _ in range(2)]

    kinds = {m["type"] for m in messages}
    assert "STATUS" in kinds, "장애 상태가 통지되지 않았다"
    assert "FEEDBACK" in kinds, "FEEDBACK 이 끊기면 클라이언트 백프레셔가 잠긴다"

    status = next(m for m in messages if m["type"] == "STATUS")
    assert status["health"] in ("DEGRADED", "DOWN")


def test_survives_vision_timeout(client, app_module, monkeypatch):
    """타임아웃도 조용히 삼켜지지 않고 상태로 드러나야 한다."""
    def always_timeout(url, kwargs):
        raise httpx.ReadTimeout("simulated: slow vision engine")

    _patch_vision(monkeypatch, app_module, always_timeout)

    with client.websocket_connect("/ws/mobile/slow1") as mobile:
        mobile.send_text(BLANK_FRAME)
        messages = [mobile.receive_json() for _ in range(2)]

    status = next(m for m in messages if m["type"] == "STATUS")
    assert status["health"] in ("DEGRADED", "DOWN")
    assert status["reason"], "장애 사유가 비어 있으면 진단이 불가능하다"


def test_bad_status_code_is_not_silently_ignored(client, app_module, monkeypatch):
    """
    200이 아닌 응답도 무시되면 안 된다.
    고도화 이전에는 else 분기 자체가 없어 조용히 넘어갔다.
    """
    def server_error(url, kwargs):
        return httpx.Response(500, text="internal error", request=httpx.Request("POST", url))

    _patch_vision(monkeypatch, app_module, server_error)

    with client.websocket_connect("/ws/mobile/bad1") as mobile:
        mobile.send_text(BLANK_FRAME)
        messages = [mobile.receive_json() for _ in range(2)]

    assert any(m["type"] == "STATUS" for m in messages)


def test_null_coordinates_never_reach_pc(client, app_module, monkeypatch):
    """
    상류가 x/y 없는 응답을 줘도 PC로 null 이 흘러가면 안 된다 (P2-6 회귀 방지).

    고도화 이전 실측 페이로드:
        {"type":"GESTURE","action":"NONE","x":null,"y":null,...}
    """
    def missing_coords(url, kwargs):
        return httpx.Response(
            200,
            json={"success": True, "action": "NONE", "landmarks": []},   # x, y 키 없음
            request=httpx.Request("POST", url),
        )

    _patch_vision(monkeypatch, app_module, missing_coords)

    with client.websocket_connect("/ws/pc/null1") as pc:
        with client.websocket_connect("/ws/mobile/null1") as mobile:
            pc.receive_json()          # STATUS(mobile_connected)
            mobile.send_text(BLANK_FRAME)
            gesture = pc.receive_json()

    assert gesture["type"] == "GESTURE"
    assert gesture["x"] is not None, "null 좌표가 PC로 새어나갔다"
    assert gesture["y"] is not None
    assert isinstance(gesture["x"], (int, float))


def test_recovers_when_upstream_returns(client, app_module, monkeypatch):
    """장애가 회복되면 자동으로 정상 상태로 돌아와야 한다."""
    state = {"failing": True}

    def flaky(url, kwargs):
        if state["failing"]:
            raise httpx.ConnectError("simulated outage")
        return httpx.Response(
            200,
            json={
                "success": True, "action": "DRAW", "x": 0.4, "y": 0.6,
                "delta": 0.0, "pan_dx": 0.0, "pan_dy": 0.0,
                "detected": True, "landmarks": [], "health": "OK",
            },
            request=httpx.Request("POST", url),
        )

    _patch_vision(monkeypatch, app_module, flaky)

    with client.websocket_connect("/ws/mobile/recover1") as mobile:
        mobile.send_text(BLANK_FRAME)
        first = [mobile.receive_json() for _ in range(2)]
        degraded = next(m for m in first if m["type"] == "STATUS")
        assert degraded["health"] in ("DEGRADED", "DOWN")

        state["failing"] = False
        mobile.send_text(BLANK_FRAME)
        second = [mobile.receive_json() for _ in range(2)]

    recovered = next((m for m in second if m["type"] == "STATUS"), None)
    assert recovered is not None, "회복이 통지되지 않았다"
    assert recovered["health"] == "OK"
