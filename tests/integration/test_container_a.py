"""
Container A 통합 테스트 (CI-4)

FastAPI TestClient 로 실제 앱을 띄운다. Container B는 모킹한다.
(B를 실제로 띄우면 MediaPipe 설치가 필요해 CI가 무거워진다)
"""

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

pytestmark = pytest.mark.integration


@pytest.fixture
def app_module():
    """Container A 모듈을 깨끗한 상태로 로드한다."""
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


# ---------------------------------------------------------------------------
# 엔드포인트
# ---------------------------------------------------------------------------
def test_health_endpoint_exists(client):
    """
    A에 /health 가 있어야 한다 (P2-5 회귀 방지).
    고도화 이전 실측: HTTP 404. compose healthcheck를 걸 수 없었다.
    """
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_api_info_returns_ip_and_port(client):
    resp = client.get("/api/info")
    assert resp.status_code == 200
    body = resp.json()
    assert "ip" in body and "port" in body
    assert isinstance(body["port"], int)


def test_api_config_exposes_stream_settings(client):
    """프론트엔드가 스트리밍 파라미터를 서버에서 받아갈 수 있어야 한다 (P1-4)."""
    resp = client.get("/api/config")
    assert resp.status_code == 200
    stream = resp.json()["stream"]
    for key in ("width", "height", "jpeg_quality", "interval_ms", "max_inflight_frames"):
        assert key in stream, f"stream.{key} 누락"


def test_qr_returns_valid_png(client):
    resp = client.get("/api/qr/testsession")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n", "유효한 PNG 시그니처가 아니다"


def test_qr_honors_host_override(client):
    """host 파라미터로 QR 대상 주소를 지정할 수 있어야 한다."""
    resp = client.get("/api/qr/s1", params={"host": "10.1.2.3"})
    assert resp.status_code == 200


def test_pages_render(client):
    for path in ("/", "/mobile"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert len(resp.content) > 1000, f"{path} 페이지가 비어 있다"


# ---------------------------------------------------------------------------
# WebSocket 세션 관리
# ---------------------------------------------------------------------------
def test_pc_and_mobile_session_matching(client, app_module):
    """PC와 모바일이 같은 세션으로 매칭되고 STATUS가 오가는지."""
    with client.websocket_connect("/ws/pc/match1") as pc:
        assert "match1" in app_module.rooms
        assert app_module.rooms["match1"]["pc_ws"] is not None

        with client.websocket_connect("/ws/mobile/match1"):
            message = pc.receive_json()
            assert message["type"] == "STATUS"
            assert message["mobile_connected"] is True


def test_room_is_released_when_both_disconnect(client, app_module):
    """
    양쪽이 끊기면 방 자체가 삭제되어야 한다 (P2-7 회귀 방지).

    고도화 이전: 값만 None 으로 바꾸고 키는 영구 잔류.
    세션 ID가 매번 새로 생성되므로 새로고침마다 죽은 엔트리가 누적됐다.
    """
    with client.websocket_connect("/ws/pc/leak1"):
        assert "leak1" in app_module.rooms
    assert "leak1" not in app_module.rooms, "방이 정리되지 않아 메모리가 샌다"


def test_many_sessions_do_not_accumulate(client, app_module):
    """세션을 여러 번 열고 닫아도 방이 쌓이지 않아야 한다."""
    for i in range(20):
        with client.websocket_connect(f"/ws/pc/churn_{i}"):
            pass
    assert len(app_module.rooms) == 0, f"방 {len(app_module.rooms)}개가 남았다"


def test_room_survives_while_one_side_connected(client, app_module):
    """한쪽만 남아 있으면 방은 유지되어야 한다."""
    with client.websocket_connect("/ws/pc/half1"):
        with client.websocket_connect("/ws/mobile/half1"):
            pass
        assert "half1" in app_module.rooms, "PC가 아직 붙어 있으면 방을 지우면 안 된다"
