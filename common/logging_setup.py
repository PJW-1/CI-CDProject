"""
공통 구조화 로깅 모듈 (Pillar 4)

Container C의 log_event() 구현을 3개 컨테이너 공용으로 승격하면서
아래 결함을 함께 해결한다.

  - 기존 C는 level 인자를 받고도 항상 logger.info()로 기록해 레벨 필터링이 무의미했다.
    → 실제 로거 메서드에 매핑한다.
  - 컨테이너 이름이 "C"로 하드코딩되어 있었다.
    → 초기화 시 주입한다.
  - stdout 전용이라 컨테이너 재시작 시 로그가 소실됐다.
    → RotatingFileHandler를 선택적으로 추가한다.
  - traceback을 남길 방법이 없었다.
    → exception()이 자동 첨부한다.
  - 30fps 프레임 단위 로그를 그대로 남기면 초당 수십 줄이 쌓인다.
    → sampled()로 N회당 1회만 기록한다. 단, 에러는 절대 샘플링하지 않는다.

로그 레코드 표준 스키마
    ts          epoch milliseconds (기계 정렬용)
    time        ISO-8601 (사람이 읽는 용도)
    container   "A" | "B" | "C"
    level       DEBUG | INFO | WARNING | ERROR | CRITICAL
    event       snake_case 이벤트 이름 (문장이 아니라 식별자)
    session_id  세션 추적 키 (없으면 "-")
    trace_id    프레임 단위 추적 키 (선택)
    detail      이벤트별 부가 정보 dict
    error       예외 발생 시에만 존재: {type, message, traceback}

설정은 환경변수로 받는다. Pillar 1(설정 외부화) 작업 시 config 로더로 교체 예정이며,
그때도 이 모듈의 공개 API(setup_logging / get_logger)는 바뀌지 않는다.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import traceback as _traceback
from datetime import datetime, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 환경변수 기본값
# ---------------------------------------------------------------------------
_DEFAULTS = {
    "LOG_LEVEL": "INFO",
    "LOG_FORMAT": "json",          # json | text
    "LOG_DIR": "",                 # 비어 있으면 stdout 전용
    "LOG_MAX_BYTES": "10485760",   # 10MB
    "LOG_BACKUP_COUNT": "5",
    "LOG_FRAME_SAMPLE_RATE": "30", # 30fps 기준 초당 1회
}

# 표준 LogRecord 속성. 사용자가 추가한 필드만 골라내기 위해 사용한다.
_RESERVED_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
) | {"message", "asctime", "taskName"}


def _env(key: str) -> str:
    return os.environ.get(key, _DEFAULTS[key])


def _env_int(key: str) -> int:
    raw = _env(key)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(_DEFAULTS[key])


def _jsonable(value: Any) -> Any:
    """JSON 직렬화 불가 값이 로깅 자체를 실패시키지 않도록 방어한다."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


# ---------------------------------------------------------------------------
# 포매터
# ---------------------------------------------------------------------------
class StructuredJsonFormatter(logging.Formatter):
    """LogRecord를 표준 스키마 JSON 한 줄로 직렬화한다."""

    def __init__(self, container: str):
        super().__init__()
        self.container = container

    def format(self, record: logging.LogRecord) -> str:
        created_ms = int(record.created * 1000)
        payload = {
            "ts": created_ms,
            "time": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(timespec="milliseconds"),
            "container": getattr(record, "container", self.container),
            "level": record.levelname,
            "event": record.getMessage(),
            "session_id": getattr(record, "session_id", None) or "-",
        }

        trace_id = getattr(record, "trace_id", None)
        if trace_id:
            payload["trace_id"] = trace_id

        # detail: 명시적으로 넘긴 dict + 그 외 키워드 필드를 병합
        detail: dict[str, Any] = {}
        explicit = getattr(record, "detail", None)
        if isinstance(explicit, dict):
            detail.update(explicit)
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS:
                continue
            if key in ("container", "session_id", "trace_id", "detail"):
                continue
            detail[key] = value
        if detail:
            payload["detail"] = {k: _jsonable(v) for k, v in detail.items()}

        if record.exc_info:
            exc_type, exc_value, exc_tb = record.exc_info
            payload["error"] = {
                "type": exc_type.__name__ if exc_type else "Unknown",
                "message": str(exc_value),
                "traceback": "".join(
                    _traceback.format_exception(exc_type, exc_value, exc_tb)
                ),
            }

        return json.dumps(payload, ensure_ascii=False, default=repr)


class HumanReadableFormatter(logging.Formatter):
    """로컬 개발용. JSON보다 눈으로 따라가기 쉽다."""

    def __init__(self, container: str):
        super().__init__()
        self.container = container

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        container = getattr(record, "container", self.container)
        session = getattr(record, "session_id", None) or "-"

        detail: dict[str, Any] = {}
        explicit = getattr(record, "detail", None)
        if isinstance(explicit, dict):
            detail.update(explicit)
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS:
                continue
            if key in ("container", "session_id", "trace_id", "detail"):
                continue
            detail[key] = value

        line = (
            f"[{stamp}] [{record.levelname:<7}] [{container}] "
            f"[{session}] {record.getMessage()}"
        )
        if detail:
            line += " " + " ".join(f"{k}={v!r}" for k, v in detail.items())
        if record.exc_info:
            line += "\n" + "".join(_traceback.format_exception(*record.exc_info)).rstrip()
        return line


# ---------------------------------------------------------------------------
# 로거 래퍼
# ---------------------------------------------------------------------------
class StructuredLogger:
    """
    구조화 로깅 진입점.

        log = setup_logging("A")
        log.info("pc_connected", session_id=sid)
        log.warning("vision_timeout", session_id=sid, timeout_s=1.5)
        log.exception("relay_failed", session_id=sid)      # traceback 자동 첨부
        log.sampled("frame_processed", session_id=sid)     # N회당 1회만 기록

    첫 인자는 항상 snake_case 이벤트 이름이다. 문장을 넣지 않는다.
    사람이 읽을 설명은 키워드 인자(detail)로 넘긴다.
    """

    def __init__(self, logger: logging.Logger, container: str, sample_rate: int):
        self._logger = logger
        self._container = container
        self._sample_rate = max(1, sample_rate)
        self._counters: dict[str, int] = {}
        self._lock = threading.Lock()

    # -- 내부 ---------------------------------------------------------------
    def _emit(
        self,
        level: int,
        event: str,
        session_id: Optional[str],
        trace_id: Optional[str],
        exc_info: Any,
        fields: dict[str, Any],
    ) -> None:
        extra = {
            "container": self._container,
            "session_id": session_id,
            "trace_id": trace_id,
        }
        # 예약어와 충돌하는 키는 LogRecord 생성 자체를 깨뜨리므로 detail로 밀어 넣는다.
        safe: dict[str, Any] = {}
        collided: dict[str, Any] = {}
        for key, value in fields.items():
            (collided if key in _RESERVED_ATTRS else safe)[key] = value
        if collided:
            safe.setdefault("detail", {})
            if isinstance(safe["detail"], dict):
                safe["detail"] = {**safe["detail"], **collided}
        extra.update(safe)

        self._logger.log(level, event, exc_info=exc_info, extra=extra, stacklevel=3)

    # -- 공개 API -----------------------------------------------------------
    def debug(self, event: str, *, session_id=None, trace_id=None, **fields) -> None:
        self._emit(logging.DEBUG, event, session_id, trace_id, None, fields)

    def info(self, event: str, *, session_id=None, trace_id=None, **fields) -> None:
        self._emit(logging.INFO, event, session_id, trace_id, None, fields)

    def warning(self, event: str, *, session_id=None, trace_id=None, **fields) -> None:
        self._emit(logging.WARNING, event, session_id, trace_id, None, fields)

    def error(self, event: str, *, session_id=None, trace_id=None, exc_info=None, **fields) -> None:
        self._emit(logging.ERROR, event, session_id, trace_id, exc_info, fields)

    def critical(self, event: str, *, session_id=None, trace_id=None, exc_info=None, **fields) -> None:
        self._emit(logging.CRITICAL, event, session_id, trace_id, exc_info, fields)

    def exception(self, event: str, *, session_id=None, trace_id=None, **fields) -> None:
        """except 블록 안에서 호출한다. 현재 예외의 traceback을 자동 첨부한다."""
        self._emit(logging.ERROR, event, session_id, trace_id, True, fields)

    def sampled(
        self,
        event: str,
        *,
        session_id=None,
        trace_id=None,
        level: int = logging.DEBUG,
        **fields,
    ) -> None:
        """
        프레임 단위 고빈도 이벤트용. 같은 event 이름 기준 N회당 1회만 기록한다.

        첫 발생은 항상 기록한다. 새로운 이벤트가 나타났다는 사실 자체가 정보이며,
        N회를 기다렸다가 남기면 저빈도 이벤트는 영영 보이지 않기 때문이다.
        이후로는 N회마다 1회씩 기록한다.

        기록될 때 detail.sampled_every 와 occurrence(누적 발생 횟수)를 함께 남겨
        "1건"이 실제로는 N건을 대표한다는 사실을 잃지 않게 한다.

        에러 레벨에는 사용하지 않는다. 장애는 전량 기록해야 한다.
        """
        with self._lock:
            count = self._counters.get(event, 0)
            self._counters[event] = count + 1
            should_log = (count % self._sample_rate) == 0

        if should_log:
            fields["sampled_every"] = self._sample_rate
            fields["occurrence"] = count + 1
            self._emit(level, event, session_id, trace_id, None, fields)

    def is_enabled_for(self, level: int) -> bool:
        """비싼 detail 계산을 건너뛰기 위한 가드."""
        return self._logger.isEnabledFor(level)

    @property
    def sample_rate(self) -> int:
        return self._sample_rate


# ---------------------------------------------------------------------------
# 초기화
# ---------------------------------------------------------------------------
_instances: dict[str, StructuredLogger] = {}
_setup_lock = threading.Lock()


def setup_logging(
    container: str,
    *,
    settings: Optional[Any] = None,
    force: bool = False,
) -> StructuredLogger:
    """
    컨테이너별 구조화 로거를 초기화한다. 애플리케이션 기동 시 1회 호출한다.

    Args:
        container: "A" | "B" | "C" — 모든 로그 레코드에 찍히는 출처 식별자
        settings: config 의 logging 섹션. 주면 이 값이 우선한다.
                  주지 않으면 환경변수로 동작한다(설정 파일 없이도 쓸 수 있도록).
        force: True면 기존 핸들러를 버리고 재구성 (테스트용)

    환경변수 (settings 미지정 시):
        LOG_LEVEL / LOG_FORMAT / LOG_DIR / LOG_MAX_BYTES
        LOG_BACKUP_COUNT / LOG_FRAME_SAMPLE_RATE
    """

    def _opt(name: str, env_key: str) -> str:
        if settings is not None:
            try:
                value = settings.get(name)
            except AttributeError:
                value = None
            if value is not None:
                return str(value)
        return _env(env_key)

    def _opt_int(name: str, env_key: str) -> int:
        try:
            return int(_opt(name, env_key))
        except (TypeError, ValueError):
            return int(_DEFAULTS[env_key])

    with _setup_lock:
        if container in _instances and not force:
            return _instances[container]

        level_name = _opt("level", "LOG_LEVEL").upper()
        level = getattr(logging, level_name, logging.INFO)

        logger = logging.getLogger(f"air_canvas.{container}")
        logger.setLevel(level)
        logger.propagate = False

        if force or logger.handlers:
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()

        if _opt("format", "LOG_FORMAT").lower() == "text":
            formatter: logging.Formatter = HumanReadableFormatter(container)
        else:
            formatter = StructuredJsonFormatter(container)

        # stdout — docker logs 호환. 항상 유지한다.
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

        # 파일 — 컨테이너 재시작 후에도 남는 사후 분석용
        log_dir = _opt("dir", "LOG_DIR").strip()
        file_error: Optional[str] = None
        if log_dir:
            try:
                os.makedirs(log_dir, exist_ok=True)
                file_handler = logging.handlers.RotatingFileHandler(
                    os.path.join(log_dir, f"container_{container.lower()}.log"),
                    maxBytes=_opt_int("max_bytes", "LOG_MAX_BYTES"),
                    backupCount=_opt_int("backup_count", "LOG_BACKUP_COUNT"),
                    encoding="utf-8",
                )
                file_handler.setFormatter(formatter)
                logger.addHandler(file_handler)
            except OSError as exc:
                # 파일 로깅 실패가 서비스를 죽여서는 안 된다. stdout은 이미 살아 있다.
                file_error = f"{type(exc).__name__}: {exc}"

        structured = StructuredLogger(
            logger, container, _opt_int("frame_sample_rate", "LOG_FRAME_SAMPLE_RATE")
        )
        _instances[container] = structured

        structured.info(
            "logging_initialized",
            detail={
                "level": level_name,
                "format": _opt("format", "LOG_FORMAT"),
                "source": "config" if settings is not None else "env",
                "file_logging": bool(log_dir) and file_error is None,
                "log_dir": log_dir or None,
                "frame_sample_rate": structured.sample_rate,
            },
        )
        if file_error:
            structured.warning("file_logging_unavailable", detail={"reason": file_error})

        return structured


def get_logger(container: str) -> StructuredLogger:
    """이미 초기화된 로거를 얻는다. 없으면 초기화한다."""
    return _instances.get(container) or setup_logging(container)


class Timer:
    """
    구간 소요시간 측정용 컨텍스트 매니저 (Pillar 3의 계측 준비).

        with Timer() as t:
            result = do_work()
        log.sampled("inference_done", duration_ms=t.ms)
    """

    __slots__ = ("_start", "ms")

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        self.ms = 0.0
        return self

    def __exit__(self, *exc) -> None:
        self.ms = round((time.perf_counter() - self._start) * 1000, 2)
        return None
