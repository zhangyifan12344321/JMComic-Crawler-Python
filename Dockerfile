FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-api.txt requirements-api.txt
RUN pip install --no-cache-dir -r requirements-api.txt

COPY pyproject.toml README.md setup.py ./ 
COPY src ./src
COPY assets ./assets
COPY requirements-dev.txt requirements-dev.txt

RUN pip install --no-cache-dir -e .

COPY api_service ./api_service

EXPOSE 8000

ENV JM_OPTION_PATH=""
ENV JM_DOWNLOAD_DIR="/app/data"

CMD ["uvicorn", "api_service.main:app", "--host", "0.0.0.0", "--port", "8000"]

