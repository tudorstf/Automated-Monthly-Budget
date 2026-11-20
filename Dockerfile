FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py app.py .env.example ./
COPY src/ ./src/

RUN mkdir -p /app/data /app/failed_receipts /app/logs

EXPOSE 8765

CMD ["python", "app.py", "--host", "0.0.0.0", "--port", "8765"]
