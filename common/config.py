"""
설정 로더 (Pillar 1: 파라미터화 및 가독성)

고도화 이전에는 설정 파일이 하나도 없었다. IP·포트·타임아웃·제스처 임계값이
파이썬 코드, Dockerfile, docker-compose.yml, HTML에 각각 하드코딩되어
같은 값이 여러 곳에 중복 기재되어 있었다. 값 하나를 바꾸려면 최소 8곳을
동시에 고쳐야 했고, 하나라도 놓치면 조용히 깨졌다.

이 모듈은 그 값들의 단일 출처를 제공한다.

우선순위 (뒤로 갈수록 우선)
    1. config/default.yaml
    2. config/{APP_ENV}.yaml        (APP_ENV 미지정 시 생략)
    3. 환경변수

환경변수 규칙
    - 중첩 경로:  AIRCANVAS__GESTURE__EMA__ALPHA_FAST=0.9
                 (구분자는 이중 언더스코어. 키 자체에 쓰인 단일 _ 와 충돌하지 않는다)
    - 레거시 별칭: HOST_IP, CONTAINER_B_URL, CONTAINER_C_URL, LOG_LEVEL 등
                 기존 docker-compose.yml 을 깨지 않기 위해 유지한다.

사용법
    from common.config import load_config
    cfg = load_config()
    cfg.get("gesture.ema.alpha_fast")     # 0.85
    cfg.gesture.ema.alpha_fast            # 0.85 (동일)
"""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator, Mapping
from typing import Any

import yaml

_ENV_PREFIX = "AIRCANVAS__"
_ENV_SEP = "__"

# 기존 docker-compose.yml / 실행 스크립트가 쓰던 이름 → 설정 경로 매핑.
# 이것이 없으면 이번 리팩터링이 기존 배포 방식을 깨뜨린다.
_LEGACY_ENV_MAP = {
    "HOST_IP": "network.host_ip",
    "CONTAINER_B_URL": "network.vision_url",
    "CONTAINER_C_URL": "network.gesture_url",
    "LOG_LEVEL": "logging.level",
    "LOG_FORMAT": "logging.format",
    "LOG_DIR": "logging.dir",
    "LOG_MAX_BYTES": "logging.max_bytes",
    "LOG_BACKUP_COUNT": "logging.backup_count",
    "LOG_FRAME_SAMPLE_RATE": "logging.frame_sample_rate",
    "HAND_LANDMARKER_MODEL_PATH": "vision.model_path",
}

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_DIR = os.environ.get("CONFIG_DIR") or os.path.join(_REPO_ROOT, "config")


class ConfigError(RuntimeError):
    """설정 로딩 실패. 조용히 기본값으로 넘어가지 않고 명시적으로 실패시킨다."""


def detect_lan_ip(fallback: str = "127.0.0.1") -> str:
    """
    UDP 소켓의 로컬 바인딩 주소로 LAN IP를 탐지한다. 실제 패킷은 전송되지 않는다.
    start_local.py 에만 있던 구현을 공용으로 승격한 것이다.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return fallback


def _coerce(raw: str) -> Any:
    """환경변수 문자열을 YAML 규칙으로 파싱해 타입을 살린다 ("0.9" → float)."""
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw
    return raw if parsed is None else parsed


def _deep_merge(base: dict, override: Mapping) -> dict:
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _set_path(target: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = target
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


class Config(Mapping):
    """읽기 전용 설정 트리. dict 접근과 점 표기법을 모두 지원한다."""

    __slots__ = ("_data",)

    def __init__(self, data: dict):
        object.__setattr__(self, "_data", data)

    # -- Mapping 프로토콜 ---------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        value = self._data[key]
        return Config(value) if isinstance(value, dict) else value

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    # -- 편의 접근 -----------------------------------------------------------
    def __getattr__(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(f"설정 키가 없습니다: {key}") from exc

    def __setattr__(self, *_args) -> None:
        raise TypeError("Config는 읽기 전용입니다. 값 변경은 설정 파일이나 환경변수로 하세요.")

    def get(self, dotted: str, default: Any = None) -> Any:
        """cfg.get('gesture.ema.alpha_fast') 처럼 점 경로로 조회한다."""
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return Config(node) if isinstance(node, dict) else node

    def require(self, dotted: str) -> Any:
        """없으면 즉시 실패한다. 조용한 기본값 대체를 막기 위한 것."""
        sentinel = object()
        value = self.get(dotted, sentinel)
        if value is sentinel:
            raise ConfigError(f"필수 설정 키가 없습니다: {dotted}")
        return value

    def to_dict(self) -> dict:
        import copy

        return copy.deepcopy(self._data)


def _load_yaml(path: str, *, required: bool) -> dict:
    if not os.path.exists(path):
        if required:
            raise ConfigError(f"설정 파일을 찾을 수 없습니다: {path}")
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"설정 파일 파싱 실패 ({path}): {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"설정 파일 최상위는 매핑이어야 합니다: {path}")
    return data


def _apply_env_overrides(data: dict) -> list[str]:
    """환경변수 오버라이드를 적용하고, 적용된 경로 목록을 반환한다(로깅용)."""
    applied: list[str] = []

    for env_key, dotted in _LEGACY_ENV_MAP.items():
        raw = os.environ.get(env_key)
        if raw is not None and raw != "":
            _set_path(data, dotted, _coerce(raw))
            applied.append(f"{env_key}→{dotted}")

    for env_key, raw in os.environ.items():
        if not env_key.startswith(_ENV_PREFIX):
            continue
        path = env_key[len(_ENV_PREFIX):].lower().replace(_ENV_SEP, ".")
        if not path:
            continue
        _set_path(data, path, _coerce(raw))
        applied.append(f"{env_key}→{path}")

    return applied


def _resolve_derived(data: dict) -> None:
    """auto/빈 값 같은 파생 설정을 실제 값으로 확정한다."""
    network = data.setdefault("network", {})

    if str(network.get("host_ip", "auto")).lower() == "auto":
        network["host_ip"] = detect_lan_ip(network.get("fallback_host_ip", "127.0.0.1"))

    # 포트 폴백 리터럴을 두지 않는다. default.yaml 이 단일 출처이며,
    # 값이 없으면 조용히 넘어가지 않고 명시적으로 실패해야 한다.
    if not network.get("vision_url"):
        port = network["vision_port"]
        network["vision_url"] = f"http://container_b:{port}/analyze"
    if not network.get("gesture_url"):
        port = network["gesture_port"]
        network["gesture_url"] = f"http://container_c:{port}/gesture"

    vision = data.setdefault("vision", {})
    if not vision.get("model_path"):
        vision["model_path"] = os.path.join("models", vision.get("model_filename", "hand_landmarker.task"))


_cache: Config | None = None
_cache_meta: dict = {}


def load_config(*, reload: bool = False) -> Config:
    """설정을 로드한다. 프로세스당 1회 로드 후 캐시된다."""
    global _cache, _cache_meta
    if _cache is not None and not reload:
        return _cache

    data = _load_yaml(os.path.join(_CONFIG_DIR, "default.yaml"), required=True)

    app_env = os.environ.get("APP_ENV", "").strip().lower()
    if app_env:
        overlay = _load_yaml(os.path.join(_CONFIG_DIR, f"{app_env}.yaml"), required=True)
        _deep_merge(data, overlay)

    applied = _apply_env_overrides(data)
    _resolve_derived(data)

    _cache = Config(data)
    _cache_meta = {
        "config_dir": _CONFIG_DIR,
        "app_env": app_env or None,
        "env_overrides": applied,
    }
    return _cache


def config_meta() -> dict:
    """설정이 어디서 왔는지. 기동 로그에 남겨 장애 분석의 출발점으로 쓴다."""
    if _cache is None:
        load_config()
    return dict(_cache_meta)
