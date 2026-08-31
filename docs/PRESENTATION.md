# Air Canvas — 프로덕션 레벨 고도화 기술 상세

> README가 "무엇을 얻었는가"라면, 이 문서는 **"어떻게 진단했고 왜 그렇게 설계했는가"** 를 다룬다.
> 결과 요약은 [README](../README.md), 전체 실측 비교는 [BEFORE_AFTER_REPORT](BEFORE_AFTER_REPORT.md) 참고.

| 항목 | 내용 |
|---|---|
| 기준 문서 | `Production_Engineering_Blueprint.pptx` — 4대 엔지니어링 축 + CI/CD |
| 베이스라인 | `a6b2596` (PoC 완성 시점) |
| 변경 규모 | 38 files, +5,903 / −199 |
| 작업 원칙 | 기능 추가 없음. 6대 제스처 동작 100% 보존 |

---

## 1. 작업의 전제

### 1.1 무엇을 하는 시스템인가

스마트폰 카메라로 허공에 손을 움직이면 PC 캔버스(6000×4500px)에 실시간으로 그림이 그려진다.

![아키텍처 데이터 흐름도](images/architecture.png)

데이터가 3개 컨테이너를 거치며 **압축**되는 것이 이 아키텍처의 핵심 설계 의도다.

| 구간 | 데이터 | 크기 |
|---|---|---:|
| 폰 → A | base64 JPEG (480×360, q=0.6) | **≈ 7 KB** |
| A → B | 동일 (릴레이) | ≈ 7 KB |
| B → C | 21개 관절 정규화 좌표 | **≈ 500 B** |
| C → A → PC | `action · x · y · delta` | ≈ 100 B |

무거운 픽셀이 오가는 **A↔B 구간이 가장 비싼 구간**이며, 성능 작업의 초점도 여기였다.

### 1.2 보존해야 할 것

고도화의 대전제는 *"기능을 추가하지 않는다. 기존 동작을 100% 보존한다"* 였다.
따라서 아래는 **변경 금지 대상**으로 못 박고 시작했다.

| 규칙 | 조건 | 산출 |
|---|---|---|
| DRAW | 검지 tip이 pip보다 위 + 나머지 4개 접힘 | 검지끝 좌표 |
| ERASE | 검지+중지 폄, 두 손끝 높이차 `< 0.12` | 두 손끝 중점 |
| ZOOM_IN | 주먹 + 엄지만 폄 (엄지–손바닥 거리 `> 0.15`) | delta `+0.008` |
| ZOOM_OUT | 주먹 + 새끼만 폄 | delta `−0.008` |
| PAN | 4손가락 접힘 + 엄지 접힘 (`< 0.13`) | 손바닥 중심 이동량 |
| HOVER | 그 외 전부 | 검지끝 좌표 |

여기에 **속도 적응형 EMA 3단계**, **3프레임 다수결 디바운스**,
**비대칭 무지연 펜-업 컷오프**까지 신호처리 로직 전부가 보존 대상이었다.

> 특히 비대칭 컷오프는 `DRAW → 비DRAW` 전이에서만 다수결을 건너뛰고 즉시 전환한다.
> 획 끝의 '삐침'을 막기 위한 **의도된 비대칭**이며, "일관성 있게" 고치면 드로잉 품질이 나빠진다.
> 이 사실을 모르는 사람이 나중에 정리하려 들 수 있어 테스트로 못 박았다.

---

## 2. 진단 — 무엇이 문제였나

기능은 완벽히 동작했다. 문제는 **운영 관점에서 봤을 때** 드러났다.
진단 결과 26건을 4대 축으로 분류했고, 그중 심각도가 높은 것들이 아래다.

| ID | 축 | 문제 | 왜 심각한가 |
|---|---|---|---|
| P1-1 | 파라미터화 | 사설 IP가 3곳에 하드코딩 | 다른 네트워크로 옮기면 QR이 죽음 |
| P2-1 | 예외 처리 | `except Exception: pass` 1곳 | 모든 장애를 무음 처리 |
| P2-3 | 예외 처리 | Graceful Degradation 부재 | 상류 장애 시 전체 정지 + 무통보 |
| P3-1 | 성능 | 이벤트 루프 블로킹 | 동시 접속 시 처리량 붕괴 |
| P3-2 | 성능 | 전역 detector + VIDEO 모드 | 세션 간 추적 상태 오염 |
| P4-1~3 | 로깅 | 3개 컨테이너 사실상 무로깅 | 장애 원인 추적 불가 |
| CI-0~9 | CI/CD | 테스트 0개, CI 없음 | 리팩터링 안전망 없음 |

### 2.1 진단이 "추측"이 되지 않게 한 방법

코드 리뷰만으로는 "이럴 것 같다"에 그친다. 그래서 **베이스라인을 실제로 띄워 측정**했다.

```bash
# 고도화 이전 커밋을 별도 워크트리로 꺼내 독립 프로젝트로 기동
git worktree add /tmp/baseline a6b2596
cd /tmp/baseline && docker compose -p aircanvas-baseline up --build -d
```

이렇게 얻은 것들:

- QR PNG를 디코딩해 **실제로 어떤 주소가 들어가는지** 확인 → `https://192.168.55.208:8443/...`
- 동일 시나리오(정상 40프레임 + 손상 3프레임)를 돌려 **로그가 몇 줄 남는지** 확인 → **0줄**
- 동일 프레임으로 **단일/동시 4세션 벤치마크** 측정 → 성능 비교의 기준선 확보

> 이 과정에서 예상 밖의 사실도 나왔다.
> Container A의 `print()` 4개가 `docker logs` 에 **아예 출력되지 않고 있었다.**
> stdout 블록 버퍼링 때문에 버퍼에 갇힌 것이다. uvicorn 로그는 `StreamHandler` 가
> emit마다 flush해서 보였을 뿐이었다. 즉 A의 로깅은 "구조화되지 않았다"가 아니라
> **사실상 존재하지 않았다.**

---

## 3. Pillar 1 — 파라미터화

### 3.1 문제의 구조

같은 값이 여러 계층에 중복 기재되어 있었다. `192.168.55.208` 하나가 3곳이었다.

| 계층 | 위치 |
|---|---|
| 파이썬 | `container_a_web/main.py:20` |
| 프론트엔드 | `container_a_web/static/pc.html:294` |
| 오케스트레이션 | `docker-compose.yml` |

포트는 더 심해서 코드·Dockerfile·compose·entrypoint·HTML 5개 계층 8곳에 흩어져 있었다.
**포트 하나를 바꾸려면 8곳을 동시에 고쳐야 했고, 하나라도 놓치면 조용히 깨졌다.**

제스처 임계값 9종도 코드에 직접 박혀 있어, 튜닝하려면 코드 수정 + 이미지 재빌드가 필요했다.

### 3.2 설계 — 계층형 설정

```
config/default.yaml  →  config/{APP_ENV}.yaml  →  환경변수
```

우선순위를 3단으로 둔 이유:

- `default.yaml` — **기본값은 고도화 이전 하드코딩 값과 정확히 동일**하게 유지. 베이스라인 보존
- `{APP_ENV}.yaml` — dev는 `DEBUG`/`text` 로그, prod는 `INFO`/`json` + 파일 회전
- 환경변수 — Docker/CI에서 주입해야 하므로 최우선

환경변수는 두 가지 형태를 지원한다.

```bash
# 중첩 경로 지정
AIRCANVAS__GESTURE__EMA__ALPHA_FAST=0.9

# 레거시 별칭 (기존 docker-compose.yml 을 깨뜨리지 않기 위해 유지)
HOST_IP=192.168.0.10
```

레거시 별칭을 남긴 것은 **이번 리팩터링이 기존 배포 방식을 깨뜨리지 않게** 하기 위해서다.
리팩터링이 "돌아가던 걸 못 돌아가게" 만들면 그건 개선이 아니다.

### 3.3 까다로웠던 지점 — LAN IP 자동 탐지

`host_ip: auto` 로 두고 컨테이너 안에서 탐지하면 **Docker 브리지 주소(172.x)** 가 잡힌다.
그 주소를 QR에 넣으면 폰이 접속할 수 없다. 그렇다고 IP를 코드에 박으면 원점 회귀다.

**해법: 탐지는 호스트에서, 주입은 환경변수로.**

```python
# scripts/compose_up.py
def detect_lan_ip(fallback: str = "127.0.0.1") -> str:
    """UDP 소켓의 로컬 바인딩 주소로 LAN IP 탐지. 실제 패킷은 전송되지 않는다."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return fallback
```

UDP `connect()` 는 패킷을 보내지 않고 **커널 라우팅 테이블만 조회**해 로컬 주소를 확정한다.
외부 통신 없이 "8.8.8.8로 나가려면 내 어느 인터페이스를 쓰는가"를 알아내는 관용적 기법이다.

그럼에도 값이 컨테이너 내부 대역이면 명시적으로 경고를 남긴다.

```python
# container_a_web/main.py
if _is_container_internal_ip(DEFAULT_HOST_IP):
    log.warning("host_ip_unusable", detail={
        "resolved_ip": DEFAULT_HOST_IP,
        "reason": "컨테이너 내부 주소라 폰에서 접속 불가",
        "fix": "scripts/compose_up.py 로 기동하거나 HOST_IP 환경변수를 지정하세요",
    })
```

**조용히 잘못된 값을 쓰는 것보다, 시끄럽게 알리는 편이 낫다.**

### 3.4 검증

```python
# tests/unit/test_gesture_rules.py
BASELINE_VALUES = {
    "THUMB_OPEN_PALM_DIST": 0.15,
    "ERASE_HEIGHT_DIFF": 0.12,
    "ZOOM_STEP": 0.008,
    "EMA_ALPHA_MICRO": 0.35,
    # ... 총 13종
}

@pytest.mark.parametrize("name,expected", sorted(BASELINE_VALUES.items()))
def test_baseline_default_values_preserved(gesture_module, name, expected):
    """설정 기본값이 고도화 이전 하드코딩 값과 정확히 동일한지."""
    assert getattr(gesture_module, name) == expected
```

임계값 13종을 전부 고정했다. 누군가 `default.yaml` 을 잘못 건드리면 CI가 깨진다.

---

## 4. Pillar 2 — 예외 처리와 복구 탄력성

### 4.1 한 줄이 만든 사각지대

```python
# container_a_web/main.py:126 (수정 전)
            except Exception:
                pass
```

이 한 줄이 삼킨 것들:

- B 컨테이너 다운 (`ConnectError`)
- 타임아웃 초과 (`TimeoutException`) — 당시 설정이 **0.2초**라 상시 발생 가능
- JSON 파싱 실패
- PC WebSocket이 이미 끊긴 상태에서의 전송 실패
- 코드 버그로 인한 `AttributeError` / `KeyError`

게다가 `status_code != 200` 을 처리하는 `else` 분기가 **아예 없어서**,
B가 500을 반환해도 정상 프레임과 구분되지 않았다.

사용자에게는 "그림이 안 그려진다"로만 나타나고, 로그에는 아무 흔적이 없다.
**청사진 Pillar 4가 말하는 "책임 소재를 밝히는 법적 흔적"이 원천적으로 소실되는 지점이다.**

### 4.2 재시도 정책 — 일반 웹과 다르게 설계한 이유

일반적인 HTTP 재시도는 "3초 대기 후 재시도, 5회까지" 같은 형태다.
**30fps 실시간 파이프라인에서는 이게 독이 된다.**

33ms마다 새 프레임이 도착하는데 실패한 프레임을 몇 초씩 붙잡으면,
그 사이 도착한 프레임 수십 장이 큐에 쌓여 지연이 누적된다.
회복되어도 밀린 프레임을 처리하느라 실시간성이 돌아오지 않는다.

| 항목 | 값 | 근거 |
|---|---|---|
| 최대 재시도 | 2회 | 일시적 흔들림만 흡수. 그 이상은 무의미 |
| 백오프 | 50ms | 프레임 주기(33ms)와 같은 규모 |
| **프레임 예산** | 66ms | 프레임 주기의 2배. 초과 시 **재시도 포기하고 프레임 폐기** |
| 서킷 임계 | 연속 10회 | 죽은 대상을 계속 두드리지 않음 |
| 서킷 복구 | 5초 후 탐침 | 영구 차단 방지 |

```python
# common/http_client.py
for attempt in range(1, self._max_attempts + 1):
    if deadline and time.monotonic() >= deadline:
        # 예산 초과. 더 시도하면 뒤따르는 프레임까지 밀린다.
        last_reason = "BUDGET_EXCEEDED"
        break
    ...
    # 5xx는 재시도할 가치가 있지만 4xx는 다시 보내도 같은 결과다.
    if resp.status_code < 500:
        break
```

**"빨리 포기하는 것"이 실시간 시스템에서는 올바른 전략이다.**

### 4.3 서킷 브레이커 상태 전이

```
CLOSED ──연속 10회 실패──▶ OPEN ──5초 경과──▶ HALF_OPEN ──성공──▶ CLOSED
                                                   └──실패──▶ OPEN (타이머 리셋)
```

HALF_OPEN에서는 **요청 하나만** 통과시킨다. 여러 개를 보내면 회복 중인 서버를 다시 무너뜨린다.

```python
def allow(self) -> bool:
    current = self.state
    if current == "CLOSED":  return True
    if current == "OPEN":    return False
    # HALF_OPEN: 탐침 요청 하나만 통과시킨다
    if self.half_open_in_flight:  return False
    self.half_open_in_flight = True
    return True
```

### 4.4 Graceful Degradation — 무엇을 살리고 무엇을 포기할까

상류가 죽었을 때 "전부 죽는다"와 "아무 일 없는 척한다" 사이에서 선택해야 한다.
이 시스템에서는 **커서는 살리고 그리기만 포기**하는 것이 맞다고 판단했다.

- 랜드마크는 B에서 이미 확보했으므로 PC는 손 스켈레톤과 커서를 계속 그릴 수 있다
- 제스처 판별(C)만 불가하므로 `HOVER` 로 고정한다
- 사용자에게 상태를 통지해 "내 손동작이 잘못됐나?" 하는 오해를 막는다

```python
# common/schemas.py
@classmethod
def degraded(cls, reason, landmarks=None, health=HealthState.DEGRADED):
    return cls(
        success=True,                              # 파이프라인 자체는 살아 있다
        action="HOVER" if landmarks else "NONE",   # 커서는 유지
        landmarks=landmarks or [],
        health=health, error=reason,
    )
```

**중요한 부수 조건** — degradation 중에도 모바일에 `FEEDBACK` 을 계속 돌려줘야 한다.
그러지 않으면 클라이언트의 백프레셔 카운터(in-flight)가 잠겨 스트리밍이 영구 정지한다.
장애 대응 코드가 새로운 장애를 만드는 전형적인 함정이다.

### 4.5 헬스체크는 전이적이어야 한다

A가 B의 `/health`(자기 자신만 확인)를 부르면 **C가 죽어도 A는 ready로 보고한다.**
그래서 A는 B의 `/health/ready` 를 부르고, B는 다시 C를 확인하게 했다.

```
A /health/ready  →  B /health/ready  →  C /health
     503                  503              (다운)
```

**실측 검증** — `docker stop air-canvas-gesture`:

```json
{"status":"degraded","checks":{"vision_chain":{"ok":false,"status_code":503,
 "upstream":{"status":"degraded","circuit":"CLOSED","reason":"TIMEOUT"}}}}
```

동시에 A의 `/health` 는 200, 웹페이지도 200을 유지했다. **A는 살아 있고, 상태만 정확히 보고한다.**

함정이 하나 있었다. 헬스체크에 프레임용 타임아웃(1.5초)을 쓰면
B가 C를 확인하고 재시도할 시간을 못 기다려 **정상 상황에서도 오탐**이 난다.
헬스체크 전용으로 10초를 따로 뒀다.

### 4.6 응답 계약의 부재

수정 전 PC가 실제로 수신하던 페이로드:

```json
{"type":"GESTURE","action":"NONE","x":null,"y":null,"delta":0,"landmarks":[]}
```

B가 손 미검출 시 `x`/`y` 키 없이 응답하고, A가 `result.get("x")` 로 꺼내
그대로 릴레이한 결과다. 프론트 JS가 우연히 견디고 있었을 뿐,
누군가 `x.toFixed()` 를 호출하는 순간 런타임 에러였다.

3개 컨테이너가 같은 Pydantic 모델을 공유하도록 바꾸고, 정규화 함수를 뒀다.

```python
# common/schemas.py
@classmethod
def from_upstream(cls, raw, session_id=""):
    """키 누락, None, 타입 불일치를 전부 흡수해 항상 유효한 결과를 만든다."""
    if not isinstance(raw, dict):
        return cls.neutral(session_id)

    def _num(key, default):
        value = raw.get(key)
        if value is None:  return default
        try:               return float(value)
        except (TypeError, ValueError):  return default
    ...
```

---

## 5. Pillar 3 — 동시성과 성능

### 5.1 두 개의 문제, 그리고 순서

두 문제가 얽혀 있었고 **고치는 순서가 중요했다.**

**문제 A — 이벤트 루프 블로킹**

```python
# container_b_vision/main.py (수정 전)
@app.post("/analyze")
async def analyze_frame(payload: FramePayload):
    image = decode_base64_frame(payload.image)         # 동기 CPU
    detected, landmarks = extract_landmarks(image)     # 동기 CPU (MediaPipe 추론)
```

uvicorn의 이벤트 루프는 단일 스레드다. 추론이 도는 동안 **워커의 이벤트 루프 전체가 정지**한다.
다른 세션의 요청은 물론 헬스체크 응답조차 처리되지 않는다.

**문제 B — 전역 detector 공유 + 벽시계 타임스탬프**

```python
hands_detector = mp_vision.HandLandmarker.create_from_options(
    ..., running_mode=mp_vision.RunningMode.VIDEO)     # 전역 1개, 상태를 가지는 모드
...
timestamp_ms = int(time.time() * 1000)                 # 단조 증가 미보장
```

`RunningMode.VIDEO` 는 이전 프레임의 추적 결과를 다음 프레임에 활용한다.
그것이 부드러운 트래킹의 원리지만, 동시에 **detector가 상태를 가진다**는 뜻이다.
전역 인스턴스 하나를 모든 세션이 공유했으므로 사용자 2명이 서로의 추적 상태를 오염시킨다.

### 5.2 왜 B를 먼저 고쳐야 했는가

**수정 전 코드에서 동시 2세션을 돌렸을 때 타임스탬프 예외는 재현되지 않았다.**

이유가 중요하다. 수정 전 코드는 이벤트 루프를 블로킹하므로 **호출이 완전히 직렬화**된다.
호출 간격이 약 70ms라 같은 밀리초에 두 프레임이 들어갈 일이 없었다.

> 즉 이것은 **"지금 터지는 버그"가 아니라 "스레드풀을 넣는 순간 터지는 잠재 버그"** 였다.
> 문제 A만 먼저 고쳤다면 새로운 장애를 만들었을 것이다.

실제 MediaPipe로 이 위험을 증명하는 테스트를 남겼다.

```python
# tests/vision/test_detector_isolation.py
def test_wallclock_timestamp_is_actually_unsafe(detector_factory, frame):
    detector.detect_for_video(frame, 1000)
    with pytest.raises(Exception) as exc_info:
        detector.detect_for_video(frame, 1000)   # 같은 타임스탬프 재사용
    assert any(k in str(exc_info.value).lower()
               for k in ("timestamp", "monotonic", "increas"))
```

### 5.3 DetectorPool 설계

```python
# common/detector_pool.py
def __enter__(self):
    self._entry.lock.acquire()
    # 프레임 간 간격을 33ms(≈30fps)로 가정한다.
    # 실제 값이 아니어도 무방하다. VIDEO 모드가 요구하는 것은 "단조 증가"뿐이다.
    timestamp_ms = self._entry.next_timestamp_ms
    self._entry.next_timestamp_ms += 33
    return self._entry.detector, timestamp_ms
```

설계 요점 네 가지:

1. **세션별 detector 인스턴스** — 추적 상태가 섞이지 않는다
2. **세션별 단조 증가 카운터** — 벽시계를 쓰지 않으므로 역전이 원천적으로 불가능하다
3. **세션 내 락으로 직렬화** — VIDEO 모드는 프레임 순서가 의미를 가지므로 같은 세션은 순차 처리
4. **LRU + TTL 회수** — 세션마다 모델을 만들면 메모리가 무한 증가한다

```yaml
vision:
  detector_pool_size: 8       # prod 16
  detector_idle_ttl_s: 120    # 2분 무활동 시 회수
```

> `detector_idle_ttl_s`(120초)와 Container C의 `gesture.session.ttl_s`(600초)는
> **서로 다른 값**이다. 전자는 무거운 AI 모델 인스턴스를, 후자는 가벼운 EMA 상태를 관리한다.

### 5.4 백프레셔

```javascript
// 수정 전 — mobile.html
setInterval(() => { ws.send(base64Image); }, 33);   // 서버 상태 무관하게 발사
```

서버가 느려져도 클라이언트가 계속 밀어넣으면 송신 버퍼에 프레임이 적체된다.
지연이 누적되어 갈수록 뒤처지고, 회복 수단이 없다.

```javascript
// 수정 후 — in-flight 제한 + 버퍼 감시
if (inflight >= streamCfg.max_inflight_frames || ws.bufferedAmount > 512 * 1024) {
    droppedFrames++;
    return;   // 밀린 프레임은 버리고 최신 위치만 보낸다
}
```

**드로잉은 "밀린 과거 프레임"보다 "최신 위치"가 중요하다.** 그래서 드롭이 옳은 선택이다.

### 5.5 측정 결과와 해석

```
[단일 세션 60프레임]
  Before  avg 20.5ms  p50 19.7ms  p95 21.8ms
  After   avg 22.1ms  p50 19.8ms  p95 24.7ms

[동시 4세션 240프레임]
  Before  avg 69.9ms  p50 69.1ms  p95 74.4ms   처리량  56.7 fps
  After   avg 31.8ms  p50 24.9ms  p95 47.6ms   처리량 121.6 fps
```

두 가지를 짚어야 한다.

**① 단일 세션은 개선되지 않았다.** 오히려 평균이 1.6ms 늘었다.
추가된 로깅·검증·스키마 정규화 비용이다. 관측 가능성을 얻는 대가로 타당하다고 판단했다.
개선은 **동시 접속 상황에만** 나타난다. 블로킹 해소가 원인이기 때문이다.

**② 수정 전 수치가 직렬화의 증거다.**
4세션 p50이 69.1ms인데 단일 세션은 19.7ms다. `19.7 × 4 ≈ 78.8ms` 에 근접한다는 것은
요청이 **완전히 순차 처리**되고 있었다는 뜻이다. 수정 후 24.9ms는 병렬 처리가 실제로
일어났음을 보여준다.

### 5.6 계측이 뒤집은 가정

청사진 Pillar 3의 명제는 *"모델보다 전처리가 지연을 더 잡아먹는 일은 없어야 한다"* 였다.
구간별로 나눠 재보니 **이 프로젝트에서는 애초에 그런 일이 없었다.**

```json
{"event":"hand_not_detected","detail":{"decode_ms":1.71,"inference_ms":44.12}}
{"event":"hand_not_detected","detail":{"decode_ms":0.49,"inference_ms":15.38}}
```

디코딩 0.5~1.7ms 대 추론 15~44ms. 병목은 전처리가 아니라 모델이다.
따라서 **전처리 병렬화가 아니라 동시성 확보**가 옳은 처방이었고, 실제로 그렇게 했다.

> 계측 없이 청사진 문구만 따랐다면 전처리를 멀티프로세싱으로 쪼개느라
> 시간을 쓰고 성능은 그대로였을 것이다. **측정이 설계를 바꿨다.**

---

## 6. Pillar 4 — 구조화 로깅

### 6.1 출발점

| 컨테이너 | 수정 전 |
|---|---|
| A | `print()` 4곳 — **버퍼링으로 `docker logs` 에 출력조차 안 됨** |
| B | `import logging` 만 하고 한 줄도 사용 안 함 |
| C | 구조화 JSON 로깅 보유 — 단, **쓰이지 않는 WebSocket 경로에만** |

C의 사례가 특히 시사적이다. `log_event()` 구현 자체는 쓸 만했지만
**관측되던 경로(WebSocket)와 실제로 쓰이던 경로(HTTP `/gesture`)가 서로 달랐다.**
"로깅이 있다"와 "관측이 된다"는 다른 문제다.

### 6.2 공통 로거로 승격하며 함께 고친 결함

C의 구현을 `common/logging_setup.py` 로 옮기면서 아래를 해결했다.

| 결함 | 수정 |
|---|---|
| `level` 인자를 받고도 항상 `logger.info()` 호출 → 필터링 무의미 | 실제 로거 메서드에 매핑 |
| 컨테이너 이름 `"C"` 하드코딩 | 초기화 시 주입 |
| stdout 전용 → 재시작 시 로그 소실 | `RotatingFileHandler` (10MB × 5) + 볼륨 |
| traceback을 남길 방법 없음 | `exception()` 이 자동 첨부 |
| 30fps 로그를 전량 INFO로 기록 | `sampled()` 도입 |

추가로 `PYTHONUNBUFFERED=1` 을 Dockerfile에 넣어 버퍼링 문제를 근본 차단했다.

### 6.3 표준 스키마

```python
payload = {
    "ts":         epoch_ms,       # 기계 정렬용
    "time":       iso8601,        # 사람이 읽는 용도
    "container":  "A"|"B"|"C",
    "level":      levelname,
    "event":      snake_case,     # 문장이 아니라 식별자
    "session_id": ... or "-",     # 세션 단위 추적
    "trace_id":   ...,            # 프레임 단위 추적
    "detail":     {...},
}
if record.exc_info:
    payload["error"] = {"type": ..., "message": ..., "traceback": ...}
```

`event` 를 문장이 아닌 **snake_case 식별자**로 강제한 것이 핵심이다.
`"PC 캔버스 연결됨!"` 대신 `"pc_connected"` 를 쓰면 집계·필터링·알람 연동이 가능해진다.
사람이 읽을 설명은 `detail` 로 보낸다.

### 6.4 샘플링 — 무엇을 버리고 무엇을 남길까

30fps × 세션 수만큼 초당 로그가 발생한다. 그대로 두면 디스크와 성능을 동시에 잡아먹는다.
그렇다고 전부 끄면 관측이 안 된다.

```python
def sampled(self, event, *, level=logging.DEBUG, **fields):
    """
    첫 발생은 항상 기록한다. 새로운 이벤트가 나타났다는 사실 자체가 정보이며,
    N회를 기다렸다가 남기면 저빈도 이벤트는 영영 보이지 않기 때문이다.
    에러 레벨에는 사용하지 않는다. 장애는 전량 기록해야 한다.
    """
    with self._lock:
        count = self._counters.get(event, 0)
        self._counters[event] = count + 1
        should_log = (count % self._sample_rate) == 0

    if should_log:
        fields["sampled_every"] = self._sample_rate
        fields["occurrence"] = count + 1   # 1건이 N건을 대표한다는 사실 보존
        self._emit(level, event, session_id, trace_id, None, fields)
```

세 가지 판단이 들어 있다.

1. **첫 발생은 무조건 기록** — 새 이벤트의 등장 자체가 정보다
2. **`occurrence` 를 함께 남김** — "1건 보였다"가 실제로는 90건이었음을 잃지 않는다
3. **에러는 샘플링 금지** — 장애를 확률적으로 놓치면 안 된다

### 6.5 로그 설계의 세 층위

빈도와 가치가 다르므로 층을 나눴다.

| 층 | 예시 | 빈도 | 레벨 | 처리 |
|---|---|---|---|---|
| 생명주기 | `pc_connected`, `session_created` | 낮음 | INFO | 전량 |
| **상태 전이** | `action_changed`, `pipeline_health_changed` | 중간 | INFO | 전량 |
| 프레임 | `frame_relayed`, `hand_not_detected` | 30/초 | DEBUG | 샘플링 |
| 장애 | `frame_rejected`, `analyze_failed` | 낮음 | WARN/ERROR | 전량 + traceback |

**상태 전이 로깅이 특히 유용하다.** 프레임 7장을 처리해도 전이는 2건뿐이다.

```
[INFO] [C] [s1] session_created  active_sessions=1
[INFO] [C] [s1] action_changed   from='HOVER' to='DRAW'  instant_pen_up=False
[INFO] [C] [s1] action_changed   from='DRAW' to='HOVER'  instant_pen_up=True
```

`instant_pen_up=True` 는 **비대칭 무지연 펜-업 컷오프가 발동했다**는 뜻이다.
이 프로젝트에서 가장 미묘한 로직이 이제 로그로 관측된다.

### 6.6 전 구간 추적

`trace_id` 는 A가 프레임마다 발급해 B, C로 전파한다.
장애 시 이 값 하나로 전 구간 타임라인을 복원할 수 있다.

```jsonc
{"ts":100, "container":"A", "trace_id":"user1-frame-45", "event":"frame_received"}
{"ts":115, "container":"B", "trace_id":"user1-frame-45", "event":"inference_done",
 "detail":{"inference_ms":15.2}}
{"ts":118, "container":"C", "trace_id":"user1-frame-45", "event":"gesture_computed",
 "detail":{"action":"DRAW"}}
{"ts":120, "container":"A", "trace_id":"user1-frame-45", "event":"frame_relayed",
 "detail":{"total_rtt_ms":20.0}}
```

---

## 7. CI/CD — 기준을 영구적으로 유지하는 장치

### 7.1 테스트 전략 — 계층별로 난이도가 다르다

| 대상 | 난이도 | 이유 | 방식 |
|---|---|---|---|
| Container C | 쉬움 | 순수 함수. I/O·외부 의존 없음 | 합성 랜드마크로 완전 커버 |
| Container A | 보통 | FastAPI/WebSocket 의존 | `TestClient` + B/C 모킹 |
| Container B | 어려움 | MediaPipe 수백 MB + 모델 다운로드 | **별도 CI 잡 + 캐시** |

**C를 먼저, 그리고 두텁게 짰다.** 순수 로직이라 투자 대비 효과가 가장 크고,
이후 모든 리팩터링의 안전망이 된다. 실제로 42개가 C에 몰려 있다.

### 7.2 합성 랜드마크 팩토리

카메라 없이 제스처를 결정론적으로 재현하는 것이 테스트의 토대였다.

```python
# tests/conftest.py
def make_hand(index=False, middle=False, ring=False, pinky=False, thumb_x=0.5, ...):
    """
    MediaPipe 좌표계에서 y는 아래로 갈수록 커진다.
    따라서 "손가락을 폈다" = 끝(tip)이 중간관절(pip)보다 y가 작다.
    """
    points = [{"x": 0.5, "y": 0.5, "z": 0.0} for _ in range(21)]
    for name, (tip, pip) in TIP_PIP.items():
        opened = {...}[name]
        points[tip] = {"x": 0.5, "y": 0.3 if opened else 0.7, "z": 0.0}
```

> 이 팩토리를 만들다 테스트가 한 번 실패했는데, **원인은 코드가 아니라 테스트 쪽 가정**이었다.
> `y=0.5` 로 좌표를 덮어쓰면 tip과 pip 높이가 같아져 손가락이 '접힘'으로 판정되고
> PAN으로 빠진다. 좌표가 손바닥 중심에 고정되어 EMA 테스트가 무의미해졌다.
> 코드를 고치는 대신 테스트를 고쳤고, 왜 `0.3` 이어야 하는지 주석으로 남겼다.

### 7.3 4대 축을 강제하는 테스트

청사진의 핵심 요구 — *"코드가 변경될 때마다 자동으로 검증되고 유지되도록"* 에 대응한다.
`tests/unit/test_pillars.py` 23개가 안티패턴의 재유입을 차단한다.

```python
def test_no_silent_exception_swallowing():
    violations = []
    for path in _python_files():
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and _body_is_only_pass(node):
                violations.append(f"{_rel(path)}:{node.lineno}")
    assert not violations
```

정규식이 아니라 **AST로 검사**한다. 문자열 매칭은 주석이나 문서 안의 예시를 오탐하고,
포맷이 조금만 달라져도 놓친다. 주석은 `_strip_py_comments()` 로 미리 제거한다.

| 축 | 검사 항목 |
|---|---|
| P1 | IP·포트 리터럴 부재, 설정 로딩, 필수 키, 누락 시 명시적 실패, 환경변수 오버라이드 |
| P2 | `except: pass` 부재, 서킷 상태 전이, null 좌표 차단, degraded 응답 유효성 |
| P3 | 전역 detector 부재, 벽시계 타임스탬프 부재, 세션 격리, 타임스탬프 단조성(100회), LRU 회수, 스레드풀 오프로딩, 백프레셔 |
| P4 | `print()` 부재, 공통 로거 사용, 레벨 필터링 실동작, traceback+context, 샘플링 100→10건 |

> **이 테스트들은 실제로 일을 했다.** 작성 직후 첫 실행에서 **방금 작성한 코드의 위반 3건**을 잡아냈다 —
> `__main__` 블록의 포트 리터럴, `config.py` 의 포트 폴백 리터럴, `detector_pool` 의 `except: pass`.
> 세 건 모두 수정했다.

### 7.4 파이프라인 구성

```
git push ─▶ ① lint ─▶ ② test-light ─▶ ④ build
                   └─▶ ③ test-vision
```

| Job | 시간 | 분리 이유 |
|---|---:|---|
| `lint` | 7s | 가장 싸므로 먼저 걸러낸다 |
| `test-light` | 23s | 대부분의 변경은 여기서 걸린다 |
| `test-vision` | 47s | MediaPipe가 무거워 분리 + 모델 캐시 |
| `build` | 1m59s | 테스트 통과 후에만 실행 |

`build` 잡은 `sleep` 이 아니라 **실제 healthcheck 상태를 폴링**한다.
고정 대기는 느린 러너에서 실패하고 빠른 러너에서 시간을 낭비한다.

```yaml
for i in $(seq 1 60); do
  unhealthy=$(docker compose ps --format '{{.Name}} {{.Health}}' | grep -v healthy | wc -l)
  if [ "$unhealthy" -eq 0 ]; then echo "모든 컨테이너 healthy"; exit 0; fi
  sleep 5
done
docker compose logs --tail 100   # 실패 시 원인을 남긴다
exit 1
```

마지막으로 컨테이너 로그가 **유효한 JSON 스키마인지까지** 확인한다.
로깅이 깨져도 서비스는 돌아가므로, 이걸 검사하지 않으면 조용히 퇴화한다.

### 7.5 첫 CI 실행은 실패했다

푸시 후 첫 실행에서 2개 잡이 깨졌다. 원인과 조치를 남긴다.

| 잡 | 종료 코드 | 원인 | 조치 |
|---|---|---|---|
| `lint` | 1 | ruff 위반 67건 (대부분 pyupgrade 계열) | 자동 수정 후 Python 3.10 컨테이너에서 파싱 검증 |
| `test-vision` | 5 | **수집된 테스트 0개** — 마커만 만들고 테스트를 안 씀 | vision 테스트 6개 작성 |

`test-vision` 의 exit 5는 pytest가 "no tests ran"일 때 반환하는 값이다.
**마커와 CI 잡만 준비하고 실제 테스트를 쓰지 않은 것이 그대로 드러났다.**

로컬에서 ruff를 실행할 수 없는 환경이었기에(Windows 애플리케이션 제어 정책),
**CI와 동일한 Linux 컨테이너로 재현**해 고쳤다.

```bash
docker run --rm -v "$(pwd):/app" -w /app python:3.11-slim \
  sh -c "pip install ruff && ruff check ."
```

이 과정에서 부수적으로 하나 더 깨졌다. isort가 import 순서를 바꾸자
**문자열 완전일치로 검사하던 자체 테스트**가 실패했다. AST 기반으로 교체했다.
테스트가 코드의 형태 변화에 취약하면 안 된다.

---

## 8. 부수적으로 발견하고 고친 것들

작업 과정에서 원래 목표가 아니었지만 드러난 문제들이다.

### 8.1 `entrypoint.sh` 의 CRLF

성능 비교를 위해 `git worktree` 로 베이스라인을 꺼냈더니 Container A가 무한 재시작했다.

```
exec ./entrypoint.sh: no such file or directory
```

Windows 체크아웃에서 CRLF로 변환된 것이 원인이었다.
**저장소에 그대로 있던 잠재 문제**라 `.gitattributes` 로 고정했다.

```gitattributes
*.sh text eol=lf
*.yaml text eol=lf
requirements*.txt text eol=lf
Dockerfile text eol=lf
```

### 8.2 인증서·모델 미영속

- SSL 인증서가 컨테이너 재생성마다 새로 발급되어 **폰에서 매번 보안 경고를 다시 승인**해야 했다
- MediaPipe 모델(7.8MB)을 컨테이너를 새로 만들 때마다 다시 다운로드했다

둘 다 볼륨 마운트로 해결했다.

### 8.3 기타

| 항목 | 조치 |
|---|---|
| 미사용 import (B에 4개) | 제거 |
| deprecated `@app.on_event` | `lifespan` 이관 |
| `str(e)` 를 응답에 노출 | 로그엔 traceback, 응답엔 예외 타입만 |
| `rooms` 세션 누수 | 양쪽 해제 시 키 삭제 |
| import 시점 모델 다운로드 | 로그 + 소요시간 기록, 실패 시 traceback |

---

## 9. 정리

이번 작업에서 기능은 하나도 추가하지 않았다. 바뀐 것은 **기능을 떠받치는 기반**이다.

| 관점 | Before | After |
|---|---|---|
| 관측 | 애플리케이션 로그 0줄 | `trace_id` 로 전 구간 추적 |
| 장애 | 조용히 정지, 무통보 | 통지하고 살아남음 |
| 동시성 | 직렬 처리 (56.7 fps) | 병렬 처리 (121.6 fps) |
| 유지 | 수동 검증 | 97개 테스트 + CI 4-Job |

작업하며 확인한 원칙 세 가지를 남긴다.

**① 측정이 설계를 바꾼다.**
청사진은 "전처리가 병목"이라고 전제했지만, 계측해보니 병목은 모델이었다.
가정대로 갔다면 엉뚱한 곳을 최적화했을 것이다.

**② 고치는 순서가 결과를 바꾼다.**
블로킹 해소를 먼저 했다면 타임스탬프 버그를 새로 만들었을 것이다.
잠재 버그는 "지금 안 터진다"가 안전을 뜻하지 않는다.

**③ 자동화되지 않은 기준은 유지되지 않는다.**
4대 축을 손으로 한 번 적용하는 것과, 위반 시 빌드가 깨지게 만드는 것은 다르다.
후자만이 시간이 지나도 남는다.

---

## 참고 문서

| 문서 | 내용 |
|---|---|
| [README](../README.md) | 프로젝트 개요와 결과 요약 |
| [BEFORE_AFTER_REPORT](BEFORE_AFTER_REPORT.md) | 수정 전/후 실측 비교 (측정 원문 포함) |
| [PRODUCTION_REFINING_PLAN](PRODUCTION_REFINING_PLAN.md) | 진단 26건 전체 목록과 실행 계획 |
| [diagrams/README](diagrams/README.md) | 아키텍처 다이어그램 재생성 방법 |
