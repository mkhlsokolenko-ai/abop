FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY pyproject.toml ./
COPY server ./server
COPY portal_api ./portal_api
RUN pip install --upgrade pip && pip install .

EXPOSE 8787
CMD ["ape-mcp"]
