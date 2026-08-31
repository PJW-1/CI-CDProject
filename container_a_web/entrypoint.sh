#!/bin/bash
set -e

mkdir -p /certs

# SSL 인증서가 없으면 컨테이너 시작 시 즉시 자동 생성 (HTTPS 활성화)
if [ ! -f /certs/cert.pem ] || [ ! -f /certs/key.pem ]; then
    echo "[Container A] 로컬 전용 자체 SSL 인증서 생성 중..."
    openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout /certs/key.pem \
        -out /certs/cert.pem \
        -days 3650 \
        -subj "/C=KR/ST=Seoul/L=Seoul/O=AirCanvas/OU=Dev/CN=localhost"
    echo "[Container A] SSL 인증서 생성 완료!"
fi

echo "[Container A] HTTPS 웹 서버 시작 (포트 8443)..."
exec uvicorn main:app --host 0.0.0.0 --port 8443 --ssl-keyfile /certs/key.pem --ssl-certfile /certs/cert.pem
