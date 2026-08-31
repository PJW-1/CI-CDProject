#!/usr/bin/env python3
"""
Docker 스택 기동 헬퍼.

왜 필요한가
    컨테이너 안에서 LAN IP를 탐지하면 Docker 내부 주소(172.x.x.x)가 잡힌다.
    QR에 그 주소를 넣으면 폰이 접속할 수 없다.
    그렇다고 소스에 IP를 하드코딩하면 고도화 이전 상태로 되돌아간다.

해법
    IP 탐지는 "호스트에서" 수행하고, 환경변수로 컨테이너에 주입한다.
    소스코드에는 여전히 IP 리터럴이 하나도 없다.

사용법
    python scripts/compose_up.py              # 자동 탐지 후 기동
    python scripts/compose_up.py --ip 1.2.3.4 # 수동 지정
    python scripts/compose_up.py --down       # 종료
"""

import argparse
import os
import socket
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def detect_lan_ip(fallback: str = "127.0.0.1") -> str:
    """UDP 소켓의 로컬 바인딩 주소로 LAN IP 탐지. 실제 패킷은 전송되지 않는다."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return fallback


def main() -> int:
    parser = argparse.ArgumentParser(description="Air Canvas Docker 스택 기동")
    parser.add_argument("--ip", help="호스트 LAN IP 수동 지정 (미지정 시 자동 탐지)")
    parser.add_argument("--down", action="store_true", help="스택 종료")
    parser.add_argument("--build", action="store_true", help="이미지 재빌드")
    args = parser.parse_args()

    env = os.environ.copy()

    if args.down:
        cmd = ["docker", "compose", "down"]
    else:
        host_ip = args.ip or detect_lan_ip()
        env["HOST_IP"] = host_ip

        if host_ip.startswith("127."):
            print("[!] LAN IP 탐지 실패. 루프백으로 기동합니다.")
            print("    폰에서 QR로 접속하려면 --ip 로 직접 지정하세요.")
        else:
            print(f"[+] 호스트 LAN IP 탐지: {host_ip}")
            print(f"    PC 접속 : https://localhost:8443")
            print(f"    폰 접속 : https://{host_ip}:8443")

        cmd = ["docker", "compose", "up", "-d"]
        if args.build:
            cmd.insert(3, "--build")

    print(f"[*] 실행: {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=REPO_ROOT, env=env)


if __name__ == "__main__":
    sys.exit(main())
