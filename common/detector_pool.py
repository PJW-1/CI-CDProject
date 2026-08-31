"""
세션별 MediaPipe Detector 풀 (Pillar 3: 성능 및 메모리 관리)

고도화 이전의 결함
-----------------
    # container_b_vision/main.py (수정 전)
    hands_detector = mp_vision.HandLandmarker.create_from_options(...)   # 전역 1개
    ...
    timestamp_ms = int(time.time() * 1000)                               # 벽시계
    result = hands_detector.detect_for_video(mp_image, timestamp_ms)

두 가지 문제가 겹쳐 있었다.

1) 세션 간 추적 상태 오염
   RunningMode.VIDEO 는 이전 프레임의 추적 결과를 다음 프레임에 활용한다.
   그것이 부드러운 트래킹의 원리지만, 동시에 detector 가 "상태를 가진다"는 뜻이다.
   전역 인스턴스 하나를 모든 세션이 공유했으므로 사용자 2명이 동시에 접속하면
   서로의 손 추적 상태를 오염시켰다.

2) 타임스탬프 단조 증가 위반
   detect_for_video() 는 단조 증가하는 타임스탬프를 요구한다.
   int(time.time() * 1000) 은 이를 보장하지 못한다.
     - 같은 밀리초에 두 프레임이 도착하면 값이 동일해진다
     - 여러 세션의 프레임이 섞이면 순서가 뒤집힌다
   30fps × 다중 세션에서 밀리초 충돌은 드문 일이 아니다.

해결
----
    - 세션마다 독립된 detector 인스턴스를 준다
    - 세션마다 자체 카운터로 단조 증가 타임스탬프를 생성한다 (벽시계 사용 안 함)
    - 같은 세션 내 동시 요청은 락으로 직렬화한다 (VIDEO 모드는 순서가 의미를 가짐)
    - LRU + TTL 로 유휴 세션의 detector 를 회수해 메모리 누수를 막는다
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Optional


class _Entry:
    __slots__ = ("detector", "lock", "next_timestamp_ms", "last_used")

    def __init__(self, detector: Any, start_ts: int):
        self.detector = detector
        self.lock = threading.Lock()
        # 세션별 단조 증가 카운터. 벽시계와 무관하게 항상 증가한다.
        self.next_timestamp_ms = start_ts
        self.last_used = time.monotonic()


class DetectorPool:
    """
    세션 단위로 detector 를 격리 보관한다.

        pool = DetectorPool(factory, max_size=8, idle_ttl_s=120)
        with pool.acquire(session_id) as (detector, timestamp_ms):
            result = detector.detect_for_video(image, timestamp_ms)
    """

    def __init__(
        self,
        factory: Callable[[], Any],
        *,
        max_size: int,
        idle_ttl_s: float,
        log: Optional[Any] = None,
    ):
        self._factory = factory
        self._max_size = max(1, int(max_size))
        self._idle_ttl_s = float(idle_ttl_s)
        self._log = log
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()
        self._guard = threading.Lock()
        self._created = 0
        self._evicted = 0

    # -- 내부 ---------------------------------------------------------------
    def _evict_idle_locked(self) -> None:
        now = time.monotonic()
        stale = [
            sid for sid, entry in self._entries.items()
            if (now - entry.last_used) > self._idle_ttl_s
        ]
        for sid in stale:
            self._close_locked(sid, reason="idle_ttl")

    def _close_locked(self, session_id: str, *, reason: str) -> None:
        entry = self._entries.pop(session_id, None)
        if entry is None:
            return
        try:
            entry.detector.close()
        except Exception as exc:
            # 회수 실패가 서비스를 죽여서는 안 되지만, 흔적은 반드시 남긴다.
            # 반복되면 네이티브 리소스가 새고 있다는 신호다.
            if self._log:
                self._log.warning(
                    "detector_close_failed",
                    session_id=session_id,
                    detail={"exception": type(exc).__name__, "message": str(exc)[:200]},
                )
        self._evicted += 1
        if self._log:
            self._log.info(
                "detector_released",
                session_id=session_id,
                detail={"reason": reason, "pool_size": len(self._entries)},
            )

    def _get_entry(self, session_id: str) -> _Entry:
        with self._guard:
            self._evict_idle_locked()

            entry = self._entries.get(session_id)
            if entry is not None:
                self._entries.move_to_end(session_id)   # LRU 갱신
                entry.last_used = time.monotonic()
                return entry

            # 풀이 가득 찼으면 가장 오래 안 쓴 세션을 회수한다
            while len(self._entries) >= self._max_size:
                oldest = next(iter(self._entries))
                self._close_locked(oldest, reason="lru_evict")

            entry = _Entry(self._factory(), start_ts=0)
            self._entries[session_id] = entry
            self._created += 1
            if self._log:
                self._log.info(
                    "detector_created",
                    session_id=session_id,
                    detail={"pool_size": len(self._entries), "total_created": self._created},
                )
            return entry

    # -- 공개 API -----------------------------------------------------------
    def acquire(self, session_id: str) -> "_AcquiredDetector":
        return _AcquiredDetector(self._get_entry(session_id))

    def release(self, session_id: str) -> None:
        """세션 종료 시 즉시 회수한다."""
        with self._guard:
            self._close_locked(session_id, reason="session_ended")

    def close_all(self) -> None:
        with self._guard:
            for sid in list(self._entries):
                self._close_locked(sid, reason="shutdown")

    def stats(self) -> dict:
        with self._guard:
            return {
                "pool_size": len(self._entries),
                "max_size": self._max_size,
                "created": self._created,
                "evicted": self._evicted,
            }


class _AcquiredDetector:
    """
    컨텍스트 매니저. (detector, timestamp_ms) 를 내주고 락을 관리한다.
    타임스탬프는 세션별 카운터에서 나오므로 항상 단조 증가한다.
    """

    __slots__ = ("_entry",)

    def __init__(self, entry: _Entry):
        self._entry = entry

    def __enter__(self):
        self._entry.lock.acquire()
        # 프레임 간 간격을 33ms(≈30fps)로 가정한다.
        # 실제 값이 아니어도 무방하다. VIDEO 모드가 요구하는 것은 "단조 증가"뿐이다.
        timestamp_ms = self._entry.next_timestamp_ms
        self._entry.next_timestamp_ms += 33
        return self._entry.detector, timestamp_ms

    def __exit__(self, *exc):
        self._entry.last_used = time.monotonic()
        self._entry.lock.release()
        return False
