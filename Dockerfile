FROM python:3.11-slim

WORKDIR /app

# System deps (none heavy; keep slim)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY run.bat ./

# Data dirs (mounted as volumes in compose; sessions are per-visitor)
RUN mkdir -p data/sessions && touch data/.gitkeep

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["python", "-m", "src.web"]
