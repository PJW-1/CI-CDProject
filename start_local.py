import os
import sys
import subprocess
import time
import socket

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CERTS_DIR = os.path.join(BASE_DIR, "certs")
os.makedirs(CERTS_DIR, exist_ok=True)

cert_file = os.path.join(CERTS_DIR, "cert.pem")
key_file = os.path.join(CERTS_DIR, "key.pem")

# 1. 자체 SSL 인증서 생성 (없는 경우)
if not os.path.exists(cert_file) or not os.path.exists(key_file):
    print("[*] 로컬 HTTPS 전용 SSL 인증서 생성 중...")
    try:
        # OpenSSL 명령어 실행
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", key_file, "-out", cert_file, "-days", "3650",
            "-subj", "/C=KR/ST=Seoul/L=Seoul/O=AirCanvas/OU=Dev/CN=localhost"
        ], check=True)
        print("[+] SSL 인증서 생성 완료!")
    except Exception as e:
        print(f"[!] OpenSSL 자동 생성 실패 ({e}). HTTP 모드로 실행하거나 OpenSSL을 설치하세요.")

local_ip = get_local_ip()
print("="*65)
print(f" [🖐️ Air Canvas 로컬 마이크로서비스 3개 구동 시작]")
print(f" - 내 컴퓨터 로컬 IP: {local_ip}")
print(f" - PC 접속 주소: https://localhost:8443 또는 https://{local_ip}:8443")
print("="*65)

# 환경 변수 설정
env_a = os.environ.copy()
env_a["CONTAINER_B_URL"] = "http://127.0.0.1:8001/analyze"

env_b = os.environ.copy()
env_b["CONTAINER_C_URL"] = "http://127.0.0.1:8002/gesture"

procs = []

try:
    # Container C: Gesture Engine (8002)
    print("[*] Container C (동작 판별 엔진 - 포트 8002) 시작...")
    p_c = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8002"],
        cwd=os.path.join(BASE_DIR, "container_c_gesture")
    )
    procs.append(p_c)

    # Container B: Vision Engine (8001)
    print("[*] Container B (MediaPipe 비전 엔진 - 포트 8001) 시작...")
    p_b = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8001"],
        cwd=os.path.join(BASE_DIR, "container_b_vision"),
        env=env_b
    )
    procs.append(p_b)

    time.sleep(1)

    # Container A: Web & Signaling Server (8443 HTTPS)
    print("[*] Container A (웹/보안 서버 - 포트 8443 HTTPS) 시작...")
    cmd_a = [sys.executable, "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8443"]
    if os.path.exists(cert_file) and os.path.exists(key_file):
        cmd_a.extend(["--ssl-keyfile", key_file, "--ssl-certfile", cert_file])

    p_a = subprocess.Popen(
        cmd_a,
        cwd=os.path.join(BASE_DIR, "container_a_web"),
        env=env_a
    )
    procs.append(p_a)

    print("\n[+] 3개 마이크로서비스가 모두 성공적으로 실행되었습니다!")
    print(f"👉 브라우저를 열고 https://localhost:8443 에 접속하세요.")
    print("👉 종료하려면 터미널에서 Ctrl + C 를 누르세요.\n")

    while True:
        time.sleep(1)

except KeyboardInterrupt:
    print("\n[*] 마이크로서비스 종료 중...")
    for p in procs:
        p.terminate()
    print("[*] 모든 서비스가 종료되었습니다.")
