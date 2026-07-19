FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

ENV AGENT_TOKEN=change-me
ENV BIND_HOST=0.0.0.0
ENV BIND_PORT=9105

EXPOSE 9105
CMD ["sh", "-c", "uvicorn app:app --host ${BIND_HOST} --port ${BIND_PORT}"]
