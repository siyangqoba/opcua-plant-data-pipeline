FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY plantdata ./plantdata
COPY config ./config
RUN pip install --no-cache-dir . && useradd --create-home plant && mkdir -p /data /app/reports && chown plant /data /app/reports
USER plant
CMD ["plant-collector", "--config", "config/docker.yaml"]
