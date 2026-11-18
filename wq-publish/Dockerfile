FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY wq_publish ./wq_publish
COPY wsgi.py ./

ENV WQ_DATA_DIR=/data \
    WQ_DOWNSTREAM_DIR=/downstream
RUN mkdir -p /data /downstream

EXPOSE 5000

# 单实例 waitress：并发正确性依赖同一 WQ_DATA_DIR / WQ_DOWNSTREAM_DIR，
# 多容器部署时必须共享这两个卷。
CMD ["waitress-serve", "--host", "0.0.0.0", "--port", "5000", "--call", "wsgi:create_app"]
