FROM python:3.11-slim

WORKDIR /app

# 关联 GitHub 仓库，允许 GitHub Actions 将编译好的镜像自动绑定并推送到本仓库的 GHCR 包管理器
LABEL org.opencontainers.image.source=https://github.com/godeluoo1/cf

# 安装底层系统加解密所需的编译环境以防万一，但大部分情况下 wheels 已内置
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libssl-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 设置默认端口环境变量，确保容器启动后直接开启 24h 持续 Web 守护模式
ENV PORT=8080

# 默认容器暴露 8080 端口
EXPOSE 8080

CMD ["python", "cf.py"]
