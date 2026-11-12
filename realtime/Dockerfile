FROM gcc:14-bookworm AS cpp-build
WORKDIR /build
COPY realtime/cpp/delay_engine.cpp ./
RUN g++ -std=c++17 -O2 -Wall -Wextra -Werror delay_engine.cpp -o delay-engine
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_ROOT=/data REALTIME_DIR=/realtime-data
WORKDIR /service
COPY realtime/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY realtime/realtime ./realtime
COPY data/gtfs/certs/spb_gtfs_ca_bundle.pem ./certs/ca.pem
COPY --from=cpp-build /build/delay-engine /usr/local/bin/delay-engine
USER 10001:10001
EXPOSE 8001
CMD ["uvicorn", "realtime.main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1"]
