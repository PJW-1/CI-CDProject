"""
서비스 간 응답 계약 (Pillar 2: 예외 처리 및 안정성)

고도화 이전 문제
    B는 손 미검출 시 {"success": true, "action": "NONE", "landmarks": []} 만 반환하고
    x / y 키 자체가 없었다. A는 result.get("x") 로 꺼내 그대로 릴레이했으므로
    PC 브라우저는 {"x": null, "y": null} 을 수신했다.

    실측 확인된 실제 페이로드:
        {"type":"GESTURE","action":"NONE","x":null,"y":null, ...}

    지금은 프론트 JS가 우연히 견디고 있을 뿐 계약이 없다.
    누군가 x.toFixed() 를 호출하는 순간 런타임 에러다.

해결
    3개 컨테이너가 같은 Pydantic 모델을 공유하고, 좌표에 기본값을 보장한다.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# 좌표 미확정 시의 중립값. 화면 중앙을 의미한다.
NEUTRAL_X = 0.5
NEUTRAL_Y = 0.5


class GestureResult(BaseModel):
    """Container C 가 산출하고 B·A 를 거쳐 PC로 전달되는 제스처 결과."""

    session_id: str = ""
    action: str = "NONE"
    # null 좌표 누수를 스키마 레벨에서 차단한다.
    x: float = NEUTRAL_X
    y: float = NEUTRAL_Y
    delta: float = 0.0
    pan_dx: float = 0.0
    pan_dy: float = 0.0
    detected: bool = False

    @classmethod
    def neutral(cls, session_id: str = "", action: str = "NONE") -> GestureResult:
        return cls(session_id=session_id, action=action)

    @classmethod
    def from_upstream(cls, raw: dict[str, Any] | None, session_id: str = "") -> GestureResult:
        """
        상류 응답을 안전하게 정규화한다.
        키 누락, None, 타입 불일치를 전부 흡수해 항상 유효한 결과를 만든다.
        """
        if not isinstance(raw, dict):
            return cls.neutral(session_id)

        def _num(key: str, default: float) -> float:
            value = raw.get(key)
            if value is None:
                return default
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        return cls(
            session_id=str(raw.get("session_id") or session_id),
            action=str(raw.get("action") or "NONE"),
            x=_num("x", NEUTRAL_X),
            y=_num("y", NEUTRAL_Y),
            delta=_num("delta", 0.0),
            pan_dx=_num("pan_dx", 0.0),
            pan_dy=_num("pan_dy", 0.0),
            detected=bool(raw.get("detected", False)),
        )


class HealthState:
    """PC 클라이언트에 통지하는 파이프라인 상태 (Graceful Degradation)."""

    OK = "OK"
    DEGRADED = "DEGRADED"   # 일부 기능 손실. 커서는 살아 있음
    DOWN = "DOWN"           # 상류 완전 단절


class AnalyzeResponse(BaseModel):
    """Container B 의 /analyze 응답."""

    success: bool = True
    action: str = "NONE"
    x: float = NEUTRAL_X
    y: float = NEUTRAL_Y
    delta: float = 0.0
    pan_dx: float = 0.0
    pan_dy: float = 0.0
    detected: bool = False
    landmarks: list[dict[str, float]] = Field(default_factory=list)
    health: str = HealthState.OK
    error: str | None = None

    @classmethod
    def from_gesture(
        cls,
        gesture: GestureResult,
        landmarks: list[dict[str, float]],
        health: str = HealthState.OK,
    ) -> AnalyzeResponse:
        return cls(
            success=True,
            action=gesture.action,
            x=gesture.x,
            y=gesture.y,
            delta=gesture.delta,
            pan_dx=gesture.pan_dx,
            pan_dy=gesture.pan_dy,
            detected=gesture.detected,
            landmarks=landmarks,
            health=health,
        )

    @classmethod
    def degraded(
        cls,
        reason: str,
        landmarks: list[dict[str, float]] | None = None,
        health: str = HealthState.DEGRADED,
    ) -> AnalyzeResponse:
        """
        상류(C)가 죽어도 A에게 유효한 응답을 돌려준다.
        landmarks 가 있으면 PC는 최소한 손 스켈레톤과 커서를 계속 그릴 수 있다.
        """
        return cls(
            success=True,          # 파이프라인은 살아 있다. 기능만 축소됐다.
            action="HOVER" if landmarks else "NONE",
            detected=bool(landmarks),
            landmarks=landmarks or [],
            health=health,
            error=reason,
        )
