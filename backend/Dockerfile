FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY backend/requirements.txt ./requirements.txt
COPY data/requirements.txt ./data-requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r data-requirements.txt
COPY backend/app ./app
COPY data/gtfs ./data/gtfs
COPY data/metro ./data/metro
COPY otp/scripts ./otp/scripts
COPY otp/config ./otp/config
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
