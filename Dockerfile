FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY dharma/ dharma/

RUN pip install --no-cache-dir .

ENTRYPOINT ["dharma"]
