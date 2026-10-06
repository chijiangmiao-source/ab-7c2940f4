FROM python:3.11-slim

WORKDIR /workspace

# 仅使用 Python 标准库，无第三方依赖；拷贝全部源码以便同一镜像既可提供
# 交付页面，也可运行 verify（构建检查 / 冒烟复核 / 代码测试）。
COPY app ./app
COPY tests ./tests
COPY verify ./verify
COPY build.py ./build.py

ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    DATA_DIR=/data \
    TOTAL_QUOTA=10

EXPOSE 8080
VOLUME ["/data"]

CMD ["python", "-m", "app.server"]
