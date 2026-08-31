"""
4대 축 자체를 검증하는 회귀 테스트 (CI-5)

청사진의 핵심 요구:
    "코드가 변경될 때마다 이 실무적 기준이 자동으로 검증되고 유지되도록 만드는 것,
     그것이 CI/CD 파이프라인의 핵심 역할입니다."

이 파일은 고도화로 제거한 안티패턴이 다시 기어들어오는 것을 자동으로 막는다.
사람의 코드리뷰에 의존하지 않는다.
"""

import ast
import os
import re

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.pillars]

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SOURCE_DIRS = ["container_a_web", "container_b_vision", "container_c_gesture", "common"]
PY_EXCLUDE = {"__pycache__"}


def _python_files():
    for directory in SOURCE_DIRS:
        base = os.path.join(REPO_ROOT, directory)
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in PY_EXCLUDE]
            for name in files:
                if name.endswith(".py"):
                    yield os.path.join(root, name)


def _html_files():
    static = os.path.join(REPO_ROOT, "container_a_web", "static")
    for name in os.listdir(static):
        if name.endswith(".html"):
            yield os.path.join(static, name)


def _strip_py_comments(source: str) -> str:
    """주석은 검사 대상이 아니다. '이전에는 X였다' 같은 설명을 위반으로 잡으면 안 된다."""
    return "\n".join(line.split("#", 1)[0] for line in source.splitlines())


def _strip_js_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return "\n".join(line.split("//", 1)[0] for line in source.splitlines())


def _rel(path: str) -> str:
    return os.path.relpath(path, REPO_ROOT)


# ===========================================================================
# Pillar 1 — 파라미터화
# ===========================================================================
IP_LITERAL = re.compile(r"\b(?:192\.168|10\.\d+|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+\b")


def test_no_hardcoded_ip_literals():
    """
    소스에 사설 IP 리터럴이 없어야 한다.

    고도화 이전: '192.168.55.208' 이 main.py, pc.html, docker-compose.yml 3곳에 중복.
    다른 네트워크로 옮기면 QR이 죽는 구조였다.
    """
    violations = []
    for path in list(_python_files()):
        body = _strip_py_comments(open(path, encoding="utf-8").read())
        for match in IP_LITERAL.finditer(body):
            violations.append(f"{_rel(path)}: {match.group(0)}")
    for path in _html_files():
        body = _strip_js_comments(open(path, encoding="utf-8").read())
        for match in IP_LITERAL.finditer(body):
            violations.append(f"{_rel(path)}: {match.group(0)}")

    assert not violations, "하드코딩된 IP 발견:\n  " + "\n  ".join(violations)


PORT_LITERAL = re.compile(r"[:\s=]\b(8443|8001|8002)\b")


def test_no_hardcoded_port_literals_in_app_code():
    """
    애플리케이션 코드에 서비스 포트 리터럴이 없어야 한다.
    (Dockerfile의 EXPOSE, compose의 포트 매핑, 헬스체크 스크립트는 인프라 선언이므로 제외)
    """
    violations = []
    for path in _python_files():
        if "scripts" in path:
            continue
        body = _strip_py_comments(open(path, encoding="utf-8").read())
        for match in PORT_LITERAL.finditer(body):
            violations.append(f"{_rel(path)}: {match.group(1)}")
    for path in _html_files():
        body = _strip_js_comments(open(path, encoding="utf-8").read())
        for match in PORT_LITERAL.finditer(body):
            violations.append(f"{_rel(path)}: {match.group(1)}")

    assert not violations, "하드코딩된 포트 발견:\n  " + "\n  ".join(violations)


def test_config_files_exist_and_load():
    """설정 파일이 존재하고 필수 키를 갖추고 있어야 한다."""
    from common.config import load_config

    cfg = load_config(reload=True)
    for key in [
        "network.web_port", "network.vision_port", "network.gesture_port",
        "http.read_timeout_s", "http.retry.max_attempts",
        "stream.width", "stream.interval_ms", "stream.max_inflight_frames",
        "vision.num_hands", "vision.detector_pool_size", "vision.max_frame_bytes",
        "gesture.thresholds.thumb_open_palm_dist", "gesture.ema.alpha_fast",
        "gesture.debounce.window", "gesture.session.ttl_s",
        "logging.level", "logging.frame_sample_rate",
    ]:
        assert cfg.require(key) is not None, f"필수 설정 키 누락: {key}"


def test_missing_config_key_fails_loudly():
    """설정 키가 없으면 조용히 기본값으로 넘어가지 않고 명시적으로 실패해야 한다."""
    from common.config import ConfigError, load_config

    cfg = load_config()
    with pytest.raises(ConfigError):
        cfg.require("gesture.does_not_exist")


def test_env_override_works():
    """환경변수로 설정을 덮어쓸 수 있어야 한다 (Dev/Prod 분리의 전제)."""
    from common.config import load_config

    os.environ["AIRCANVAS__GESTURE__EMA__ALPHA_FAST"] = "0.99"
    try:
        cfg = load_config(reload=True)
        assert cfg.get("gesture.ema.alpha_fast") == 0.99
    finally:
        os.environ.pop("AIRCANVAS__GESTURE__EMA__ALPHA_FAST", None)
        load_config(reload=True)


# ===========================================================================
# Pillar 2 — 예외 처리 및 안정성
# ===========================================================================
def test_no_silent_exception_swallowing():
    """
    `except ...: pass` 형태의 침묵 예외가 없어야 한다.

    고도화 이전: container_a_web/main.py 의 `except Exception: pass` 한 줄이
    B 다운, 타임아웃, JSON 파싱 실패, 소켓 단절을 전부 무음 처리했다.
    사용자는 "그림이 안 그려진다"만 알고 로그에는 흔적조차 없었다.
    """
    violations = []
    for path in _python_files():
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            body = [n for n in node.body if not isinstance(n, ast.Expr)
                    or not isinstance(getattr(n, "value", None), ast.Constant)]
            if len(body) == 1 and isinstance(body[0], ast.Pass):
                violations.append(f"{_rel(path)}:{node.lineno}")

    assert not violations, (
        "침묵 예외(except: pass) 발견:\n  " + "\n  ".join(violations)
    )


def test_retry_and_circuit_breaker_available():
    """재시도와 서킷 브레이커가 실제로 구현되어 있어야 한다."""
    from common.http_client import CircuitState, ResilientClient

    assert hasattr(ResilientClient, "post_json")
    circuit = CircuitState(failure_threshold=2, recovery_timeout_s=60)
    assert circuit.state == "CLOSED"
    circuit.record_failure()
    assert circuit.state == "CLOSED", "임계 미달에서는 열리면 안 된다"
    assert circuit.record_failure() == "opened"
    assert circuit.state == "OPEN"
    assert circuit.allow() is False, "OPEN 상태에서는 요청을 차단해야 한다"


def test_circuit_recovers_to_half_open():
    """서킷은 일정 시간 후 탐침을 허용해야 한다 (영구 차단 방지)."""
    from common.http_client import CircuitState

    circuit = CircuitState(failure_threshold=1, recovery_timeout_s=0.0)
    circuit.record_failure()
    assert circuit.state == "HALF_OPEN"
    assert circuit.allow() is True, "탐침 요청 1개는 통과해야 한다"
    assert circuit.allow() is False, "탐침 중 추가 요청은 막아야 한다"
    circuit.record_success()
    assert circuit.state == "CLOSED"


def test_gesture_result_never_yields_null_coordinates():
    """
    응답 스키마가 좌표 기본값을 보장해야 한다 (P2-6 회귀 방지).
    수정 전 실측: PC가 {"x": null, "y": null} 을 수신했다.
    """
    from common.schemas import GestureResult

    cases = [
        None,
        {},
        {"action": "NONE"},
        {"action": "NONE", "x": None, "y": None},
        {"x": "not-a-number", "y": []},
    ]
    for raw in cases:
        result = GestureResult.from_upstream(raw, session_id="s")
        assert result.x is not None and result.y is not None
        assert isinstance(result.x, float) and isinstance(result.y, float)


def test_degraded_response_keeps_pipeline_alive():
    """
    상류가 죽어도 성공 응답 + DEGRADED 상태를 돌려줘야 한다.
    (전체 정지가 아니라 기능 축소)
    """
    from common.schemas import AnalyzeResponse, HealthState

    landmarks = [{"x": 0.5, "y": 0.5, "z": 0.0}] * 21
    degraded = AnalyzeResponse.degraded("TIMEOUT", landmarks)

    assert degraded.success is True, "파이프라인 자체는 살아 있어야 한다"
    assert degraded.health == HealthState.DEGRADED
    assert degraded.action == "HOVER", "랜드마크가 있으면 커서는 유지되어야 한다"
    assert len(degraded.landmarks) == 21


# ===========================================================================
# Pillar 3 — 성능 및 메모리
# ===========================================================================
def test_no_global_shared_detector():
    """
    전역 detector 공유가 사라졌는지 (P3-2 회귀 방지).

    고도화 이전: 전역 인스턴스 1개를 모든 세션이 공유 + VIDEO 모드
    → 동시 접속자 2명이면 서로의 손 추적 상태를 오염시켰다.
    """
    source = open(
        os.path.join(REPO_ROOT, "container_b_vision", "main.py"), encoding="utf-8"
    ).read()
    body = _strip_py_comments(source)
    assert "hands_detector = mp_vision.HandLandmarker.create_from_options" not in body
    assert "detector_pool" in body, "세션별 detector 풀을 사용해야 한다"


def test_no_wallclock_timestamp_for_video_mode():
    """
    detect_for_video 에 벽시계 타임스탬프를 쓰지 않는지 (P3-2 회귀 방지).
    time.time() 기반은 단조 증가를 보장하지 못해 같은 ms에 두 프레임이 오면 예외가 난다.
    """
    source = open(
        os.path.join(REPO_ROOT, "container_b_vision", "main.py"), encoding="utf-8"
    ).read()
    body = _strip_py_comments(source)
    assert "int(time.time() * 1000)" not in body


def test_detector_pool_isolates_sessions():
    """세션마다 다른 detector 인스턴스를 받아야 한다."""
    from common.detector_pool import DetectorPool

    created = []

    class FakeDetector:
        def __init__(self, index):
            self.index = index

        def close(self):
            pass

    def factory():
        detector = FakeDetector(len(created))
        created.append(detector)
        return detector

    pool = DetectorPool(factory, max_size=4, idle_ttl_s=60)
    with pool.acquire("user_a") as (det_a, _):
        pass
    with pool.acquire("user_b") as (det_b, _):
        pass

    assert det_a is not det_b, "세션이 다르면 detector도 달라야 한다"
    assert pool.stats()["pool_size"] == 2


def test_detector_pool_timestamps_are_monotonic():
    """같은 세션의 타임스탬프는 항상 증가해야 한다 (VIDEO 모드 요구사항)."""
    from common.detector_pool import DetectorPool

    class FakeDetector:
        def close(self):
            pass

    pool = DetectorPool(FakeDetector, max_size=2, idle_ttl_s=60)
    stamps = []
    for _ in range(100):
        with pool.acquire("same_session") as (_, ts):
            stamps.append(ts)

    assert stamps == sorted(stamps), "타임스탬프가 역행했다"
    assert len(set(stamps)) == len(stamps), "타임스탬프가 중복됐다"


def test_detector_pool_evicts_over_capacity():
    """풀이 가득 차면 LRU로 회수해 메모리가 무한 증가하지 않아야 한다."""
    from common.detector_pool import DetectorPool

    closed = []

    class FakeDetector:
        def close(self):
            closed.append(1)

    pool = DetectorPool(FakeDetector, max_size=2, idle_ttl_s=60)
    for i in range(5):
        with pool.acquire(f"session_{i}"):
            pass

    assert pool.stats()["pool_size"] <= 2
    assert len(closed) >= 3, "초과분이 회수되지 않았다"


def test_inference_is_offloaded_from_event_loop():
    """
    CPU 바운드 추론이 스레드풀로 오프로딩되는지 (P3-1 회귀 방지).
    async 핸들러 안에서 직접 호출하면 이벤트 루프 전체가 블로킹된다.
    """
    source = open(
        os.path.join(REPO_ROOT, "container_b_vision", "main.py"), encoding="utf-8"
    ).read()
    body = _strip_py_comments(source)
    assert "run_in_threadpool(extract_landmarks" in body


def test_frontend_has_backpressure():
    """
    프론트엔드가 백프레셔 없이 무조건 전송하지 않는지 (P3-3 회귀 방지).
    서버 상태와 무관하게 33ms마다 쏘면 송신 버퍼에 적체되어 지연이 누적된다.
    """
    source = open(
        os.path.join(REPO_ROOT, "container_a_web", "static", "mobile.html"),
        encoding="utf-8",
    ).read()
    body = _strip_js_comments(source)
    assert "inflight" in body, "in-flight 프레임 카운터가 없다"
    assert "bufferedAmount" in body, "송신 버퍼 감시가 없다"


# ===========================================================================
# Pillar 4 — 로깅
# ===========================================================================
def test_no_print_statements_in_services():
    """
    서비스 코드에 print() 가 없어야 한다.

    고도화 이전: Container A는 print() 4곳이 전부였고, 그마저도 stdout 버퍼링 때문에
    docker logs 에 나타나지 않았다(실측 확인). 즉 사실상 무로깅이었다.
    """
    violations = []
    for path in _python_files():
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                violations.append(f"{_rel(path)}:{node.lineno}")

    assert not violations, "print() 발견:\n  " + "\n  ".join(violations)


def test_all_containers_use_shared_logger():
    """3개 컨테이너가 모두 공통 구조화 로거를 사용해야 한다."""
    for directory in ["container_a_web", "container_b_vision", "container_c_gesture"]:
        source = open(
            os.path.join(REPO_ROOT, directory, "main.py"), encoding="utf-8"
        ).read()
        assert "from common.logging_setup import setup_logging" in source, directory
        assert "setup_logging(" in source, directory


def test_log_level_actually_filters(capsys):
    """
    level 인자가 실제 로그 레벨에 반영되는지.

    고도화 이전 Container C의 결함: level 인자를 받고도 항상 logger.info() 로 기록해
    JSON 안의 level 필드는 문자열일 뿐 필터링이 동작하지 않았다.
    """
    from common.logging_setup import setup_logging

    log = setup_logging(
        "TEST_FILTER",
        settings={"level": "WARNING", "format": "json", "dir": ""},
        force=True,
    )
    log.debug("should_not_appear")
    log.info("should_not_appear_either")
    log.warning("should_appear")

    output = capsys.readouterr().out
    assert "should_not_appear" not in output
    assert "should_appear" in output


def test_exception_logging_includes_traceback_and_context():
    """
    장애 로그에 traceback 과 컨텍스트(session_id)가 포함되는지 (P4-7).
    """
    import io
    import json
    import logging as _logging

    from common.logging_setup import StructuredJsonFormatter, setup_logging

    buffer = io.StringIO()
    log = setup_logging(
        "TEST_TB",
        settings={"level": "DEBUG", "format": "json", "dir": ""},
        force=True,
    )
    handler = _logging.StreamHandler(buffer)
    handler.setFormatter(StructuredJsonFormatter("TEST_TB"))
    log._logger.handlers = [handler]

    try:
        raise ValueError("의도된 테스트 예외")
    except ValueError:
        log.exception("boom", session_id="sess-42", detail={"frame": 7})

    record = json.loads(buffer.getvalue().strip().splitlines()[-1])
    assert record["level"] == "ERROR"
    assert record["session_id"] == "sess-42"
    assert record["error"]["type"] == "ValueError"
    assert "Traceback" in record["error"]["traceback"]
    assert record["detail"]["frame"] == 7


def test_frame_logs_are_sampled():
    """
    프레임 단위 로그가 샘플링되는지 (P4-5).
    30fps × 세션수를 전량 기록하면 디스크와 성능을 동시에 잡아먹는다.
    """
    import io
    import json
    import logging as _logging

    from common.logging_setup import StructuredJsonFormatter, setup_logging

    buffer = io.StringIO()
    log = setup_logging(
        "TEST_SAMPLE",
        settings={"level": "DEBUG", "format": "json", "dir": "", "frame_sample_rate": 10},
        force=True,
    )
    handler = _logging.StreamHandler(buffer)
    handler.setFormatter(StructuredJsonFormatter("TEST_SAMPLE"))
    log._logger.handlers = [handler]

    for i in range(100):
        log.sampled("frame_event", session_id="s", detail={"i": i})

    lines = [json.loads(line) for line in buffer.getvalue().strip().splitlines()]
    assert len(lines) == 10, f"100회 호출 → 10건이어야 하는데 {len(lines)}건"
    assert lines[0]["detail"]["sampled_every"] == 10
    assert lines[0]["detail"]["occurrence"] == 1, "첫 발생은 즉시 기록되어야 한다"


def test_log_records_are_valid_json_with_required_fields():
    """모든 로그가 유효한 JSON이고 표준 필드를 갖는지."""
    import io
    import json
    import logging as _logging

    from common.logging_setup import StructuredJsonFormatter, setup_logging

    buffer = io.StringIO()
    log = setup_logging(
        "TEST_SCHEMA",
        settings={"level": "DEBUG", "format": "json", "dir": ""},
        force=True,
    )
    handler = _logging.StreamHandler(buffer)
    handler.setFormatter(StructuredJsonFormatter("TEST_SCHEMA"))
    log._logger.handlers = [handler]

    log.info("event_one", session_id="abc", detail={"k": "v"})
    log.warning("event_two", trace_id="abc-1")

    for line in buffer.getvalue().strip().splitlines():
        record = json.loads(line)
        for field in ("ts", "time", "container", "level", "event", "session_id"):
            assert field in record, f"필수 필드 누락: {field}"
