FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8082
CMD ["gunicorn", "--bind", "0.0.0.0:8082", "--workers", "2", "app:app"]
