# syntax=docker/dockerfile:1.7
# One image definition for the three runtimes (orchestrator, market-analyst, quant-mcp). AgentCore Runtime requires linux/arm64.
#   docker buildx build --platform linux/arm64 -f docker/Dockerfile \
#     --build-arg APP_MODULE=src.quant_mcp.app --build-arg APP_PORT=8000 --build-arg SERVICE_NAME=quant-mcp .
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim-bookworm AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv && /opt/venv/bin/pip install -r /tmp/requirements.txt

FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim-bookworm
ARG APP_MODULE
ARG APP_PORT
ARG SERVICE_NAME
RUN test -n "$APP_MODULE" && test -n "$APP_PORT" && test -n "$SERVICE_NAME" \
 && groupadd --gid 1000 app && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin app
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    APP_MODULE=${APP_MODULE} APP_PORT=${APP_PORT} \
    OTEL_SERVICE_NAME=${SERVICE_NAME} \
    OTEL_PYTHON_DISTRO=aws_distro OTEL_PYTHON_CONFIGURATOR=aws_configurator \
    OTEL_PROPAGATORS=tracecontext,baggage,xray \
    OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=logging \
    OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf OTEL_TRACES_EXPORTER=otlp \
    AGENT_OBSERVABILITY_ENABLED=true \
    BEDROCK_MODEL_ID=us.amazon.nova-micro-v1:0
WORKDIR /app
COPY --from=deps /opt/venv /opt/venv
COPY --chown=1000:1000 src/ ./src/
COPY --chown=1000:1000 data/market_reference.json ./data/market_reference.json
USER 1000
EXPOSE ${APP_PORT}
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"APP_PORT\"]}/ping', timeout=2)"
CMD ["sh", "-c", "exec opentelemetry-instrument python -m ${APP_MODULE}"]
