# Коннектор-воркер ABOP: исполняет команды навыков из шины (сервер-2, рядом с Redpanda).
FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN pip install --upgrade pip && pip install "aiokafka>=0.10" "httpx>=0.27"
COPY worker.py /app/worker.py
VOLUME ["/data"]
EXPOSE 9105
CMD ["python", "/app/worker.py"]
