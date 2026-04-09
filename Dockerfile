FROM python:3.11-slim

WORKDIR /app

# System deps — nothing heavy needed for Telethon
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# /data is where the Railway volume will be mounted (holds the .session file)
RUN mkdir -p /data

ENV PYTHONUNBUFFERED=1
ENV SESSION_PATH=/data/telegram_session

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
