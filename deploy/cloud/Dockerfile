FROM python:3.12.14-slim
WORKDIR /app
COPY workbench /app/workbench
COPY web /app/web
COPY plugin_worker.py /app/plugin_worker.py
COPY srf /app/srf
COPY config /app/config
COPY data /app/data
COPY docs /app/docs
RUN groupadd --gid 10001 neuromorph && useradd --uid 10001 --gid 10001 --no-create-home neuromorph && mkdir -p /state && chown 10001:10001 /state
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 NEUROMORPH_STATE_DIR=/state PORT=8080
USER 10001:10001
EXPOSE 8080
VOLUME ["/state"]
CMD ["python", "-m", "workbench.cloud_app"]
