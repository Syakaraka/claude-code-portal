FROM python:3.12-slim

# Portal HTTP 端口（容器内视角）
#   - 与 docker-compose.yml 的 `PORTAL_CONTAINER_PORT` 一致（默认 9900）
#   - 改了要 rebuild portal 镜像：`docker compose build portal`
#   - host 视角的端口在 docker-compose.yml 的 `PORTAL_HOST_PORT`（可以不同）
ARG PORTAL_CONTAINER_PORT=9900
ENV PORTAL_CONTAINER_PORT=${PORTAL_CONTAINER_PORT}

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY templates/ ./templates/
COPY static/ ./static/

EXPOSE ${PORTAL_CONTAINER_PORT}

# 内部低流量工具，gunicorn 2 worker 足够
# 用 shell form 让 ${PORTAL_CONTAINER_PORT} 在 runtime 解析（不写死到 image layer）
CMD ["sh", "-c", "exec gunicorn -b 0.0.0.0:${PORTAL_CONTAINER_PORT} -w 2 --timeout 120 app:app"]