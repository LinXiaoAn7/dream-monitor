FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DM_HOST=0.0.0.0 \
    DM_AUTO=1

WORKDIR /app

# 纯标准库 + PyYAML，依赖极少
RUN pip install --no-cache-dir pyyaml==6.0.2

COPY *.py ./
COPY config.yaml ./
COPY ui ./ui

# data 目录用于 SQLite，挂卷持久化
RUN mkdir -p /app/data

EXPOSE 8770
CMD ["python", "gui_server.py", "--no-open"]
