"""
재시도 + 서킷 브레이커 HTTP 클라이언트 (Pillar 2: 예외 처리 및 안정성)

고도화 이전 상태
    A→B, B→C 모두 단발 요청이었고, 실패는 `except Exception: pass` 로 삼켜졌다.
    일시적인 네트워크 흔들림 한 번에 프레임이 사라졌고 아무 흔적도 남지 않았다.

설계 시 주의점 (중요)
    30fps 파이프라인에서 무분별한 재시도는 오히려 해롭다.
    33ms마다 새 프레임이 도착하는데 실패한 프레임을 오래 붙잡고 재시도하면
    큐가 밀려 지연이 누적된다. 따라서

      - 재시도는 최대 1~2회, 백오프는 수십 ms 수준
      - 총 소요가 예산(budget)을 넘으면 재시도를 포기하고 프레임을 버린다
      - 연속 실패가 임계치를 넘으면 서킷을 열어 재시도 자체를 중단한다

서킷 브레이커 상태
    CLOSED    정상. 요청을 그대로 보낸다.
    OPEN      차단. 즉시 실패시켜 죽은 대상을 계속 두드리지 않는다.
    HALF_OPEN 탐침. 요청 하나만 통과시켜 회복 여부를 확인한다.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx


class CircuitOpenError(Exception):
    """서킷이 열려 있어 요청을 보내지 않았다. 장애가 아니라 의도된 차단이다."""


@dataclass
class CircuitState:
    failure_threshold: int
    recovery_timeout_s: float
    consecutive_failures: int = 0
    opened_at: Optional[float] = None
    half_open_in_flight: bool = False

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "CLOSED"
        if (time.monotonic() - self.opened_at) >= self.recovery_timeout_s:
            return "HALF_OPEN"
        return "OPEN"

    def allow(self) -> bool:
        current = self.state
        if current == "CLOSED":
            return True
        if current == "OPEN":
            return False
        # HALF_OPEN: 탐침 요청 하나만 통과시킨다
        if self.half_open_in_flight:
            return False
        self.half_open_in_flight = True
        return True

    def record_success(self) -> Optional[str]:
        """성공 기록. 서킷이 닫혔다면 'closed' 를 반환한다(로깅용)."""
        was_open = self.opened_at is not None
        self.consecutive_failures = 0
        self.opened_at = None
        self.half_open_in_flight = False
        return "closed" if was_open else None

    def record_failure(self) -> Optional[str]:
        """실패 기록. 서킷이 새로 열렸다면 'opened' 를 반환한다(로깅용)."""
        self.half_open_in_flight = False
        self.consecutive_failures += 1
        if self.opened_at is None and self.consecutive_failures >= self.failure_threshold:
            self.opened_at = time.monotonic()
            return "opened"
        if self.opened_at is not None:
            # HALF_OPEN 탐침이 실패했다. 다시 대기 시간을 리셋한다.
            self.opened_at = time.monotonic()
        return None


class ResilientClient:
    """
    httpx.AsyncClient 래퍼. 재시도와 서킷 브레이커를 캡슐화한다.

        client = ResilientClient(cfg, log, peer="vision")
        result = await client.post_json(url, payload, session_id=sid, trace_id=tid)
        if result.ok:
            data = result.data
        else:
            # result.reason: TIMEOUT | UNREACHABLE | BAD_STATUS | CIRCUIT_OPEN | ERROR
    """

    def __init__(self, cfg: Any, log: Any, *, peer: str):
        self._log = log
        self._peer = peer
        self._max_attempts = max(1, int(cfg.get("http.retry.max_attempts", 1)))
        self._backoff_base = float(cfg.get("http.retry.backoff_base_s", 0.05))
        self._read_timeout = float(cfg.get("http.read_timeout_s", 1.5))
        self._connect_timeout = float(cfg.get("http.connect_timeout_s", 1.0))

        self._circuit = CircuitState(
            failure_threshold=int(cfg.get("http.circuit_breaker.failure_threshold", 10)),
            recovery_timeout_s=float(cfg.get("http.circuit_breaker.recovery_timeout_s", 5.0)),
        )

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=self._connect_timeout,
                read=self._read_timeout,
                write=self._read_timeout,
                pool=self._connect_timeout,
            )
        )

    @property
    def circuit_state(self) -> str:
        return self._circuit.state

    async def aclose(self) -> None:
        await self._client.aclose()

    async def post_json(
        self,
        url: str,
        payload: dict,
        *,
        session_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        budget_s: Optional[float] = None,
    ) -> "CallResult":
        if not self._circuit.allow():
            return CallResult(ok=False, reason="CIRCUIT_OPEN", attempts=0)

        deadline = time.monotonic() + budget_s if budget_s else None
        last_reason = "ERROR"
        last_detail: dict = {}

        for attempt in range(1, self._max_attempts + 1):
            if deadline and time.monotonic() >= deadline:
                # 예산 초과. 더 시도하면 뒤따르는 프레임까지 밀린다.
                last_reason = "BUDGET_EXCEEDED"
                break
            try:
                resp = await self._client.post(url, json=payload)
                if resp.status_code == 200:
                    if self._circuit.record_success() == "closed":
                        self._log.info(
                            "circuit_closed",
                            session_id=session_id,
                            detail={"peer": self._peer, "url": url},
                        )
                    return CallResult(ok=True, data=resp.json(), attempts=attempt)

                last_reason = "BAD_STATUS"
                last_detail = {"status_code": resp.status_code, "body_preview": resp.text[:200]}
                # 5xx는 재시도할 가치가 있지만 4xx는 다시 보내도 같은 결과다.
                if resp.status_code < 500:
                    break

            except httpx.TimeoutException:
                last_reason = "TIMEOUT"
                last_detail = {"read_timeout_s": self._read_timeout}
            except httpx.ConnectError:
                last_reason = "UNREACHABLE"
                last_detail = {"url": url}
            except Exception as exc:
                last_reason = "ERROR"
                last_detail = {"exception": type(exc).__name__, "message": str(exc)[:200]}
                break  # 예기치 못한 예외는 재시도해도 소용없다

            if attempt < self._max_attempts:
                await asyncio.sleep(self._backoff_base * attempt)

        opened = self._circuit.record_failure()
        if opened == "opened":
            self._log.error(
                "circuit_opened",
                session_id=session_id,
                detail={
                    "peer": self._peer,
                    "url": url,
                    "consecutive_failures": self._circuit.consecutive_failures,
                    "recovery_timeout_s": self._circuit.recovery_timeout_s,
                    "last_reason": last_reason,
                },
            )

        return CallResult(
            ok=False,
            reason=last_reason,
            detail=last_detail,
            attempts=min(self._max_attempts, max(1, self._max_attempts)),
        )


@dataclass
class CallResult:
    ok: bool
    data: Optional[dict] = None
    reason: str = ""
    detail: Optional[dict] = None
    attempts: int = 0
