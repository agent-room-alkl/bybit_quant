# 智能量化交易系统 Docker 配置
FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

# 复制源码
COPY . .

# 创建数据和日志目录
RUN mkdir -p /app/data /app/logs

# Web端口

# 环境变量
ENV PYTHONUNBUFFERED=1

# 启动主程序（交易+看板一体化）
EXPOSE 8000
ENTRYPOINT ["python", "/app/main.py"]
