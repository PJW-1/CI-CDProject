FROM python:3.11-slim

WORKDIR /app

# OpenSSL 설치 (SSL 인증서 자동 생성용)
RUN apt-get update && apt-get install -y openssl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x entrypoint.sh

EXPOSE 8443

ENTRYPOINT ["./entrypoint.sh"]
