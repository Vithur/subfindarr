FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# unrar-free 用于部分 rar；7z 走 py7zr，zip 走标准库
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        tzdata \
        p7zip-full \
        unrar-free \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY config.example.yaml /app/config.example.yaml

RUN mkdir -p /config /logs /subs-out /config/cache

EXPOSE 8097

CMD ["python", "-m", "app.main"]
