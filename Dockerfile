# tracker替换 —— 网页版镜像（纯 web：不装任何图形库，容器里不弹窗口）
# 用法见 README 的 Docker 一节；界面就是浏览器网页，账号密码登录
FROM python:3.11-slim

LABEL org.opencontainers.image.title="tracker-replace" \
      org.opencontainers.image.description="批量更换 qBittorrent / Transmission 种子 tracker 的网页工具" \
      org.opencontainers.image.source="https://github.com/littleskyfish/tracker-replace"

COPY tracker_replace.py tracker_ui.py /opt/app/
RUN printf '#!/bin/sh\n# 启动时把脚本放进 /data 卷（工作目录），clients.json/state 都住在这儿，升级镜像不丢\nmkdir -p /data\ncp -f /opt/app/tracker_replace.py /opt/app/tracker_ui.py /data/\ncd /data\nexec python3 tracker_replace.py "$@"\n' > /entrypoint.sh \
    && chmod +x /entrypoint.sh

VOLUME /data
EXPOSE 8642

# 默认：网页监听 0.0.0.0:8642，浏览器访问，非本机访问强制网页登录。
# 密码：不设 TR_UI_PASS 就每次启动随机生成，用 docker logs tracker-replace 查看。
# 也可以换命令跑 CLI，例：docker run --rm ghcr.io/... doctor -c /data/config.json
ENTRYPOINT ["/entrypoint.sh"]
CMD ["ui", "--ui-host", "0.0.0.0", "--ui-port", "8642", "--no-browser"]
