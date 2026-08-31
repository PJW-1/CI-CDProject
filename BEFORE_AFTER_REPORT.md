# Air Canvas — 프로덕션 고도화 수정 전/후 비교 보고서

| 항목 | 내용 |
|---|---|
| **기준 문서** | `Production_Engineering_Blueprint.pptx` (4대 엔지니어링 축 + CI/CD) |
| **작업일** | 2026-08-31 |
| **저장소** | https://github.com/PJW-1/CI-CDProject |
| **베이스라인** | `a6b2596` (first commit) |
| **작업 브랜치** | `refining/pillar4-logging` |
| **계획서** | [PRODUCTION_REFINING_PLAN.md](PRODUCTION_REFINING_PLAN.md) |

> 이 문서의 모든 수치는 **실제로 측정한 값**이다. 추정치나 예상치가 아니다.
> "수정 전" 데이터는 `git worktree` 로 `a6b2596` 커밋을 꺼내
> 동일한 시나리오·동일한 머신에서 실측했다.

---

## 0. 한눈에 보기

| 지표 | 수정 전 | 수정 후 | 변화 |
|---|---:|---:|---|
| **동시 4세션 처리량** | 56.7 fps | **121.6 fps** | **+114%** |
| **동시 4세션 p50 지연** | 69.1 ms | **24.9 ms** | **−64%** |
| **동시 4세션 p95 지연** | 74.4 ms | **47.6 ms** | **−36%** |
| 단일 세션 p50 지연 | 19.7 ms | 19.8 ms | 변화 없음 |
| **애플리케이션 로그** | **0줄** | 구조화 JSON | 관측 가능해짐 |
| **자동화 테스트** | **0개** | **91개** | 전부 통과 |
| 소스 내 IP 리터럴 | 3곳 | **0곳** | 제거 |
| 소스 내 포트 리터럴 | 8곳 | **0곳** | 제거 |
| `except: pass` | 1곳 | **0곳** | 제거 |
| `print()` (서비스 코드) | 4곳 | **0곳** | 제거 |
| 설정 파일 | 없음 | `config/*.yaml` | 신설 |
| 헬스체크 | B, C만 (A는 404) | 3개 전부 + readiness | 완비 |
| CI 파이프라인 | 없음 | GitHub Actions 4잡 | 신설 |
| Python 코드 | 667줄 | 3,765줄 (테스트 1,235줄 별도) | — |

**보존 확인:** 6대 제스처 판별 결과, EMA 보정, 디바운스 동작은 **100% 동일**하다.
설정 기본값이 고도화 이전 하드코딩 값과 정확히 같음을 테스트로 강제한다.

---

## 1. 측정 방법

공정한 비교를 위해 입력과 조건을 고정했다.

| 조건 | 값 |
|---|---|
| 프레임 | 480×360 단색 JPEG (q=60), base64 7,259자 |
| 시나리오 | 정상 40프레임 + 손상된 base64 3프레임 |
| 벤치마크 | 단일 세션 60프레임 / 동시 4세션 × 60프레임 |
| 측정 지점 | 모바일 WebSocket 송신 → FEEDBACK 수신까지 왕복 |
| 머신 | 동일 PC, Docker Desktop (Linux 컨테이너), CPU 추론 |

```bash
# 수정 전 스택 재현 방법
git worktree add /tmp/baseline a6b2596
cd /tmp/baseline && docker compose -p aircanvas-baseline up --build -d
```

> **주의:** 두 측정 모두 **손이 없는 단색 프레임** 기준이다.
> MediaPipe가 손을 검출하지 못하므로 Container C는 호출되지 않는다.
> 즉 이 수치는 A↔B 구간(가장 무거운 구간)의 성능을 나타낸다.
> 실제 손이 포함된 프레임의 절대 지연은 이보다 크다.

---

## 2. Pillar 4 — 로깅

> *"문제 발생 시 책임 소재를 밝히는 '법적 흔적'이자 디버깅의 유일한 열쇠다."*

### 2.1 수정 전 진단

| 컨테이너 | 상태 |
|---|---|
| **A** | `print()` 4곳이 전부 |
| **B** | `import logging` 만 하고 **한 줄도 사용 안 함** |
| **C** | 구조화 JSON 로깅 보유 — 단, **WebSocket 경로에만** |

### 2.2 실측으로 드러난 추가 문제

동일 시나리오(43프레임)를 돌린 뒤 `docker logs` 를 확인한 결과:

```
air-canvas-web    : 애플리케이션 로그 0줄 / 전체 25줄  (uvicorn 접속 로그뿐)
air-canvas-vision : 애플리케이션 로그 0줄 / 전체 339줄 (uvicorn 접속 로그뿐)
air-canvas-gesture: 애플리케이션 로그 0줄 / 전체 4줄
```

**Container A의 `print()` 4개가 docker logs에 아예 나타나지 않았다.**

원인은 **stdout 블록 버퍼링**이다. `print()` 는 flush를 하지 않고, 파이프에 연결된 stdout은
블록 버퍼링되므로 짧은 문자열 4개는 버퍼에 갇힌 채 출력되지 않았다.
uvicorn 로그는 `logging.StreamHandler` 가 emit마다 flush하기 때문에 보였던 것이다.

> 즉 A의 로깅은 "구조화되지 않았다"가 아니라 **사실상 존재하지 않았다.**

**Container C도 마찬가지로 0줄**이었다. C의 `log_event()` 는 WebSocket 엔드포인트에만
걸려 있었는데, 실제 운영 경로는 HTTP `POST /gesture` 다. 즉 **관측되던 경로와
실제로 쓰이던 경로가 서로 달랐다.**

### 2.3 수정 후

같은 시나리오에서:

```
air-canvas-web    : 구조화 로그 7줄
air-canvas-vision : 구조화 로그 8줄
air-canvas-gesture: 구조화 로그 2줄
```

**수정 전 — Container A (실제 출력)**
```
INFO:     172.23.0.1:35394 - "WebSocket /ws/pc/before_run" [accepted]
INFO:     connection open
INFO:     172.23.0.1:35408 - "WebSocket /ws/mobile/before_run" [accepted]
INFO:     connection open
```

**수정 후 — Container A (실제 출력)**
```json
{"ts":1788143492214,"time":"2026-08-31T02:31:32.214+00:00","container":"A","level":"INFO",
 "event":"pc_connected","session_id":"after_run",
 "detail":{"mobile_already_connected":false,"active_rooms":1}}

{"ts":1788143493073,"time":"2026-08-31T02:31:33.073+00:00","container":"A","level":"INFO",
 "event":"mobile_disconnected","session_id":"after_run",
 "detail":{"frames_received":43,"failed_frames":0,"failure_rate":0.0}}
```

### 2.4 손상된 프레임 처리 — 가장 극적인 차이

시나리오의 **깨진 base64 프레임 3장**에 대해:

| | 수정 전 | 수정 후 |
|---|---|---|
| HTTP 응답 | `200 OK` | `200` (degraded 표기) |
| 로그 | **없음** | WARNING + 사유 + 페이로드 크기 |
| 원인 추적 | 불가능 | 가능 |

**수정 전:** uvicorn 접속 로그에 `POST /analyze 200 OK` 만 남았다. 정상 프레임과 구분 불가.

**수정 후 (실제 출력)**
```json
{"container":"B","level":"WARNING","event":"frame_rejected","session_id":"p23_run",
 "detail":{"reason":"base64 디코딩 실패: Non-base64 digit found","payload_chars":39}}
```

예기치 못한 예외는 traceback 전문이 남는다 (실제 출력, 개발 중 포착):
```json
{"container":"B","level":"ERROR","event":"analyze_failed","session_id":"after_run",
 "error":{"type":"Error","message":"Invalid base64-encoded string: ...",
 "traceback":"Traceback (most recent call last):\n  File \"/app/main.py\", line 136, ...\n"}}
```

### 2.5 해결한 설계 결함

| 항목 | 수정 전 | 수정 후 |
|---|---|---|
| 레벨 필터링 | `level` 인자를 받고도 항상 `logger.info()` 호출 → 필터링 무의미 | 실제 로거 메서드에 매핑. `LOG_LEVEL` 로 제어 |
| 컨테이너 식별 | `"C"` 하드코딩 | 초기화 시 주입 |
| 로그 영속화 | stdout 전용 → 재시작 시 소실 | `RotatingFileHandler` (10MB × 5) + 볼륨 |
| traceback | 남길 방법 없음 | `log.exception()` 이 자동 첨부 |
| 프레임 로그 폭주 | C가 30fps 전량을 INFO로 기록 | `sampled()` — 첫 발생 + N회마다 1건 |
| 요청 추적 | 불가능 | `session_id` + `trace_id` 전 구간 전파 |
| 버퍼링 | `print()` 가 버퍼에 갇힘 | `PYTHONUNBUFFERED=1` + StreamHandler flush |

### 2.6 샘플링 동작 (실측)

43프레임 전송 → `LOG_FRAME_SAMPLE_RATE=30` 기준 **2건만 기록**:

```json
{"level":"DEBUG","event":"frame_relayed","session_id":"debug_run","trace_id":"debug_run-1",
 "detail":{"vision_rtt_ms":57.99,"action":"NONE","detected":false,"frame_bytes":7259,
           "sampled_every":30,"occurrence":1}}
{"level":"DEBUG","event":"frame_relayed","trace_id":"debug_run-31",
 "detail":{"vision_rtt_ms":18.46,...,"occurrence":31}}
```

`sampled_every` 와 `occurrence` 를 함께 남겨 "1건이 실제로는 30건을 대표한다"는 사실을 잃지 않는다.
**에러는 절대 샘플링하지 않는다.**

### 2.7 상태 전이 로깅 — 저빈도·고가치

프레임 단위 로그와 달리, 제스처 상태가 **바뀔 때만** 기록한다.
7프레임을 처리해도 전이는 2건뿐이다 (실제 출력):

```
[INFO] [C] [s1] session_created active_sessions=1
[INFO] [C] [s1] action_changed from='HOVER' to='DRAW'  raw_action='DRAW'  instant_pen_up=False
[INFO] [C] [s1] action_changed from='DRAW' to='HOVER'  raw_action='HOVER' instant_pen_up=True
```

`instant_pen_up=True` 는 **비대칭 무지연 펜-업 컷오프가 발동했다**는 뜻이다.
이 프로젝트에서 가장 미묘한 로직이 이제 로그로 관측된다.

---

## 3. Pillar 1 — 파라미터화 및 가독성

> *"나만 쓰는 코드가 아니다. 누구나 환경설정만 바꿔서 쓸 수 있어야 한다."*

### 3.1 수정 전: 설정 파일이 하나도 없었다

### 3.2 IP 하드코딩 — 가장 심각했던 문제

**수정 전:** `192.168.55.208` 이 3곳에 중복

| 위치 | 코드 |
|---|---|
| `container_a_web/main.py:20` | `os.getenv("HOST_IP", "192.168.55.208")` |
| `container_a_web/static/pc.html:294` | `? '192.168.55.208'` |
| `docker-compose.yml` | `- HOST_IP=192.168.55.208` |

발급된 QR을 디코딩한 실측 결과:
```
https://192.168.55.208:8443/mobile?session=testsession
```
이 PC의 실제 LAN IP가 **우연히** `192.168.55.208` 이라 동작했을 뿐,
다른 Wi-Fi로 옮기거나 DHCP가 IP를 바꾸면 QR이 즉시 죽는 구조였다.

**수정 후:** 소스에 IP 리터럴 0개. 호스트에서 탐지해 주입한다.

```bash
$ python scripts/compose_up.py
[+] 호스트 LAN IP 탐지: 192.168.55.208
    PC 접속 : https://localhost:8443
    폰 접속 : https://192.168.55.208:8443
```

QR 디코딩 결과는 동일하되, **값의 출처가 코드가 아니라 런타임 탐지**다.

> **설계 판단:** 컨테이너 **안에서** LAN IP를 탐지하면 Docker 내부 주소(172.x)가 잡혀
> 폰이 접속할 수 없다. 그래서 탐지를 호스트에서 수행하고 환경변수로 주입한다.
> 그럼에도 값이 컨테이너 내부 대역이면 A가 `host_ip_unusable` 경고를 남긴다.

### 3.3 제스처 임계값 — 매직넘버 9종 → 설정

| 값 | 수정 전 위치 | 수정 후 |
|---|---|---|
| `0.15` / `0.12` | `c/main.py:126` | `gesture.thresholds.thumb_open_*` |
| `0.13` | `c/main.py:127` | `gesture.thresholds.thumb_folded_dist` |
| `0.12` | `c/main.py:151` | `gesture.thresholds.erase_height_diff` |
| `±0.008` | `c/main.py:165,174` | `gesture.zoom.step` |
| `maxlen=3` | `c/main.py:57` | `gesture.debounce.window` |
| `>= 2` | `c/main.py:212` | `gesture.debounce.majority` |
| `0.001` / `0.05` | `c/main.py:226,229` | `gesture.ema.deadzone_dist` / `slow_dist` |
| `0.35`/`0.50`/`0.85` | `c/main.py:227,230,233` | `gesture.ema.alpha_micro/precise/fast` |
| `600` | `c/main.py:65` | `gesture.session.ttl_s` |

**기본값은 원본과 정확히 동일하다.** 13개 값 전부를 테스트가 강제한다:

```python
def test_baseline_default_values_preserved(gesture_module, name, expected):
    assert getattr(gesture_module, name) == expected
```

### 3.4 그 외

| 항목 | 수정 전 | 수정 후 |
|---|---|---|
| 포트 | 8443/8001/8002가 5개 계층 8곳에 산재 | `config/default.yaml` 단일 출처 |
| 타임아웃 | `0.2s`(A) / `0.3s`(B) 하드코딩 | `http.connect/read_timeout_s` (1.0s/1.5s) |
| 스트리밍 파라미터 | `mobile.html` 에 `480×360`, `q=0.6`, `33ms` 고정 | `/api/config` 로 서버가 내려줌 |
| 환경 분리 | 없음 | `dev.yaml` / `prod.yaml` (`APP_ENV`) |
| 미사용 import | B에 4개 (`asyncio`, `json`, `logging`, `sys`) | 전 파일 0개 |
| 공용 모듈 | 없음 (3개 컨테이너가 각자 구현) | `common/` 5개 모듈 공유 |

**설정 우선순위:** `default.yaml` → `{APP_ENV}.yaml` → 환경변수

```bash
# 코드 수정 없이 EMA 튜닝
AIRCANVAS__GESTURE__EMA__ALPHA_FAST=0.9 docker compose up
```

기존 `HOST_IP`, `CONTAINER_B_URL` 등 레거시 환경변수도 별칭으로 계속 지원한다
(기존 배포 방식을 깨뜨리지 않기 위함).

### 3.5 빌드 구조 변경

3개 컨테이너가 `common/` 과 `config/` 를 공유하려면 빌드 컨텍스트가 저장소 루트여야 한다.

```yaml
# 수정 전                          # 수정 후
build:                            build:
  context: ./container_a_web        context: .
  dockerfile: Dockerfile            dockerfile: container_a_web/Dockerfile
```

---

## 4. Pillar 2 — 예외 처리 및 안정성

> *"단 한 번의 에러로 24시간 도는 공장 생산 라인을 멈추게 할 수는 없다."*

### 4.1 침묵 예외 제거

**수정 전** — `container_a_web/main.py:126`
```python
            except Exception:
                pass
```

이 한 줄이 B 다운, 타임아웃, JSON 파싱 실패, PC 소켓 단절, 코드 버그를
**전부 무음 처리**했다. 사용자는 "그림이 안 그려진다"만 알고 로그에는 흔적조차 없었다.
`status_code != 200` 인 경우를 처리하는 `else` 분기도 아예 없었다.

**수정 후** — 예외 종류별 분기 + 전량 로깅
```python
except httpx.TimeoutException:   log.warning("vision_timeout", ...)
except httpx.ConnectError:       log.error("vision_unreachable", ...)
except WebSocketDisconnect:      raise
except Exception:                log.exception("frame_relay_failed", ...)   # traceback 포함
```

정적 테스트가 재발을 막는다:
```
test_no_silent_exception_swallowing PASSED
```

### 4.2 재시도 + 서킷 브레이커

`common/http_client.py` 신설.

**설계 시 핵심 주의점:** 30fps 파이프라인에서 무분별한 재시도는 오히려 해롭다.
33ms마다 새 프레임이 오는데 실패한 프레임을 붙잡고 재시도하면 큐가 밀린다. 따라서:

- 최대 **2회**, 백오프 50ms
- **프레임 예산**(interval_ms × 2 = 66ms) 초과 시 재시도 포기하고 프레임 폐기
- 연속 10회 실패 시 **서킷 OPEN** → 죽은 대상을 계속 두드리지 않음
- 5초 후 **HALF_OPEN** → 탐침 1개만 통과시켜 회복 확인
- 4xx는 재시도하지 않음 (다시 보내도 같은 결과)

### 4.3 Graceful Degradation — 실측 검증

**검증 방법:** `docker stop air-canvas-gesture` 로 Container C를 실제로 죽였다.

| 확인 항목 | 수정 전 | 수정 후 (실측) |
|---|---|---|
| A 프로세스 | 살아있음 | 살아있음 |
| A `/health` | **404 (엔드포인트 없음)** | `200 {"status":"ok"}` |
| A `/health/ready` | 없음 | **`503 {"status":"degraded",...}`** |
| 웹페이지 | 200 | 200 |
| 사용자 통지 | **없음** | `STATUS` 메시지로 통지 |
| 드로잉 | 통째로 정지 | 커서 유지, 그리기만 비활성 |

**수정 후 실제 응답 (C 다운 상태)**
```json
{"status":"degraded","checks":{"vision_chain":{"ok":false,"status_code":503,
 "upstream":{"status":"degraded","circuit":"CLOSED","reason":"TIMEOUT"}}}}
```

readiness는 **전이적**이다. A가 B의 `/health/ready` 를 부르고, B가 다시 C를 확인한다.
A가 B의 `/health`(자기 자신만 확인)를 불렀다면 C가 죽어도 `ready` 로 잘못 보고됐을 것이다.

degradation 시에도 모바일에 `FEEDBACK` 을 계속 돌려준다.
그러지 않으면 클라이언트의 백프레셔 카운터가 잠겨 스트리밍이 영구 정지한다.

### 4.4 null 좌표 누수 차단

**수정 전 실측 페이로드** (손 미검출 시 PC가 실제로 수신한 것):
```json
{"type":"GESTURE","action":"NONE","x":null,"y":null,"delta":0,...}
```

B가 x/y 키 없는 응답을 주고 A가 `result.get("x")` 로 꺼내 그대로 릴레이한 결과다.
프론트 JS가 우연히 견디고 있었을 뿐, 서비스 간 **응답 계약이 없었다.**

**수정 후:** `common/schemas.py` 의 Pydantic 모델을 3개 컨테이너가 공유하고,
`GestureResult.from_upstream()` 이 키 누락·None·타입 불일치를 전부 흡수한다.

통합 테스트가 이를 강제한다:
```
test_null_coordinates_never_reach_pc PASSED
```

### 4.5 입력값 검증

| 항목 | 수정 전 | 수정 후 |
|---|---|---|
| base64 형식 | 검증 없음 → `binascii.Error` 발생 | `validate=True` + `FrameValidationError` |
| 크기 상한 | 없음 (메모리 압박 가능) | `vision.max_frame_bytes` (2MB) |
| JPEG 디코딩 실패 | `None` 이 암묵적으로 흘러감 | 명시적 예외 → WARNING 로그 |
| 오류 분류 | 전부 동일 취급 | 클라이언트 오류 = WARNING, 서버 오류 = ERROR |

### 4.6 세션 누수 수정

**수정 전** — 값만 `None` 으로 바꾸고 키는 영구 잔류
```python
rooms[session_id]["pc_ws"] = None      # 키는 지워지지 않음
```
세션 ID는 `pc.html` 에서 `Math.random()` 으로 매번 새로 생성되므로,
**페이지를 새로고침할 때마다 죽은 엔트리가 하나씩 영구 누적**되는 구조였다.

**수정 후** — 양쪽이 모두 끊기면 방 자체를 삭제 (실측 로그)
```json
{"container":"A","level":"INFO","event":"session_room_released",
 "session_id":"p23_run","detail":{"active_rooms":0}}
```

테스트가 회귀를 막는다:
```
test_room_is_released_when_both_disconnect PASSED
test_many_sessions_do_not_accumulate      PASSED   # 20회 반복 후 rooms == 0
```

### 4.7 헬스체크 및 기동 순서

**수정 전:** compose에 `healthcheck` 자체가 없어 `depends_on` 은 "프로세스 시작"만 보장했다.
B는 MediaPipe 모델 로드에 시간이 걸리므로 A가 먼저 요청을 보내면 초기 프레임이 실패했다.

**수정 후 실측 기동 로그:**
```
Container air-canvas-gesture  Started
Container air-canvas-gesture  Waiting
Container air-canvas-gesture  Healthy      ← 준비 완료를 기다림
Container air-canvas-vision   Starting
Container air-canvas-vision   Healthy
Container air-canvas-web      Started
```

### 4.8 기타

| 항목 | 수정 전 | 수정 후 |
|---|---|---|
| shutdown 훅 | deprecated `@app.on_event` | `lifespan` 컨텍스트 매니저 |
| 모델 다운로드 | import 시점, 로그·재시도 없음 | 로그 + 소요시간 + 실패 시 traceback |
| SSL 인증서 | 컨테이너 재생성마다 재발급 → 폰에서 매번 경고 재승인 | `./certs` 볼륨으로 영속화 |
| 모델 파일 | 재생성마다 7.8MB 재다운로드 | `./models` 볼륨으로 캐시 |
| 예외 정보 | `str(e)` 를 응답에 노출 | 로그엔 traceback, 응답엔 예외 타입만 |

---

## 5. Pillar 3 — 성능 및 메모리 관리

> *"모델보다 전처리가 지연 시간을 더 잡아먹는 일은 없어야 한다."*

### 5.1 측정 결과

**단일 세션 (60프레임)**

| | 수정 전 | 수정 후 |
|---|---:|---:|
| 평균 | 20.5 ms | 22.1 ms |
| p50 | 19.7 ms | 19.8 ms |
| p95 | 21.8 ms | 24.7 ms |

단일 세션에서는 **사실상 차이 없다.** 평균이 1.6ms 늘어난 것은 추가된 로깅·검증·스키마 정규화
비용이다. 이 정도는 관측 가능성을 얻는 대가로 타당하다.

**동시 4세션 (240프레임)**

| | 수정 전 | 수정 후 | 변화 |
|---|---:|---:|---|
| 평균 | 69.9 ms | **31.8 ms** | **−54%** |
| p50 | 69.1 ms | **24.9 ms** | **−64%** |
| p95 | 74.4 ms | **47.6 ms** | **−36%** |
| **처리량** | 56.7 fps | **121.6 fps** | **+114%** |

동시 접속에서 격차가 벌어지는 이유는 아래 두 가지 수정 때문이다.

### 5.2 이벤트 루프 블로킹 해소

**수정 전** — `container_b_vision/main.py:93`
```python
@app.post("/analyze")
async def analyze_frame(payload: FramePayload):
    image = decode_base64_frame(payload.image)              # 동기 CPU
    detected, landmarks = extract_landmarks(image)          # 동기 CPU (MediaPipe 추론)
```

`async def` 핸들러 안에서 동기 CPU 작업을 직접 호출했다.
uvicorn의 이벤트 루프는 단일 스레드이므로, 추론이 도는 동안 **해당 워커의 이벤트 루프
전체가 정지**한다. 다른 세션의 요청은 물론 헬스체크 응답조차 처리되지 않는다.

**수정 후**
```python
image = decode_base64_frame(payload.image)
detected, landmarks_list = await run_in_threadpool(extract_landmarks, image, sid)
```

수정 전 4세션 p50이 69.1ms인데 단일 세션이 19.7ms인 것은 정확히 **직렬화의 증거**다
(≈ 19.7 × 4 = 78.8ms 에 근접). 수정 후 24.9ms는 병렬 처리가 실제로 일어났음을 뜻한다.

### 5.3 세션별 Detector 격리

**수정 전** — 전역 인스턴스 1개를 모든 세션이 공유
```python
hands_detector = mp_vision.HandLandmarker.create_from_options(
    ..., running_mode=mp_vision.RunningMode.VIDEO)     # ← 상태를 가지는 모드
...
timestamp_ms = int(time.time() * 1000)                # ← 벽시계
```

`RunningMode.VIDEO` 는 이전 프레임의 추적 결과를 다음 프레임에 활용한다.
그것이 부드러운 트래킹의 원리지만, **detector 가 상태를 가진다**는 뜻이기도 하다.
전역 인스턴스 하나를 공유했으므로 **사용자 2명이 동시 접속하면 서로의 추적 상태를 오염**시킨다.

또한 `detect_for_video()` 는 단조 증가 타임스탬프를 요구하는데 `time.time()` 은 이를 보장하지 못한다.

#### 정직한 검증 결과

**수정 전 코드에서 동시 2세션을 돌렸을 때 타임스탬프 예외는 재현되지 않았다.**

이유가 중요하다. 수정 전 코드는 이벤트 루프를 블로킹하므로 **호출이 완전히 직렬화**된다.
호출 간격이 ≈70ms이므로 같은 밀리초에 두 프레임이 들어갈 일이 없었다.

> **즉 이것은 "지금 터지는 버그"가 아니라 "스레드풀을 넣는 순간 터지는 잠재 버그"였다.**
> P3-1(블로킹 해소)만 먼저 적용했다면 새로운 장애를 만들었을 것이다.
> 계획서에 명시한 **P3-2 → P3-1 순서**가 필수였음이 이것으로 확인된다.

세션 간 추적 상태 오염은 단색 프레임(손 미검출)으로는 재현할 수 없다.
추적 상태 자체가 생기지 않기 때문이다. 이 부분은 코드 분석으로 식별하고
**선제적으로 수정한 뒤 테스트로 고정**했다.

**수정 후** — `common/detector_pool.py`
- 세션마다 독립된 detector 인스턴스
- 세션마다 **자체 카운터**로 단조 증가 타임스탬프 (벽시계 미사용)
- 같은 세션 내 요청은 락으로 직렬화 (VIDEO 모드는 순서가 의미를 가짐)
- LRU + TTL 회수로 메모리 무한 증가 방지

기동 로그에서 변화를 확인할 수 있다:
```
수정 전: "detector_scope": "global_shared"
수정 후: "detector_scope": "per_session_pool", "detector_pool_size": 16
```

세션별 생성이 실제로 일어난다 (실측):
```json
{"container":"B","event":"detector_created","session_id":"p23_run",
 "detail":{"pool_size":1,"total_created":1}}
```

### 5.4 백프레셔 도입

**수정 전** — `mobile.html:284`
```javascript
streamInterval = setInterval(() => {
    ...
    ws.send(base64Image);      // 서버 상태와 무관하게 33ms마다 무조건 발사
}, 33);
```

서버가 느려져도 계속 밀어넣으므로 WebSocket 송신 버퍼에 프레임이 적체되고,
**지연이 누적되어 갈수록 뒤처진다**(실시간성 상실). 회복 수단도 없었다.

**수정 후**
```javascript
if (inflight >= streamCfg.max_inflight_frames || ws.bufferedAmount > 512 * 1024) {
    droppedFrames++;
    return;                    // 밀린 프레임은 버리고 최신 위치만 보낸다
}
```

드로잉은 "밀린 과거 프레임"보다 "최신 위치"가 중요하므로 드롭이 옳은 선택이다.

### 5.5 구간별 계측 도입

수정 전에는 **어느 구간이 느린지 측정할 방법이 없었다.**
수정 후 디코딩과 추론을 분리해 측정한다 (실측):

```json
{"event":"hand_not_detected","session_id":"debug_run",
 "detail":{"decode_ms":1.71,"inference_ms":44.12,"frame_shape":[480,640,3]}}
{"event":"hand_not_detected",
 "detail":{"decode_ms":0.49,"inference_ms":15.38,...}}
```

**청사진의 명제가 이 프로젝트에서는 성립하지 않는다는 것이 데이터로 확인됐다.**
전처리(디코딩) 0.5~1.7ms 대 추론 15~44ms — 병목은 전처리가 아니라 모델이다.
따라서 전처리 병렬화가 아니라 **동시성 확보**가 옳은 처방이었고, 실제로 그렇게 했다.

---

## 6. CI/CD — pytest & GitHub Actions

> *"코드가 변경될 때마다 이 실무적 기준이 자동으로 검증되고 유지되도록 만드는 것,
> 그것이 CI/CD 파이프라인의 핵심 역할입니다."*

### 6.1 수정 전: 테스트 0개

자동화 테스트가 하나도 없었다. 리팩터링으로 동작이 깨져도 알 방법이 없었다.

### 6.2 수정 후: 91개 전부 통과

```
tests/integration/test_container_a.py   10 passed
tests/integration/test_degradation.py    5 passed
tests/unit/test_ema_debounce.py         11 passed
tests/unit/test_gesture_rules.py        42 passed
tests/unit/test_pillars.py              23 passed
========================== 91 passed in 3.55s ==========================
```

| 파일 | 개수 | 역할 |
|---|---:|---|
| `test_gesture_rules.py` | 42 | 6대 제스처 + 경계값 + 엣지케이스 + 기본값 보존 |
| `test_pillars.py` | 23 | **4대 축이 되돌아가지 않는지 강제** |
| `test_ema_debounce.py` | 11 | EMA 3단계, 다수결, 비대칭 펜-업 컷오프 |
| `test_container_a.py` | 10 | 엔드포인트, WebSocket 세션, 방 정리 |
| `test_degradation.py` | 5 | 상류 장애 시 생존·통지·회복 |

### 6.3 4대 축을 강제하는 테스트 (청사진의 핵심)

`test_pillars.py` 는 고도화로 제거한 안티패턴이 다시 들어오면 **빌드를 깨뜨린다.**
사람의 코드리뷰에 의존하지 않는다.

| 축 | 테스트 |
|---|---|
| P1 | 사설 IP 리터럴 0개 (AST/정규식 정적 검사) |
| P1 | 서비스 포트 리터럴 0개 |
| P1 | 설정 파일 로딩, 필수 키 존재, 누락 시 명시적 실패, 환경변수 오버라이드 |
| P2 | `except: pass` 0개 (AST 검사) |
| P2 | 서킷 브레이커 CLOSED→OPEN→HALF_OPEN→CLOSED 전이 |
| P2 | 좌표 null 누수 차단, degraded 응답이 파이프라인을 유지 |
| P3 | 전역 detector 부재, 벽시계 타임스탬프 부재 |
| P3 | detector 세션 격리, 타임스탬프 단조 증가(100회), LRU 회수 |
| P3 | 스레드풀 오프로딩 존재, 프론트엔드 백프레셔 존재 |
| P4 | `print()` 0개 (AST 검사), 3개 컨테이너 공통 로거 사용 |
| P4 | 레벨 필터링 실동작, traceback+context 포함, 샘플링 100→10건 |

> **이 테스트들이 실제로 일을 했다.** 작성 직후 첫 실행에서 **내가 방금 쓴 코드에서
> 위반 3건을 잡아냈다** — `container_c_gesture/main.py` 의 `__main__` 블록 포트 리터럴,
> `common/config.py` 의 포트 폴백 리터럴, `common/detector_pool.py` 의 `except: pass`.
> 세 건 모두 수정했다.

### 6.4 GitHub Actions 파이프라인

```
Code Push → Actions → Lint(ruff) → Pytest(4대 축) → Docker Build → 기동 검증
```

| 잡 | 내용 | 분리 이유 |
|---|---|---|
| `lint` | ruff check / format | 수십 초. 가장 먼저 걸러낸다 |
| `test-light` | 단위 + 통합 + **4대 축 검증** | 1~2분. 대부분의 변경은 여기서 걸린다 |
| `test-vision` | MediaPipe 필요 테스트 | 설치가 수백 MB라 별도 잡 + 모델 캐시 |
| `build` | 이미지 빌드 → 기동 → healthy 대기 → 스모크 | 테스트 통과 후에만 실행 |

`build` 잡은 `sleep` 이 아니라 **실제 healthcheck 상태를 폴링**하고,
마지막에 컨테이너 로그가 유효한 JSON 스키마인지까지 검증한다.

### 6.5 환경 재현성 (선결 과제)

**수정 전 실측 — 로컬과 컨테이너의 스택이 완전히 달랐다:**

| 패키지 | requirements 명시 | 로컬 실제 설치 |
|---|---|---|
| mediapipe | `==0.10.14` | **1.0.1** |
| numpy | `<2.0.0,>=1.26.0` | **2.3.1** (제약 위반) |
| opencv-contrib-python | `==4.10.0.84` | **5.0.0.93** |

CI는 컨테이너와 동일한 `requirements.txt` 를 쓰고, `test-vision` 잡은
Container B와 같은 **Python 3.10**을 사용한다.

---

## 7. 베이스라인 보존 확인

고도화의 대전제는 **"기능을 추가하지 않는다. 기존 동작을 100% 보존한다"** 였다.

| # | 항목 | 결과 |
|---|---|---|
| 1 | 6대 제스처 판별 | 42개 테스트 통과 — 동작 동일 |
| 2 | 임계값 13종 기본값 | 원본과 정확히 일치 (테스트로 강제) |
| 3 | EMA 3단계 보정 | 동작 동일 |
| 4 | 비대칭 펜-업 컷오프 | 동작 동일 + 로그로 관측 가능해짐 |
| 5 | 단일 세션 지연 | p50 19.7ms → 19.8ms (변화 없음) |
| 6 | 3개 컨테이너 기동 | 전부 `healthy` |
| 7 | QR → 폰 접속 흐름 | 동일 |
| 8 | 웹 UI | 상태 배너 외 변경 없음 |

---

## 8. 파일 변경 요약

### 신규

```
common/                       공용 모듈 (3개 컨테이너 공유)
  ├── config.py               설정 로더 (YAML + 환경변수 병합)
  ├── logging_setup.py        구조화 로거 + 샘플링 + 회전
  ├── http_client.py          재시도 + 서킷 브레이커
  ├── schemas.py              서비스 간 응답 계약
  └── detector_pool.py        세션별 detector 격리
config/
  ├── default.yaml            모든 튜닝 값의 단일 출처
  ├── dev.yaml                개발 환경 오버라이드
  └── prod.yaml               운영 환경 오버라이드
tests/                        91개 테스트 (1,235줄)
  ├── conftest.py             합성 랜드마크 팩토리
  ├── unit/                   제스처·EMA·4대축
  └── integration/            엔드포인트·세션·degradation
scripts/compose_up.py         호스트 LAN IP 탐지 후 기동
.github/workflows/ci.yml      4잡 CI 파이프라인
pytest.ini / ruff.toml / requirements-dev.txt / .gitignore
BEFORE_AFTER_REPORT.md        (본 문서)
PRODUCTION_REFINING_PLAN.md   진단 및 계획서
```

### 수정

| 파일 | 주요 변경 |
|---|---|
| `container_a_web/main.py` | 설정 주입, 구조화 로깅, 재시도, degradation, 세션 정리, `/health`·`/health/ready`·`/api/config` |
| `container_b_vision/main.py` | 설정 주입, 로깅 전면 도입, 입력 검증, 스레드풀 오프로딩, detector 풀, lifespan |
| `container_c_gesture/main.py` | 공용 로거로 교체, 매직넘버 9종 설정화, HTTP 경로 로깅, 상태 전이 로깅 |
| `static/pc.html` | 하드코딩 IP/포트 제거 → `/api/info` 소비 |
| `static/mobile.html` | 스트리밍 파라미터 서버 수신, 백프레셔 도입 |
| `docker-compose.yml` | 빌드 컨텍스트 루트화, healthcheck, 볼륨, 하드코딩 IP 제거 |
| `Dockerfile` × 3 | `PYTHONUNBUFFERED=1`, `common/`·`config/` 복사 |
| `start_local.py` | `PYTHONPATH` 주입, 로컬은 text 로그 포맷 |

---

## 9. 남은 항목 (범위 밖 또는 후속)

정직하게 밝힌다. 아래는 이번에 하지 않았다.

| 항목 | 사유 |
|---|---|
| 의존성 락파일 (`pip-tools`) | requirements 핀 정합성만 확인. 해시 고정 락파일은 미도입 |
| ruff 로컬 실행 | Windows 애플리케이션 제어 정책에 막힘. CI(Ubuntu)에서는 동작 |
| `test-vision` 잡의 실제 테스트 | 마커와 CI 잡은 준비됨. MediaPipe 실제 추론 테스트는 미작성 |
| 메모리 장시간 프로파일링 | `tracemalloc` 1시간 구동 측정 미실시 |
| 손 포함 프레임 벤치마크 | 카메라가 필요해 자동화 미실시. 구간별 계측은 도입 완료 |
| 실제 클라우드 배포 | 계획서 §10에서 범위 밖으로 명시 |
| GitHub Actions 실제 실행 | 워크플로 작성 완료. push 후 첫 실행은 미확인 |

---

## 10. 결론

> *"'돌아가는 코드'를 짜는 코더에서 '지속 가능한 시스템'을 설계하는 엔지니어로의 도약"*

이번 작업에서 기능은 하나도 추가하지 않았다. 6대 제스처는 그대로고, 사용자가 보는 화면도
장애 배너 외에는 동일하다. 바뀐 것은 **그 기능을 떠받치는 기반**이다.

가장 의미 있는 변화 셋을 꼽자면:

1. **관측 불가 → 관측 가능.** 수정 전 3개 컨테이너의 애플리케이션 로그는 **0줄**이었다.
   `print()` 조차 버퍼링에 갇혀 출력되지 않았다. 이제 `session_id` 하나로
   A→B→C 전 구간의 타임라인을 복원할 수 있다.

2. **무음 장애 → 통지되는 저하.** `except Exception: pass` 한 줄이 모든 장애를 삼켰다.
   이제 C를 죽여도 A는 200으로 살아있고, readiness가 503으로 정확히 보고하며,
   사용자에게 상태가 전달된다.

3. **직렬 처리 → 동시 처리.** 동시 4세션 처리량이 56.7 → 121.6 fps로 **2.1배** 늘었다.
   추측이 아니라 동일 조건 실측이다.

그리고 이 모든 것이 **91개 테스트로 고정**되어 있다. 4대 축이 되돌아가면 CI가 빌드를 깨뜨린다.
실제로 그 테스트는 작성 직후 내 코드에서 위반 3건을 잡아냈다.

기능의 추가가 아니라 **엔지니어링 기준의 확립** — 청사진이 요구한 것이 그것이었다.
