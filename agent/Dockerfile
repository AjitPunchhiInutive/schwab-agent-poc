FROM python:3.11-slim AS base

RUN groupadd -r agent && useradd -r -g agent -s /sbin/nologin agent

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chown -R agent:agent /app

USER agent

ENV PORT=8080

EXPOSE 8080

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
