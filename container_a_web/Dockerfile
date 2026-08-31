FROM python:3.11-slim

WORKDIR /app

# stdout 블록 버퍼링 방지. 이것이 없으면 짧은 로그가 버퍼에 갇혀
# docker logs 에 나타나지 않는다 (수정 전 Container A의 print()가 실제로 그랬다).
ENV PYTHONUNBUFFERED=1

# OpenSSL 설치 (SSL 인증서 자동 생성용)
RUN apt-get update && apt-get install -y openssl && rm -rf /var/lib/apt/lists/*

# 빌드 컨텍스트가 저장소 루트다. 공용 모듈(common/)을 3개 컨테이너가 함께 쓰기 위함이다.
COPY container_a_web/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY config/ ./config/
COPY container_a_web/ .

RUN chmod +x entrypoint.sh

EXPOSE 8443

ENTRYPOINT ["./entrypoint.sh"]
