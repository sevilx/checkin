# =============================================================================
# 吾爱破解「瑞数 WZWS」真浏览器 PoC 专用镜像
#
# 【这是什么】
#   一个纯实验用的独立镜像，只用来回答一个问题：
#   "容器里的 headless Chromium + stealth 到底能不能过 52pojie 的瑞数挑战？"
#
# 【和主项目的关系】
#   与 checkin-hub 主镜像（docker/Dockerfile）完全无关。
#   不装在主镜像里、不进 docker-compose、不改 requirements.txt、不改任何 checkin/sites/*.py。
#   本目录随时可以整个删掉，主项目不受任何影响。
#
# 【体积警告】
#   playwright install --with-deps chromium 会把 Chromium 和一堆系统库拉进来，
#   镜像大约 +1GB。这就是"真浏览器方案"的代价，PoC 阶段先量一下再决定值不值。
#
# 【凭据】
#   这里不写任何账号 / Cookie / 口令 / 代理地址。
#   凭据只在运行时由环境变量注入（见 README），且不落到镜像层。
#   容器以 root 运行，Chromium 需要 --no-sandbox —— 在 probe.py 里处理，不在这里。
# =============================================================================
FROM python:3.12-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates tzdata xvfb \
    && rm -rf /var/lib/apt/lists/*

# playwright-stealth：抹掉 navigator.webdriver / CDP 痕迹等常见自动化特征。
# 注意：瑞数和 Cloudflare Turnstile 不是一套信号，stealth 不代表一定能过，实测为准。
RUN pip install --no-cache-dir playwright playwright-stealth \
    && playwright install --with-deps chromium

WORKDIR /poc

COPY probe.py /poc/probe.py

# profile（长时效 Discuz 票据 + 浏览器算出的短时效 wzws_cid）和 shots（截图）
# 都由宿主挂载进来；这里只建空目录占位，任何真实数据都不进镜像。
RUN mkdir -p /poc/profile /poc/shots

CMD ["python", "probe.py"]
