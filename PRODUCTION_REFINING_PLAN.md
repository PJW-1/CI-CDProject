# Air Canvas System — 프로덕션 레벨 고도화 계획서

| 항목 | 내용 |
|---|---|
| **기준 문서** | `Production_Engineering_Blueprint.pptx` (4대 엔지니어링 축 + CI/CD) |
| **작성일** | 2026-08-31 |
| **저장소** | https://github.com/PJW-1/CI-CDProject |
| **베이스라인 커밋** | `a6b2596` (first commit) |
| **작업 성격** | 신규 기능 개발 아님. **기존 코드의 프로덕션 레벨 고도화(Refining)** |

---

## 0. 이 문서의 전제

청사진의 명제는 다음과 같다.

> 돌아가는 코드(AI 모델 + 핵심 비즈니스 로직)는 시스템의 **20%**.
> 나머지 **80%**는 그것을 실무 환경에서 중단 없이 굴러가게 만드는 **엔지니어링 기반**이다.

Air Canvas는 **"돌아가는 20%"가 이미 완성된 상태**다. 6대 제스처 판별, EMA 손떨림 보정,
3프레임 디바운스, 마이크로서비스 분리까지 PoC로서는 충분히 동작한다.

이 문서는 나머지 **80%**인 4대 축(파라미터화 / 예외처리 / 성능 / 로깅)과 CI/CD를
이식하기 위한 **진단 결과와 실행 계획**이다.

### 절대 원칙

1. **기능을 추가하지 않는다.** 제스처 규칙, 판별 임계값의 *동작 결과*, UI는 그대로 둔다.
2. **§1.4 베이스라인 동작을 100% 보존한다.** 리팩터링 후 사용자 체감이 달라지면 실패다.
3. **테스트 없이 리팩터링하지 않는다.** 안전망을 먼저 깐다.
4. **모든 개선은 근거로 증명한다.** "빨라졌다"가 아니라 "p95 지연 X ms → Y ms".

---

## 1. 현재 프로젝트 현황

### 1.1 아키텍처

```
[📱 스마트폰 카메라]
   │  base64 JPEG (480x360, q=0.6) · setInterval 33ms ≈ 30fps
   │  WebSocket over HTTPS (자체서명)
   ▼
┌──────────────────────────────────────────────┐
│ Container A : 웹 / 보안 서버 : 8443          │
│  · entrypoint.sh 에서 자체서명 SSL 자동 발급  │
│  · QR 기반 PC ↔ 모바일 1:1 세션 매칭          │
│  · rooms{} 에 세션별 WebSocket 쌍 보관        │
│  · 순수 릴레이 (비즈니스 로직 없음)           │
└──────────────────────────────────────────────┘
   │  HTTP POST /analyze  { session_id, image(base64) }   ← 무거운 픽셀
   ▼
┌──────────────────────────────────────────────┐
│ Container B : 비전 엔진 : 8001               │
│  · MediaPipe HandLandmarker (RunningMode.VIDEO)│
│  · base64 → JPEG → BGR → RGB → 추론          │
│  · 21개 관절 (x,y,z) 정규화 좌표로 압축       │
└──────────────────────────────────────────────┘
   │  HTTP POST /gesture  { session_id, landmarks[21] }   ← 초경량 좌표
   ▼
┌──────────────────────────────────────────────┐
│ Container C : 모션 엔진 : 8002               │
│  · 6대 제스처 규칙 판별 (기하학적 관계 연산)  │
│  · 속도 적응형 3단계 EMA 손떨림 보정          │
│  · 3프레임 슬라이딩 윈도우 다수결 디바운스     │
│  · 비대칭 무지연 펜-업 컷오프                 │
└──────────────────────────────────────────────┘
   │  { action, x, y, delta, pan_dx, pan_dy }
   ▼  (A를 거쳐 PC로 릴레이)
[🖥️ PC 브라우저 HTML5 Canvas 실시간 드로잉]
```

**데이터 압축비가 이 아키텍처의 핵심 설계 의도다.**
약 7KB base64 프레임 → 21개 좌표(약 500B)로 줄여 C에 넘긴다.
따라서 **A와 B 사이가 가장 무거운 구간**이며, 성능 작업의 초점도 여기다.

### 1.2 코드 규모

| 구성 | 파일 | 라인 수 |
|---|---|---:|
| Container A (웹/보안) | `container_a_web/main.py` | 137 |
| Container B (비전) | `container_b_vision/main.py` | 121 |
| Container C (모션) | `container_c_gesture/main.py` | 311 |
| 로컬 실행 스크립트 | `start_local.py` | 98 |
| PC 프론트엔드 | `container_a_web/static/pc.html` | 640 |
| 모바일 프론트엔드 | `container_a_web/static/mobile.html` | 318 |
| **Python 합계** | | **667** |
| **전체 파일 수** | | **17** |

> 규모가 작다는 것은 **작업 대부분이 기존 코드 리팩터링이 아니라 기반 인프라 신규 구축**임을 뜻한다.
> 청사진이 요구하는 바가 정확히 그것이므로 문제는 아니되, 예상 작업량 산정 시 감안해야 한다.

### 1.3 보존 대상 — 6대 제스처 규칙 (동작 변경 금지)

| # | 손 모양 | 판정 조건 (`container_c_gesture/main.py`) | action | 산출값 |
|---|---|---|---|---|
| 1 | 검지만 폄 | `index_open` & 나머지 4개 전부 닫힘 | `DRAW` | 검지끝 `lm[8]` |
| 2 | 검지+중지 (✌️) | 두 손가락 폄 & 높이차 `< 0.12` | `ERASE` | `lm[8]`,`lm[12]` 중점 |
| 3 | 주먹+엄지만 | `thumb_open` & 나머지 닫힘 | `ZOOM_IN` | delta `+0.008` |
| 4 | 주먹+새끼만 | `pinky_open` & 나머지 닫힘 | `ZOOM_OUT` | delta `-0.008` |
| 5 | 완전한 주먹 | 4손가락 닫힘 & `thumb_folded` | `PAN` | 손바닥중심 이동량 |
| 6 | 그 외 전부 | fallback | `HOVER` | 검지끝 `lm[8]` |

**부가 신호처리 (역시 보존 대상)**

- **속도 적응형 EMA** — 이동거리에 따라 alpha 3단계 전환 (`0.35` / `0.50` / `0.85`)
- **3프레임 다수결 디바운스** — 2표 이상 득표 시에만 상태 전이
- **비대칭 무지연 펜-업 컷오프** (`main.py:206-213`) — `DRAW → 비DRAW` 전이 시에만
  다수결을 건너뛰고 즉시 전환. 획 끝의 "삐침" 방지용. **이 비대칭성은 의도된 설계이므로 유지**

### 1.4 베이스라인 검증 결과 (2026-08-31 실측)

고도화 착수 **전** 현재 코드가 정상 동작함을 확인한 기록. **리팩터링 후 이 결과가 그대로 유지되어야 한다.**

| # | 항목 | 방법 | 결과 |
|---|---|---|---|
| 1 | 이미지 빌드 | `docker compose up --build` | 3개 전부 성공 |
| 2 | 컨테이너 기동 | `docker compose ps` | 3개 전부 `Up` |
| 3 | C 제스처 로직 | 합성 랜드마크 6종 주입 | **6대 제스처 전부 정확 판별** |
| 4 | C 디바운스 | 동일 입력 반복 | 1프레임 `HOVER` → 3프레임 후 확정 (설계대로) |
| 5 | C 방어 로직 | 빈 배열 / 랜드마크 5개 | 예외 없이 `NONE` 반환 |
| 6 | B 초기화 | 컨테이너 로그 | MediaPipe XNNPACK CPU 델리게이트 정상 (GPU 없음) |
| 7 | A 엔드포인트 | `/`, `/mobile`, `/api/info`, `/api/qr` | 전부 **HTTP 200** |
| 8 | QR 생성 | PNG 디코딩 | `https://192.168.55.208:8443/mobile?session=...` |
| 9 | **전체 체인** | WebSocket 5프레임 A→B→C→A 왕복 | **평균 19ms** (min 19 / max 20) |
| 10 | 세션 동기화 | PC/모바일 STATUS 교환 | 정상 |
| 11 | **실사용 E2E** | 실제 스마트폰 + 손 동작 | **정상 동작 확인** |

**측정 조건:** 손 미검출(빈 프레임) 기준 단일 세션. 실제 추론이 포함된 부하 상태의 수치가 아니므로,
성능 작업(§4) 착수 시 **손이 포함된 프레임으로 재측정하여 진짜 베이스라인을 다시 잡아야 한다.**

> ⚠️ **현재 자동화 테스트가 0개다.** 위 검증은 전부 일회성 수동 확인이며,
> 리팩터링으로 동작이 깨져도 아무도 알려주지 않는다. → **CI/CD를 최우선으로 두는 이유.**

---

## 2. 목표 구조

작업 완료 시점의 디렉터리 구조. 신규 항목은 `[NEW]`로 표기.

```
air-canvas-system/
├── config/                          [NEW] 설정 단일 출처
│   ├── default.yaml                 [NEW] 기본값 (전 컨테이너 공통 + 서비스별)
│   ├── dev.yaml                     [NEW] 개발 환경 오버라이드
│   └── prod.yaml                    [NEW] 운영 환경 오버라이드
├── common/                          [NEW] 3개 컨테이너 공유 모듈
│   ├── __init__.py
│   ├── config.py                    [NEW] YAML + 환경변수 병합 로더
│   ├── logging_setup.py             [NEW] 구조화 로거 (C의 log_event 승격)
│   ├── schemas.py                   [NEW] Pydantic 공통 응답/요청 스키마
│   ├── http_client.py               [NEW] 재시도 + 서킷브레이커 래퍼
│   └── net.py                       [NEW] LAN IP 자동 탐지
├── container_a_web/
│   ├── main.py                      (수정) 릴레이 + degradation + 헬스체크
│   ├── static/{pc,mobile}.html      (수정) 하드코딩 제거, /api/config 소비
│   ├── Dockerfile                   (수정) 파이썬 버전 통일
│   └── entrypoint.sh                (수정) 인증서 영속화
├── container_b_vision/
│   ├── main.py                      (수정) 스레드풀 오프로딩 + 세션별 detector
│   └── ...
├── container_c_gesture/
│   ├── main.py                      (수정) 규칙 함수 분리, 설정 주입
│   └── ...
├── tests/                           [NEW]
│   ├── conftest.py                  [NEW] 공통 픽스처 (합성 랜드마크 팩토리 등)
│   ├── fixtures/
│   │   └── hand_sample.jpg          [NEW] B 테스트용 고정 이미지
│   ├── unit/
│   │   ├── test_gesture_rules.py    [NEW] 6대 제스처 × 경계값
│   │   ├── test_ema_debounce.py     [NEW] EMA 3단계 / 비대칭 컷오프
│   │   ├── test_config.py           [NEW] 설정 로딩 우선순위
│   │   └── test_logging.py          [NEW] 로그 스키마·레벨·traceback
│   ├── integration/
│   │   ├── test_container_a.py      [NEW] WebSocket 릴레이·세션·QR
│   │   ├── test_degradation.py      [NEW] B/C 다운 시 A 생존
│   │   └── test_concurrent.py       [NEW] 동시 2세션 오염 방지 (P3-2 회귀)
│   └── perf/
│       └── bench_pipeline.py        [NEW] 구간별 지연 벤치마크
├── .github/workflows/
│   └── ci.yml                       [NEW] lint → test → build
├── requirements/                    [NEW]
│   ├── *.in / *.txt                 [NEW] pip-tools 락파일
├── docker-compose.yml               (수정) healthcheck, 볼륨, 설정 마운트
├── pytest.ini                       [NEW]
├── PRODUCTION_REFINING_PLAN.md      (본 문서)
└── README.md                        (수정) 설정/운영 섹션 추가
```

### 설정 스키마 초안 (`config/default.yaml`)

```yaml
network:
  host_ip: auto            # auto | 명시적 IP. auto면 런타임 LAN IP 탐지
  web_port: 8443
  vision_port: 8001
  gesture_port: 8002

http:
  connect_timeout_s: 1.0
  read_timeout_s: 1.5      # 현재 0.2/0.3 → 현실적 값으로 상향
  retry:
    max_attempts: 2        # 30fps 특성상 과도한 재시도는 오히려 해악
    backoff_base_s: 0.05
  circuit_breaker:
    failure_threshold: 10
    recovery_timeout_s: 5.0

stream:                    # 프론트엔드로 내려보내는 값
  width: 480
  height: 360
  jpeg_quality: 0.6
  interval_ms: 33
  max_inflight_frames: 2   # 백프레셔 (신규)

vision:
  model_path: models/hand_landmarker.task
  num_hands: 1
  min_hand_detection_confidence: 0.6
  min_hand_presence_confidence: 0.5
  min_tracking_confidence: 0.5
  detector_pool_size: 4    # 세션별 격리 (신규)
  download_timeout_s: 30
  max_frame_bytes: 1048576

gesture:
  thresholds:
    thumb_open_palm_dist: 0.15
    thumb_open_index_base_dist: 0.12
    thumb_folded_dist: 0.13
    erase_height_diff: 0.12
  zoom:
    step: 0.008
  ema:
    deadzone_dist: 0.001
    slow_dist: 0.05
    alpha_micro: 0.35
    alpha_precise: 0.50
    alpha_fast: 0.85
  debounce:
    window: 3
    majority: 2
    instant_pen_up: true   # 비대칭 컷오프 유지
  session:
    ttl_s: 600

logging:
  level: INFO
  format: json
  frame_log_sample_rate: 30   # 30프레임당 1회만 기록 (로그 폭주 방지)
  rotation:
    max_bytes: 10485760
    backup_count: 5
```

**설정 우선순위:** `default.yaml` → `{env}.yaml` → 환경변수 → CLI 인자
(환경변수가 YAML을 덮어써야 Docker/CI에서 주입 가능)

---

## 3. Pillar 1 — 파라미터화 및 가독성

> *"나만 쓰는 코드가 아니다. 누구나 환경설정만 바꿔서 쓸 수 있어야 한다."*

### 진단 요약

**설정 파일이 단 하나도 없다.** `.env`, `config.yaml`, `.ini`, `pyproject.toml` 전부 부재.
모든 값이 소스코드·Dockerfile·compose·HTML에 직접 기재되어 있으며, 동일 값이 여러 곳에 중복된다.

---

### 🔴 P1-1. IP 주소 하드코딩 — 3곳 중복 · 최우선

**현상**

| 위치 | 코드 |
|---|---|
| `container_a_web/main.py:20` | `DEFAULT_HOST_IP = os.getenv("HOST_IP", "192.168.55.208")` |
| `container_a_web/static/pc.html:293-295` | `? '192.168.55.208'` |
| `docker-compose.yml` | `- HOST_IP=192.168.55.208` |

**실측 증거**

발급된 QR PNG를 디코딩한 결과:
```
https://192.168.55.208:8443/mobile?session=testsession
```
현재 이 PC의 실제 LAN IP가 **우연히** `192.168.55.208`이라 동작하고 있다.

**영향**

다른 Wi-Fi로 이동하거나 DHCP가 IP를 재할당하는 즉시 **QR이 죽고 폰이 접속 불가**가 된다.
사용자에게는 "QR을 찍었는데 아무 일도 안 일어남"으로 나타나며, 원인 파악이 어렵다.
로컬 개발자 본인 외에는 이 프로젝트를 실행조차 할 수 없다는 뜻이기도 하다.

**수정 방향**

`start_local.py:7-14`에 이미 LAN IP 자동 탐지 구현이 있다. 이를 `common/net.py`로 승격해 재사용한다.

```python
# common/net.py
def detect_lan_ip(fallback: str = "127.0.0.1") -> str:
    """UDP 소켓의 로컬 바인딩 주소로 LAN IP 탐지 (실제 패킷 전송 없음)"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return fallback
```

프론트엔드는 IP를 알 필요가 없다. `/api/info`가 이미 존재하므로 이를 소비하도록 바꾼다.

```javascript
// pc.html — 하드코딩 IP 제거
const { ip, port } = await (await fetch('/api/info')).json();
const mobileUrl = `https://${ip}:${port}/mobile?session=${sessionId}`;
```

**완료 조건**
- [ ] 소스 전체에 `192.168.` 문자열 리터럴 0개 (테스트로 검증 — `CI-5`)
- [ ] 다른 네트워크에서 실행해도 QR이 올바른 주소를 가리킴

---

### 🔴 P1-2. 포트 번호가 5개 계층에 산재

**현상** — `8443` / `8001` / `8002`가 아래 전부에 중복 기재되어 있다.

| 계층 | 위치 |
|---|---|
| 파이썬 코드 | `a/main.py:45`, `a/main.py:50`, `c/main.py:311` |
| Dockerfile | 3개 파일의 `EXPOSE` + `CMD` |
| entrypoint | `container_a_web/entrypoint.sh:18` |
| compose | `ports`, `environment` URL |
| 프론트엔드 | `pc.html:297` |

포트 하나를 바꾸려면 **최소 8곳을 동시에 수정**해야 하며, 하나라도 놓치면 조용히 깨진다.

**수정 방향**
`config/default.yaml`의 `network` 섹션을 단일 출처로 삼고, compose가 환경변수로 주입한다.
Dockerfile의 `EXPOSE`는 문서적 의미만 있으므로 유지하되, 실제 바인딩은 전부 설정에서 읽는다.

**완료 조건**
- [ ] 포트를 바꿀 때 수정할 파일이 `config/*.yaml` 하나

---

### 🟡 P1-3. 제스처 임계값 매직넘버 (Container C)

**현상** — 판별 로직 전반에 튜닝 상수가 직접 박혀 있다.

| 위치 | 값 | 의미 |
|---|---|---|
| `c/main.py:126` | `0.15`, `0.12` | 엄지 폄 판정 (손바닥 거리 / 검지밑동 거리) |
| `c/main.py:127` | `0.13` | 엄지 접힘 판정 |
| `c/main.py:151` | `0.12` | ERASE 두 손가락 높이차 허용 범위 |
| `c/main.py:165, 174` | `+0.008`, `-0.008` | 줌 배율 증감 |
| `c/main.py:57` | `maxlen=3` | 디바운스 슬라이딩 윈도우 |
| `c/main.py:212` | `>= 2` | 다수결 임계 |
| `c/main.py:226, 229` | `0.001`, `0.05` | EMA 속도 구간 경계 |
| `c/main.py:227, 230, 233` | `0.35`, `0.50`, `0.85` | EMA alpha 3단계 |
| `c/main.py:65` | `600` | 세션 만료 시간(초) |

**왜 문제인가**

이 값들은 **본질적으로 튜닝 대상**이다. 손 크기, 카메라 화각, 조명, 사용자 습관에 따라
최적값이 달라진다. 현재는 값을 바꾸려면 코드를 수정하고 이미지를 재빌드해야 한다.
운영 중 A/B 조정이 불가능하다.

**수정 방향**

`config/default.yaml`의 `gesture` 섹션으로 전량 이관하고, 모듈 로드 시 주입한다.
**주의: 기본값은 현재 값과 정확히 동일해야 한다.** 값이 바뀌면 §1.4 베이스라인이 깨진다.

**완료 조건**
- [ ] 코드에 부동소수 리터럴 임계값 0개
- [ ] 설정 기본값으로 실행 시 §1.4의 6대 제스처 판별 결과가 완전 동일 (테스트로 증명)

---

### 🟡 P1-4. 스트리밍 파라미터 하드코딩 (프론트엔드)

**현상** — `container_a_web/static/mobile.html:279-290`

```javascript
hiddenCanvas.width = 480;
hiddenCanvas.height = 360;
...
const base64Image = hiddenCanvas.toDataURL('image/jpeg', 0.6);
ws.send(base64Image);
}, 33);   // ≈ 30fps
```

해상도, JPEG 품질, 전송 주기가 전부 고정이다. 네트워크가 느린 환경에서 조정할 방법이 없다.

**수정 방향** — `/api/config` 엔드포인트를 신설해 `stream` 섹션을 내려주고 프론트가 소비한다.
(`33` → 백프레셔 도입과 함께 다뤄야 하므로 §5 P3-3과 연계)

---

### 🟡 P1-5. 타임아웃 하드코딩 · 비현실적 값

**현상**

| 위치 | 코드 | 문제 |
|---|---|---|
| `a/main.py:93` | `httpx.AsyncClient(timeout=0.2)` | 200ms — 극단적으로 짧음 |
| `b/main.py:86` | `httpx.AsyncClient(timeout=0.3)` | 300ms |

**왜 문제인가**

베이스라인 실측 왕복이 19ms이므로 평상시엔 여유가 있어 보인다. 그러나 그 측정은
**손 미검출 프레임** 기준이다. 실제 MediaPipe 추론이 도는 프레임에서는 지연이 훨씬 크며,
CPU 부하나 동시 세션이 늘면 200ms는 상시 초과된다.
게다가 현재는 타임아웃이 나도 `except Exception: pass`(§4 P2-1)로 삼켜지므로 **아무도 모른다.**

**수정 방향**
- connect/read 타임아웃 분리, 설정화
- **먼저 실제 추론 포함 지연을 측정한 뒤** 그 값에 근거해 기본값 결정 (추측으로 정하지 않는다)

---

### 🟢 P1-6. 구조 및 가독성

| 항목 | 현상 | 조치 |
|---|---|---|
| 미사용 import | `b/main.py:1-20`의 `asyncio`, `json`, `logging`, `sys` — 4개 전부 미사용 | 제거 (`logging`은 P4-3에서 실제 사용으로 부활) |
| 공통 모듈 부재 | 3개 컨테이너가 설정·로깅·스키마를 각자 구현 | `common/` 신설 |
| 거대 함수 | `c/main.py:96-243` `compute_gesture_logic()` — 약 130줄, 판별·디바운스·EMA가 한 함수에 혼재 | 규칙별 함수 + 필터 클래스로 분리 |
| 파이썬 버전 불일치 | B만 `python:3.10-slim`, A/C는 `3.11-slim` | 통일 (mediapipe 호환성 확인 후 결정) |
| 타입 힌트 부분 적용 | C는 양호, A/B는 거의 없음 | 공개 함수에 시그니처 부여 |
| 린터/포매터 부재 | 설정 없음 | `ruff` 도입 (`CI-6`에 연결) |

---

## 4. Pillar 2 — 예외 처리 및 안정성

> *"단 한 번의 에러로 24시간 도는 공장 생산 라인을 멈추게 할 수는 없다."*

---

### 🔴 P2-1. 예외 완전 침묵 — 최악의 안티패턴

**현상** — `container_a_web/main.py:96-127`

```python
try:
    resp = await client.post(CONTAINER_B_URL, json={...})
    if resp.status_code == 200:
        ...
except Exception:
    pass          # ← 모든 예외를 흔적 없이 삼킴
```

**왜 치명적인가**

이 한 줄이 다음을 **전부** 무음 처리한다.

- B 컨테이너 다운 (`ConnectError`)
- 타임아웃 초과 (`TimeoutException`) — P1-5의 200ms 설정과 결합하면 상시 발생
- JSON 파싱 실패
- PC WebSocket이 이미 끊긴 상태에서의 전송 실패
- 코드 버그로 인한 `AttributeError` / `KeyError`

사용자에게는 "그림이 안 그려진다"로만 나타나고, **로그에도 아무 흔적이 없다.**
청사진 Pillar 4가 말하는 "책임 소재를 밝히는 법적 흔적"이 원천적으로 소실되는 지점이다.

또한 `status_code != 200`인 경우도 조용히 무시된다(else 분기 없음).

**수정 방향**

```python
except httpx.TimeoutException:
    metrics.timeout += 1
    log.warning("vision_timeout", session_id=sid, timeout_s=cfg.read_timeout_s)
    await notify_degraded(sid, reason="VISION_SLOW")
except httpx.ConnectError:
    log.error("vision_unreachable", session_id=sid, url=cfg.vision_url)
    await notify_degraded(sid, reason="VISION_DOWN")
except Exception:
    log.exception("relay_unexpected", session_id=sid)   # traceback 보존
    await notify_degraded(sid, reason="INTERNAL")
```

**완료 조건**
- [ ] 소스 전체에 `except ...: pass` 패턴 0개 (테스트로 검증)
- [ ] 모든 예외 경로가 로그를 남기고, 사용자에게 상태가 통지됨

---

### 🔴 P2-2. 재시도(Retry) 메커니즘 부재

**현상** — A→B, B→C 모두 단발 요청. 실패 시 해당 프레임은 그대로 유실된다.

**설계 시 주의할 점**

30fps 파이프라인에서 **무분별한 재시도는 오히려 해롭다.**
33ms마다 새 프레임이 도착하는데 실패한 프레임을 오래 붙잡고 재시도하면
큐가 밀려 지연이 누적된다(§5 P3-3와 직결).

**따라서 다음 원칙을 적용한다.**

- 최대 재시도 **1~2회**, 백오프 `50ms` 수준
- 총 소요가 프레임 주기(33ms)의 2배를 넘으면 **재시도를 포기하고 프레임을 버린다**
- 연속 실패가 임계치를 넘으면 **서킷 브레이커 open** → 재시도 자체를 중단하고
  degradation 모드로 전환, 일정 시간 후 half-open으로 탐침

**수정 방향** — `common/http_client.py`에 재시도 + 서킷브레이커를 캡슐화하고 A/B가 공유.

---

### 🔴 P2-3. Graceful Degradation 부재

**현상** — 현재 B 또는 C가 죽으면 드로잉 전체가 정지하고, **화면에는 아무 안내도 없다.**
사용자는 자기 손동작이 잘못됐다고 착각한다.

**수정 방향 — 계층별 저하 시나리오**

| 장애 | 현재 | 목표 |
|---|---|---|
| **C 다운** | 전체 정지 | B의 원시 랜드마크로 `HOVER` 커서만이라도 유지. 그리기 기능만 비활성 + 배너 |
| **B 다운** | 전체 정지 | 마지막 유효 좌표 유지, 캔버스 보존. PC에 "비전 엔진 장애" 배너 |
| **일시적 지연** | 조용히 프레임 유실 | 프레임 드롭 + 상태 표시등을 노랑으로 |
| **복구** | 수동 새로고침 필요 | 헬스체크 성공 감지 시 자동 정상 복귀 |

프로토콜에 `{"type": "STATUS", "health": "OK|DEGRADED|DOWN", "reason": ...}` 메시지를 추가하고
PC/모바일 UI에 상태 표시등을 둔다.

> **주의:** 이는 "기능 추가"가 아니라 **기존 기능의 장애 대응**이므로 §0 원칙에 위배되지 않는다.
> 다만 UI 변경은 상태 배너 수준으로 최소화한다.

---

### 🟡 P2-4. 입력값 검증 부재

**현상** — `container_b_vision/main.py:57-63`

```python
def decode_base64_frame(frame_b64: str):
    if "," in frame_b64:
        frame_b64 = frame_b64.split(",")[1]
    jpg_bytes = base64.b64decode(frame_b64)      # 검증 없음
    np_arr = np.frombuffer(jpg_bytes, dtype=np.uint8)
    return cv2.imdecode(np_arr, cv2.IMREAD_COLOR)  # 실패 시 None
```

- base64 형식 검증 없음 → 잘못된 입력에 `binascii.Error`
- 크기 상한 없음 → 대용량 페이로드로 메모리 압박 가능 (외부에 노출되는 경로)
- `cv2.imdecode()`가 `None`을 반환할 수 있고, `extract_landmarks()`가 방어하긴 하나(`main.py:66-67`)
  **암묵적**이라 리팩터링 시 깨지기 쉽다

**수정 방향**
- Pydantic 필드 검증 (`max_length`, base64 패턴)
- 디코딩 실패를 명시적 `400`으로 응답 (현재는 `{"success": false}` 200)
- `max_frame_bytes` 설정화

---

### 🟡 P2-5. 헬스체크 불완전

**실측 증거**

| 엔드포인트 | 결과 |
|---|---|
| `GET https://localhost:8443/health` (A) | **404** |
| `GET /health` (B) | 200 `{"status":"ok","service":"video_engine"}` |
| `GET /health` (C) | 200 `{"status":"ok","service":"motion_engine"}` |

또한 `docker-compose.yml`에 `healthcheck` 정의가 **아예 없다.**
`depends_on`은 컨테이너 **프로세스 시작**만 보장할 뿐 **애플리케이션 준비 완료를 기다리지 않는다.**
B는 MediaPipe 모델 로드에 시간이 걸리므로, A가 먼저 요청을 보내면 초기 프레임이 실패한다.

**수정 방향**

- A에 `/health` 추가. 단순 `{"status":"ok"}`가 아니라 **하위 B/C 연결성까지 확인하는 딥 체크**
  (`/health` = 자기 자신, `/health/ready` = 의존성 포함 — liveness/readiness 분리)
- compose 3개 서비스 전부에 `healthcheck` 추가
- `depends_on: { condition: service_healthy }`로 기동 순서 보장

---

### 🟡 P2-6. null 좌표 누수 (계약 부재)

**실측 증거** — 손 미검출 시 PC가 수신하는 실제 페이로드:

```json
{"type":"GESTURE","action":"NONE","x":null,"y":null,"delta":0,"pan_dx":0,"pan_dy":0,"landmarks":[]}
```

**원인** — B는 미검출 시 `{"success": True, "action": "NONE", "landmarks": []}`만 반환하고
`x`/`y` 키 자체가 없다(`b/main.py:98`). A는 `a/main.py:106-107`에서 `result.get("x")`로
꺼내므로 `None`이 그대로 릴레이된다.

**왜 문제인가** — 현재 프론트 JS가 우연히 견디고 있을 뿐, **서비스 간 응답 계약이 없다.**
누군가 JS에서 `x.toFixed()`를 호출하는 순간 런타임 에러다.

**수정 방향** — `common/schemas.py`에 `GestureResult` Pydantic 모델을 정의하고
A/B/C가 **동일 모델을 공유**한다. 기본값 `x=0.5, y=0.5`를 스키마 레벨에서 보장.

---

### 🟡 P2-7. 세션 정리 로직 비대칭

**현상**

| 컨테이너 | 세션 만료 |
|---|---|
| C | ✅ 600초 TTL 존재 (`c/main.py:63-67`) |
| **A** | ❌ **없음** |

`a/main.py:76-79, 129-131`을 보면 연결 해제 시 값만 `None`으로 바꾸고 **`rooms`의 키는 영구 잔류**한다.

```python
rooms[session_id]["pc_ws"] = None    # 키는 지워지지 않음
```

세션 ID는 `pc.html:290`에서 `Math.random()`으로 매번 새로 생성되므로,
**페이지를 새로고침할 때마다 `rooms`에 죽은 엔트리가 하나씩 영구 누적**된다.

**수정 방향**
- 양쪽 WebSocket이 모두 `None`이 되면 즉시 키 삭제
- 추가로 TTL 기반 주기적 스윕 (C와 동일 정책, 설정 공유)

---

### 🟢 P2-8. 기타 안정성 항목

| 항목 | 위치 | 문제 | 조치 |
|---|---|---|---|
| import 시 모델 다운로드 | `b/main.py:38-45` | `ensure_model()`이 **모듈 import 시점**에 실행. 네트워크 실패 시 **컨테이너 자체가 기동 불가**. 타임아웃·재시도·무결성 검증 전부 없음 | lifespan으로 이동, 타임아웃/재시도/체크섬, 실패 시 명확한 에러 |
| SSL 인증서 미영속 | `entrypoint.sh:7-15`, `compose` | 볼륨이 없어 컨테이너 재생성마다 **새 인증서 발급** → **폰에서 매번 경고 재승인 필요** | `certs` 볼륨 마운트 |
| deprecated API | `b/main.py:118` | `@app.on_event("shutdown")` — FastAPI에서 deprecated | `lifespan` 컨텍스트 매니저로 이관 |
| 프로세스 감독 부재 | `start_local.py:88-89` | `while True: sleep(1)` — 자식 프로세스가 죽어도 감지 못 함 | 종료 감지 + 로그 출력 |
| 예외 정보 손실 | `b/main.py:115-116` | `except Exception as e: return {"error": str(e)}` — traceback 소실, 내부 오류 메시지가 외부로 노출 | 로그엔 traceback, 응답엔 일반화된 메시지 |

---

## 5. Pillar 3 — 성능 및 메모리 관리

> *"모델보다 전처리가 지연 시간을 더 잡아먹는 일은 없어야 한다."*

---

### 🔴 P3-1. 이벤트 루프 블로킹 — 가장 심각한 성능 결함

**현상** — `container_b_vision/main.py:93-96`

```python
@app.post("/analyze")
async def analyze_frame(payload: FramePayload):
    image = decode_base64_frame(payload.image)              # 동기 CPU (base64+JPEG 디코딩)
    detected, landmarks_list = extract_landmarks(image)     # 동기 CPU (MediaPipe 추론)
```

`async def` 핸들러 안에서 **동기 CPU 바운드 작업을 직접 호출**하고 있다.

**왜 치명적인가**

uvicorn의 이벤트 루프는 단일 스레드다. MediaPipe 추론이 도는 동안
**해당 워커의 이벤트 루프 전체가 정지**한다. 그 시간 동안 다른 세션의 요청은 물론
헬스체크 응답조차 처리되지 않는다. 동시 접속자가 늘면 처리량이 선형으로 붕괴하고
지연이 누적된다.

현재 1인 사용 시나리오에서만 문제가 드러나지 않는 것뿐이다.

**수정 방향**

```python
from starlette.concurrency import run_in_threadpool

@app.post("/analyze")
async def analyze_frame(payload: FramePayload):
    image = await run_in_threadpool(decode_base64_frame, payload.image)
    detected, landmarks = await run_in_threadpool(extract_landmarks, image)
```

MediaPipe는 GIL을 놓는 네이티브 연산이 대부분이므로 스레드풀로도 실효가 있다.
다만 **P3-2(전역 detector 공유)를 먼저 해결하지 않으면 스레드풀 도입이 오히려 경합을 악화**시킨다.
**반드시 P3-2 → P3-1 순서로 작업한다.**

**완료 조건**
- [ ] 동시 1 / 2 / 4 세션에서 **처리량(fps)과 p95 지연을 개선 전후로 측정하여 수치 기록**

---

### 🔴 P3-2. 전역 단일 Detector + VIDEO 모드 = 세션 간 상태 오염

**현상** — 두 가지 문제가 겹쳐 있다.

```python
# container_b_vision/main.py:46-54
_hand_landmarker_options = mp_vision.HandLandmarkerOptions(
    ...,
    running_mode=mp_vision.RunningMode.VIDEO,     # ← 프레임 간 상태 유지 모드
)
hands_detector = mp_vision.HandLandmarker.create_from_options(_hand_landmarker_options)  # ← 전역 1개

# container_b_vision/main.py:72
timestamp_ms = int(time.time() * 1000)            # ← 벽시계 기반
result = hands_detector.detect_for_video(mp_image, timestamp_ms)
```

**문제 1 — 세션 간 추적 상태 오염**

`RunningMode.VIDEO`는 **이전 프레임의 추적 결과를 다음 프레임에 활용**한다.
이것이 부드러운 60fps 트래킹을 만드는 핵심이지만, 동시에 **상태를 가진다**는 뜻이다.
전역 인스턴스 1개를 모든 세션이 공유하므로, **사용자 2명이 동시 접속하면
서로의 손 추적 상태를 오염시킨다.** A의 손 위치가 B의 추적에 영향을 준다.

**문제 2 — 타임스탬프 단조 증가 위반**

`detect_for_video()`는 **단조 증가하는 타임스탬프를 요구**한다.
`int(time.time() * 1000)`은 이를 보장하지 못한다.

- 같은 밀리초에 2프레임이 도착하면 타임스탬프가 동일 → 예외
- 여러 세션의 프레임이 섞이면 순서가 뒤집힘 → 예외
- 30fps × 다중 세션이면 밀리초 충돌은 **드문 일이 아니다**

**수정 방향**

```python
class DetectorPool:
    """세션별로 독립된 detector와 단조 증가 타임스탬프를 보장"""
    def __init__(self, size: int, options_factory):
        self._pool = {}                # session_id -> (detector, lock, counter)
        ...

    def acquire(self, session_id: str):
        # 세션별 전용 detector + 자체 타임스탬프 카운터 반환
        # 동일 세션 내 동시 요청은 lock으로 직렬화
```

- 세션별 detector 인스턴스 (풀 크기는 설정, LRU로 유휴 세션 회수)
- 세션별 **단조 증가 카운터**로 타임스탬프 생성 (벽시계 사용 금지)
- 동일 세션 내 요청은 직렬화 (VIDEO 모드는 순서가 의미를 가짐)

**완료 조건**
- [ ] **동시 2세션 회귀 테스트 작성** (`tests/integration/test_concurrent.py`)
- [ ] 현재 코드에서 재현 → 수정 후 통과, 로 증명

---

### 🟡 P3-3. 백프레셔 부재

**현상** — `container_a_web/static/mobile.html:284-290`

```javascript
streamInterval = setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN && video.readyState === video.HAVE_ENOUGH_DATA) {
        hiddenCtx.drawImage(video, 0, 0, hiddenCanvas.width, hiddenCanvas.height);
        const base64Image = hiddenCanvas.toDataURL('image/jpeg', 0.6);
        ws.send(base64Image);
    }
}, 33);
```

**서버 처리 속도와 무관하게 33ms마다 무조건 발사**한다.
서버가 느려져도 클라이언트는 계속 밀어넣으므로 WebSocket 송신 버퍼에 프레임이 적체되고,
**지연이 누적되어 갈수록 뒤처진다**(실시간성 상실). 회복 수단도 없다.

**수정 방향**

- in-flight 프레임 수 제한 (`max_inflight_frames: 2`) — 응답을 못 받은 프레임이 임계 초과면 전송 스킵
- `ws.bufferedAmount`로 송신 버퍼 감시, 임계 초과 시 드롭
- **최신 프레임 우선** — 밀린 프레임은 버리고 최신만 보낸다 (드로잉은 최신 위치가 중요)
- 드롭률을 로그/UI에 노출 (§6 P4)

---

### 🟡 P3-4. 메모리 누수 및 불필요한 복사

| 위치 | 내용 |
|---|---|
| `a/main.py:65, 84` | `rooms` 딕셔너리 무한 증가 (§4 P2-7과 동일 근원). **새로고침 1회 = 죽은 엔트리 1개 영구 적재** |
| `b/main.py:57-63, 69-71` | 프레임마다 `base64 str → bytes → ndarray → BGR → RGB` 전체 사본 생성. 480x360 기준 회당 약 1.6MB 할당 × 30fps |
| `b/main.py:78` | 랜드마크 21개를 매 프레임 새 dict 리스트로 생성 |

**수정 방향**
- A 세션 TTL 정리 (P2-7과 통합 작업)
- `cv2.cvtColor(..., dst=buf)` 등 버퍼 재사용 검토
- **`tracemalloc`으로 1시간 연속 구동 후 메모리 증가량 측정** — 개선 전후 비교

---

### 🟢 P3-5. 성능 계측 수단 부재

**현재 어느 구간이 느린지 측정할 방법이 전혀 없다.** 최적화 이전에 계측이 먼저다.

**수정 방향 — 구간별 계측 포인트**

| 구간 | 측정 대상 |
|---|---|
| T1 | 모바일 전송 → A 수신 |
| T2 | base64 + JPEG 디코딩 |
| T3 | MediaPipe 추론 |
| T4 | B → C 왕복 |
| T5 | A → PC 릴레이 |

- 각 구간 소요시간을 구조화 로그로 기록 (샘플링 적용, §6 P4-5)
- `tests/perf/bench_pipeline.py`로 재현 가능한 벤치마크 스크립트 작성
- **§1.4의 19ms는 손 미검출 기준이므로, 손 포함 프레임으로 진짜 베이스라인을 다시 측정**

---

## 6. Pillar 4 — 로깅

> *"문제 발생 시 책임 소재를 밝히는 '법적 흔적'이자 디버깅의 유일한 열쇠다."*

### 진단: 컨테이너 3개의 로깅 수준이 전부 다르다

| 컨테이너 | 현재 상태 | 평가 |
|---|---|---|
| **A** | `print()` 4곳 (`main.py:68, 79, 87, 135`). 타임스탬프·레벨·구조 전무 | ❌ |
| **B** | `import logging`만 하고 **단 한 줄도 사용 안 함**. 완전 무로깅 | ❌ |
| **C** | 구조화 JSON 로깅 보유 (`main.py:17-33`) | ✅ 표준의 씨앗 |

Container C의 구현:

```python
# container_c_gesture/main.py:22-33
def log_event(level: str, session_id: str, event: str, detail: dict = None) -> None:
    record = {
        "ts": int(time.time() * 1000),
        "container": "C",
        "session_id": session_id,
        "level": level,
        "event": event,
        "detail": detail or {},
    }
    logger.info(json.dumps(record, ensure_ascii=False))
```

`ts / container / session_id / level / event / detail` 스키마는 쓸 만하다.
**이것을 `common/logging_setup.py`로 승격해 A와 B에 이식하는 것이 Pillar 4의 뼈대다.**

단, 현 구현에는 결함이 있다: **`level` 인자를 받지만 항상 `logger.info()`로 기록**한다(`main.py:33`).
즉 `level` 필드는 JSON 안의 문자열일 뿐 **실제 로그 레벨 필터링이 동작하지 않는다.**

---

### 작업 항목

#### 🔴 P4-1. 공통 구조화 로거 신설

`common/logging_setup.py` — C의 `log_event()`를 승격하되 다음을 보강한다.

- `level`을 **실제 로거 메서드에 매핑** (`logger.warning`, `logger.error` 등)
- 컨테이너 이름을 초기화 시 주입 (하드코딩 `"C"` 제거)
- traceback 자동 첨부 옵션
- 표준 필드 확정: `ts, container, level, event, session_id, detail, trace_id`

#### 🔴 P4-2. Container A의 `print()` 4곳 전량 교체

| 위치 | 현재 | 목표 |
|---|---|---|
| `a/main.py:68` | `print(f"[Session {sid}] PC 캔버스 연결됨!")` | `log.info("pc_connected", session_id=sid)` |
| `a/main.py:79` | `print(...PC 연결 해제됨)` | `log.info("pc_disconnected", ...)` |
| `a/main.py:87` | `print(...모바일 카메라 연결됨!)` | `log.info("mobile_connected", ...)` |
| `a/main.py:135` | `print(...모바일 연결 해제됨)` | `log.info("mobile_disconnected", ...)` |

추가로 §4 P2-1의 예외 분기 전부에 로그를 넣는다.

#### 🔴 P4-3. Container B에 로깅 전면 도입

현재 **완전 무로깅**이다. 최소 다음을 기록해야 한다.

- 모델 로드 시작/완료/실패 (소요시간 포함)
- 추론 소요시간 (샘플링)
- 검출 실패율 (집계)
- C 통신 결과 및 실패 사유
- detector 풀 획득/반납 (P3-2 도입 후)

#### 🟡 P4-4. 레벨 체계 실제 적용

| 레벨 | 용도 |
|---|---|
| `DEBUG` | 프레임 단위 상세 (좌표, 손가락 상태 플래그) |
| `INFO` | 세션 생명주기 (연결/해제/모델 로드) |
| `WARN` | 재시도 발생, 프레임 드롭, 성능 저하, degradation 진입 |
| `ERROR` | 체인 단절, 예외, 서킷 브레이커 open |

환경별 기본 레벨을 설정으로 분리 (`dev: DEBUG`, `prod: INFO`).

#### 🟡 P4-5. 프레임 단위 로그 폭주 방지

30fps × 세션 수만큼 초당 로그가 발생한다. 그대로 두면 디스크와 성능을 동시에 잡아먹는다.

- 프레임 단위 로그는 **N프레임당 1회 샘플링** (`frame_log_sample_rate: 30` → 초당 1회)
- 또는 1초 단위 집계 후 요약 기록 (평균 지연, 검출률, 드롭 수)
- **에러는 샘플링하지 않는다** (전량 기록)

#### 🟡 P4-6. 로그 로테이션

현재 `StreamHandler(stdout)`만 사용(`c/main.py:20`)하므로 파일로 남지 않는다.
컨테이너 재시작 시 로그가 소실되어 **사후 분석이 불가능**하다.

- `RotatingFileHandler` 추가 (`max_bytes: 10MB`, `backup_count: 5` — 설정화)
- stdout 병행 출력 유지 (`docker logs` 호환)
- 볼륨 마운트로 로그 영속화

#### 🟡 P4-7. 장애 시 컨텍스트 포함

예외 로그에 반드시 포함할 것:

- traceback 전문
- `session_id`, 프레임 시퀀스 번호
- 직전 `action` 및 상태
- 대상 URL / 타임아웃 설정값
- 재시도 횟수

#### 🟡 P4-8. 요청 추적성 (session_id 전파)

현재 `session_id`가 A→B→C로 **전달은 되지만 B에서는 로깅되지 않는다.**
하나의 프레임이 3개 컨테이너를 거치는 동안 **단일 요청으로 추적할 방법이 없다.**

- 모든 로그 레코드에 `session_id` 필수화
- 프레임 단위 `trace_id` 도입 검토 (A에서 생성 → B → C 전파)
- 이것이 갖춰지면 `grep trace_id` 하나로 전 구간 타임라인 복원 가능

---

## 7. The Engine — CI/CD (pytest + GitHub Actions)

> *"4대 축은 개발자의 수작업으로 단 한 번 달성하는 데 그쳐서는 안 된다.
> 코드가 변경될 때마다 자동으로 검증되고 유지되도록 만드는 것이 CI/CD의 핵심 역할이다."*

### 목표 파이프라인

```
Code Push → GitHub Actions → Lint(ruff) → Pytest(4대 축 검증) → Docker Build → 기동 검증
```

---

### 🔴 CI-0. 선결 과제: 환경 재현성 확보

**실측 증거 — 로컬과 컨테이너의 스택이 완전히 다르다.**

| 패키지 | requirements 명시 | 로컬 실제 설치 | 차이 |
|---|---|---|---|
| mediapipe | `==0.10.14` | **1.0.1** | 메이저 버전 상이 |
| numpy | `<2.0.0,>=1.26.0` | **2.3.1** | **제약 위반** |
| opencv-contrib-python | `==4.10.0.84` | **5.0.0.93** | 메이저 버전 상이 |

즉 **로컬에서 통과한 테스트가 컨테이너에서 통과한다는 보장이 전혀 없다.**
`fastapi>=0.110.0` 같은 하한만 있는 제약도 다수라, 오늘 빌드와 내일 빌드가 다를 수 있다.

**조치**
- [ ] `pip-tools`(또는 `uv`)로 `.in` → `.txt` 락파일 생성, 해시 고정
- [ ] 3개 컨테이너 간 공통 의존성 버전 정합성 확보
- [ ] 파이썬 버전 통일 (P1-6과 연계)
- [ ] **CI와 로컬이 동일 락파일을 사용**하도록 강제

> 이 항목이 해결되지 않으면 이후 모든 CI 결과가 신뢰할 수 없다. **반드시 최우선.**

---

### 테스트 전략 — 계층별 난이도가 다르므로 분리 대응

| 대상 | 난이도 | 이유 | 방식 |
|---|---|---|---|
| **Container C** | 🟢 쉬움 | `compute_gesture_logic`, `parse_landmarks`, `calculate_distance`가 **순수 함수**. I/O·외부 의존 없음 | 합성 랜드마크로 완전 커버. **커버리지 목표 높게** |
| **Container A** | 🟡 보통 | FastAPI/WebSocket 의존, B·C는 외부 | `TestClient` + `websocket_connect`, B/C는 모킹 |
| **Container B** | 🔴 어려움 | mediapipe 설치가 무겁고(수백 MB) 모델 다운로드 필요 | **별도 잡으로 분리 + pip·모델 캐시**, 고정 이미지 픽스처 사용 |

**C를 먼저, 그리고 두텁게 짠다.** 순수 로직이라 투자 대비 효과가 가장 크고,
이후 모든 리팩터링의 안전망이 된다.

---

### 작업 항목

#### CI-1. pytest 기반 구축
- [ ] `pytest.ini`, `tests/conftest.py`, 디렉터리 구조 (§2 참조)
- [ ] 합성 랜드마크 팩토리 픽스처 (`hand(index_up, middle_up, ...)` 형태)

#### CI-2. Container C 회귀 테스트 — **최우선**
- [ ] 6대 제스처 정상 케이스 (§1.3 표 그대로)
- [ ] 각 규칙의 **경계값** 테스트 (임계값 ±ε)
  - 엄지 폄/접힘 경계 `0.15` / `0.13`
  - ERASE 높이차 경계 `0.12`
- [ ] 엣지케이스: 빈 배열, 랜드마크 20개/22개, 좌표 범위 밖, `None`, 타입 혼재
- [ ] 입력 포맷 3종 호환 (`dict` / `list` / 객체) — `parse_landmarks` 분기 전부

> **§1.4의 수동 검증을 그대로 자동화하는 것이 목표.**

#### CI-3. EMA / 디바운스 단위 테스트
- [ ] EMA alpha 3단계 전환 경계 (`move_dist` `0.001` / `0.05` 전후)
- [ ] 3프레임 다수결 — 2표 미만에서는 상태 전이 없음
- [ ] **비대칭 무지연 펜-업 컷오프** (`c/main.py:206-213`) — `DRAW → HOVER`는 즉시,
      `HOVER → DRAW`는 다수결 대기. **이 비대칭성이 의도된 설계임을 테스트로 고정**
- [ ] 세션 격리 — 세션 A의 EMA 상태가 세션 B에 영향 없음

#### CI-4. Container A 통합 테스트
- [ ] WebSocket 연결/해제 생명주기
- [ ] PC↔모바일 세션 매칭 및 STATUS 전파
- [ ] 프레임 릴레이 (B 모킹)
- [ ] QR 생성 — 응답이 유효 PNG이고 **디코딩 시 올바른 URL**을 담고 있는지
- [ ] `rooms` 정리 — 양쪽 해제 후 키가 삭제되는지 (P2-7 회귀 방지)

#### CI-5. **4대 축 자체를 검증하는 테스트**

청사진의 핵심 요구. 개선이 **되돌아가지 않도록 자동 강제**한다.

| 축 | 검증 내용 |
|---|---|
| **P1** | 소스 전체에 IP 리터럴(`192.168.` 등)·포트 리터럴이 없는지 정적 검사 |
| **P1** | 설정 파일 부재/손상 시 명확한 에러로 실패하는지 |
| **P2** | `except ...: pass` 패턴이 소스에 없는지 정적 검사 |
| **P2** | B 또는 C를 강제 다운시켰을 때 **A가 죽지 않고 degrade 응답**을 내는지 |
| **P3** | **동시 2세션에서 랜드마크 오염이 없는지** (P3-2 회귀 방지 — 핵심) |
| **P3** | 이벤트 루프가 블로킹되지 않는지 (추론 중에도 `/health` 응답) |
| **P4** | 예외 발생 시 로그에 traceback + `session_id`가 남는지 |
| **P4** | 로그가 유효한 JSON이고 필수 필드를 전부 갖는지 |

#### CI-6. GitHub Actions 워크플로
- [ ] `.github/workflows/ci.yml`
- [ ] 트리거: `push`(main), `pull_request`
- [ ] 잡 구성

```
lint       : ruff check + format --check          (수십 초)
test-light : Container A/C 테스트                  (1~2분)
test-vision: Container B 테스트 (캐시 적용)         (별도 잡)
build      : 3개 이미지 빌드 + 기동 + /health 확인   (test 통과 후)
```

#### CI-7. 잡 분리 및 캐시
- [ ] `actions/cache`로 pip 캐시
- [ ] MediaPipe 모델 파일 캐시 (매 실행 다운로드 방지 — 현재 import 시 다운로드하므로 필수)
- [ ] Docker layer 캐시 (`docker/build-push-action` + GHA 캐시)
- [ ] 무거운 vision 잡은 필요 시에만 실행 (경로 필터)

#### CI-8. 커버리지
- [ ] `pytest-cov` 도입, 리포트 생성
- [ ] Container C는 높은 임계치 설정 (순수 로직이므로 달성 가능)
- [ ] 전체 임계치는 현실적으로 (B는 커버가 어렵다)

#### CI-9. 빌드 및 기동 검증
- [ ] 3개 이미지 빌드 성공
- [ ] `docker compose up -d` 후 **3개 전부 healthy** 도달 확인
- [ ] 스모크 테스트: `/api/info`, `/api/qr`, WebSocket 왕복 1회

> **CD 범위 주의:** 실제 클라우드 배포는 이번 범위 밖(§10).
> "Deploy" 단계는 **빌드 산출물이 실제로 기동 가능함을 검증**하는 데까지로 정의한다.

---

## 8. 실행 순서

의존 관계상 **아래 순서를 지켜야 한다.** 특히 테스트를 먼저 깔아야 이후 리팩터링이 안전하다.

| 단계 | 작업 | 왜 이 순서인가 |
|---|---|---|
| **0** | 베이스라인 커밋 + 작업 브랜치 분리 | 되돌릴 지점 확보 |
| **1** | `CI-0` 의존성 고정 | 이후 모든 테스트 결과의 신뢰 기반 |
| **2** | `CI-1` → `CI-2` → `CI-3` **Container C 테스트 먼저** | **안전망 없이 리팩터링 금지.** C는 순수 로직이라 빠르게 짤 수 있음 |
| **3** | `P4-1` 공통 로거 → `P4-2`, `P4-3` A/B 이식 | 이후 모든 작업의 **관측 수단**. 로그 없이는 개선 검증 불가 |
| **4** | `P1-1` IP → `P1-2` 포트 → `P1-3` 임계값 설정화 | 가장 체감이 큰 개선. 2단계 테스트가 회귀를 막아줌 |
| **5** | `P2-1` 침묵 예외 제거 → `P2-6` 스키마 → `P2-3` degradation → `P2-2` retry | 3단계 로깅이 있어야 예외 처리 검증 가능 |
| **6** | `P3-5` 계측 → `P3-2` **세션 격리** → `P3-1` 블로킹 해소 → `P3-3` 백프레셔 | **계측 먼저.** P3-2가 P3-1보다 앞서야 함(§5 참조) |
| **7** | `P2-5` 헬스체크 + `P2-7` 세션 정리 + compose 정비 | |
| **8** | `CI-4`, `CI-5` 통합·4대축 테스트 추가 | 개선된 동작을 테스트로 고정 |
| **9** | `CI-6`~`CI-9` GitHub Actions 연결, 전체 녹색 | 최종 자동화 |
| **10** | README 갱신, 최종 E2E 실기 검증 | §1.4 대비 회귀 없음 확인 |

### 시간이 부족할 경우의 우선순위

전부 못 하더라도 다음 순서로 잘라낸다.

1. **반드시** — `CI-0`, `CI-2`, `P4-1~3`, `P1-1`, `P2-1`, `CI-6`
2. **가능하면** — `P3-2`, `P3-1`, `P2-3`, `P1-3`, `CI-5`
3. **여유 있으면** — `P3-3`, `P3-4`, `P4-5~8`, `CI-7~9`

---

## 9. 완료 기준 (Definition of Done)

### 정량

- [ ] 소스코드에 IP 주소·포트 리터럴 **0개** (정적 테스트로 검증)
- [ ] `except ...: pass` 패턴 **0개**
- [ ] Container C 테스트 커버리지 **목표치 달성**
- [ ] `pytest` 전체 통과, GitHub Actions **녹색**
- [ ] 동시 **2세션**에서 랜드마크 오염 **0건** (테스트로 증명)

### 정성

- [ ] 3개 컨테이너가 **동일한 구조화 로그 포맷** 사용, 레벨 필터링 실제 동작, 로테이션 확인
- [ ] `session_id`로 A→B→C 전 구간 타임라인 복원 가능
- [ ] B 또는 C를 강제 종료해도 **A가 살아남고 사용자에게 상태가 통지**됨
- [ ] 설정 파일만 바꿔서 다른 네트워크·다른 포트로 실행 가능
- [ ] 이벤트 루프 블로킹 해소, **개선 전후 처리량·p95 지연 수치 기록**

### 최종 관문

- [ ] **§1.4 베이스라인 동작이 100% 보존됨** — 실제 스마트폰으로 6대 제스처 전부 재검증,
      체감 지연 및 드로잉 품질이 이전과 동일하거나 개선

---

## 10. 범위 밖 (이번에 하지 않을 것)

의도적으로 제외한다. 청사진의 목표는 **기능 추가가 아니라 엔지니어링 기준의 확립**이다.

| 항목 | 이유 |
|---|---|
| 새로운 제스처 추가 | 기능 추가는 범위 밖 |
| 제스처 판별 규칙 변경 | 베이스라인 보존 원칙 위배 |
| UI/UX 리디자인 | 장애 상태 배너 외 변경 없음 |
| 인증·계정 시스템 | 별도 과제 |
| 실제 클라우드 배포 | CD는 **빌드·기동 검증까지**. 배포 대상 인프라는 범위 외 |
| 정식 SSL 인증서 발급 | 로컬 자체서명 유지 (영속화만 개선) |
| 다중 손 인식 | `num_hands=1` 유지 |
| GPU 가속 | 컨테이너에 GPU 없음. CPU 최적화만 |

---

## 부록 A. 이슈 인덱스

| ID | 우선순위 | 위치 | 요약 |
|---|---|---|---|
| **P1-1** | 🔴 | `a/main.py:20`, `pc.html:293-295`, `compose` | IP 하드코딩 3곳 중복. QR이 우연히만 동작 |
| **P1-2** | 🔴 | 5개 계층 8곳 | 포트 번호 산재 |
| **P1-3** | 🟡 | `c/main.py:57,65,126,127,151,165,174,212,226-233` | 제스처 임계값 매직넘버 9종 |
| **P1-4** | 🟡 | `mobile.html:279-290` | 해상도·품질·전송주기 고정 |
| **P1-5** | 🟡 | `a/main.py:93`, `b/main.py:86` | 타임아웃 0.2s/0.3s — 비현실적 |
| **P1-6** | 🟢 | `b/main.py:1-20`, 전역 | 미사용 import 4개, 공통 모듈 부재, 파이썬 버전 불일치, 린터 없음 |
| **P2-1** | 🔴 | `a/main.py:126-127` | `except Exception: pass` — 전 장애 무음 처리 |
| **P2-2** | 🔴 | A→B→C 전 구간 | 재시도 부재 |
| **P2-3** | 🔴 | 전체 | Graceful Degradation 부재. 장애 시 무통보 정지 |
| **P2-4** | 🟡 | `b/main.py:57-63` | base64 검증·크기 상한 없음 |
| **P2-5** | 🟡 | `a/main.py`, `compose` | A `/health` **404**, compose healthcheck 없음 |
| **P2-6** | 🟡 | `a/main.py:106-107`, `b/main.py:98` | `x:null,y:null` 누수. 서비스 간 응답 계약 부재 |
| **P2-7** | 🟡 | `a/main.py:65,76-79,129-131` | `rooms` 만료 없음 — 새로고침마다 영구 누적 |
| **P2-8** | 🟢 | `b/main.py:38-45,115-118`, `entrypoint.sh` | import 시 모델 다운로드, 인증서 미영속, deprecated API, 예외정보 손실 |
| **P3-1** | 🔴 | `b/main.py:93-96` | 이벤트 루프 블로킹 — 동시성 붕괴 |
| **P3-2** | 🔴 | `b/main.py:46-54, 72` | 전역 detector + VIDEO 모드 → **세션 간 추적 오염** + 타임스탬프 단조성 위반 |
| **P3-3** | 🟡 | `mobile.html:284-290` | 백프레셔 없음 — 지연 누적 |
| **P3-4** | 🟡 | `a/main.py:65`, `b/main.py:57-78` | 메모리 누수 / 프레임당 과다 복사 |
| **P3-5** | 🟢 | 전체 | 성능 계측 수단 없음 |
| **P4-1** | 🔴 | `c/main.py:17-33` → `common/` | 공통 구조화 로거 신설 (레벨 매핑 결함 수정 포함) |
| **P4-2** | 🔴 | `a/main.py:68,79,87,135` | `print()` 4곳 |
| **P4-3** | 🔴 | `b/main.py` 전체 | **완전 무로깅** |
| **P4-4** | 🟡 | `c/main.py:33` | `level` 인자가 실제 로그 레벨에 반영되지 않음 |
| **P4-5** | 🟡 | 전체 | 프레임 단위 로그 폭주 방지 (샘플링) |
| **P4-6** | 🟡 | `c/main.py:20` | stdout 전용 — 로테이션·영속화 없음 |
| **P4-7** | 🟡 | 전체 | 예외 시 traceback·컨텍스트 부재 |
| **P4-8** | 🟡 | `b/main.py` | `session_id` 전파는 되나 로깅 안 됨 — 추적 불가 |
| **CI-0** | 🔴 | `requirements/*` | **의존성 미고정. 로컬↔컨테이너 스택 불일치** |
| **CI-1~9** | 🔴 | 저장소 전체 | **테스트 0개, CI 없음** |

---

## 부록 B. 참고 — 베이스라인 재측정 절차

리팩터링 전후 비교를 위해 동일 조건으로 재현할 수 있어야 한다.

```bash
# 1. 스택 기동
docker compose up --build -d
docker compose ps          # 3개 Up 확인

# 2. 헬스 확인
curl -sk https://localhost:8443/api/info
docker exec air-canvas-vision  curl -s localhost:8001/health
docker exec air-canvas-gesture curl -s localhost:8002/health

# 3. 체인 왕복 지연 (tests/perf/bench_pipeline.py 로 대체 예정)
#    - 손 미검출 프레임: 베이스라인 19ms
#    - 손 포함 프레임: 미측정 → 성능 작업 착수 시 최초 측정 필요

# 4. 실기 E2E
#    PC: https://localhost:8443  (인증서 경고 통과)
#    폰: QR 스캔 → 카메라 권한 → 6대 제스처 전부 확인
```

**측정 시 고정할 조건:** 동일 조명, 동일 거리, 동일 단말, 동일 Wi-Fi.
성능 수치는 조건이 다르면 비교 의미가 없다.
