# 一个镜像里同时带 Python（签到主控 + 面板）和 Node（189 运行时）
FROM node:20-bookworm-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CHECKIN_DATA_DIR=/app/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-requests python3-pip tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# curl_cffi：复刻浏览器的 TLS / HTTP2 指纹，过 Cloudflare 用（NodeSeek、LinuxDo）。
# 这一步失败不阻塞构建 —— 程序会自动退回 requests，只是 Cloudflare 站点更容易被拦。
RUN pip3 install --no-cache-dir --break-system-packages "curl_cffi>=0.7.0" \
    || echo "WARN: curl_cffi 安装失败，Cloudflare 站点可能被拦，见 README 常见问题"

WORKDIR /app

# 先装依赖，改代码时不会重复下载；按 lock 精确安装
COPY runtime189/package.json runtime189/package-lock.json runtime189/
RUN npm ci --prefix runtime189 --omit=dev --no-audit --no-fund

COPY . .

RUN chmod +x docker/entrypoint.sh \
    && mkdir -p /app/data

EXPOSE 8080

ENTRYPOINT ["/app/docker/entrypoint.sh"]
