FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py index.html ./
ENV BHYVE_CONFIG=/data/config.json
EXPOSE 8000
VOLUME /data
# Assembles /data/config.json from HA add-on options (or use a mounted config.json), then serves.
ENTRYPOINT ["python", "container_entrypoint.py"]
