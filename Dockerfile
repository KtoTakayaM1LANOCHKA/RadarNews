FROM python:3.12-slim
WORKDIR /srv/newsradar
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV DB_PATH=/data/newsradar.db UPLOAD_DIR=/data/uploads DEMO_MODE=0 DEMO_OTP=0
VOLUME /data
EXPOSE 8000
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]
