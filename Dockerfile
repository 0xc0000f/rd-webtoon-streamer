FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && mkdir -p cache_series dl_farm_w0 zips
COPY farm_ridi.py farm_worker.py app.py lezhin_dl.py titles.json ./
ENV PYTHONDONTWRITEBYTECODE=1 PORT=3000 MAX_WORKERS=16 PYTHONUNBUFFERED=1
EXPOSE 3000
CMD ["python3","app.py"]
