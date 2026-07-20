# syntax=docker/dockerfile:1.7

FROM node:22-slim

ARG APT_MIRROR=
ARG NPM_REGISTRY=https://registry.npmmirror.com/

# 1. 配置 APT 镜像源 (安装 git, ca-certificates)
RUN if [ -n "${APT_MIRROR}" ]; then \
      sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" \
        /etc/apt/sources.list \
        /etc/apt/sources.list.d/*.list \
        /etc/apt/sources.list.d/*.sources 2>/dev/null || true; \
    fi

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        curl \
        openssl \
 && rm -rf /var/lib/apt/lists/*

# 2. 配置 NPM 镜像源并安装 Claude Code
RUN npm config set registry "${NPM_REGISTRY}"

RUN npm install -g \
        @anthropic-ai/claude-code@latest \
 && npm cache clean --force

# 安装 code-server（浏览器里的 VS Code），启动后用户在终端里手动跑 `claude`
# 走 github release deb 包（npm 包装法会拉 git+ssh://github.com 依赖，国内构建环境拉不到）
# github.com 在构建环境里不通，本地预下载到 ./vendor/ 后 COPY 进来，省掉单次构建 9 分钟等镜像下载
#   - 文件缺失时改用下方 curl + ghproxy.net 兜底（ARG CODE_SERVER_VERSION 需保持一致）
#   - vendor/ 已加入 .gitignore，不入库
ARG CODE_SERVER_VERSION=4.129.0
COPY vendor/code-server_${CODE_SERVER_VERSION}_amd64.deb /tmp/code-server.deb
RUN dpkg -i /tmp/code-server.deb \
 && rm /tmp/code-server.deb \
 && code-server --version

# VS Code 扩展策略：从本地 .vsix 装，不走运行时市场
#   - vendor/*.vsix 由用户在网络通的电脑上预下载（见 ARCHITECTURE.md / 注释）
#   - 构建时只 COPY，运行时由入口脚本装，避免 Open VSX / MS Marketplace 在容器内不通
# 必须 --chown=node:node：容器以 node 用户跑，entrypoint.sh 装完后要 rm -rf 这些文件，
# root 拥有的文件 node 删不掉会让 entrypoint 在 set -e 下退出、容器立即 Exited(1)
COPY --chown=node:node vendor/*.vsix /tmp/extensions/

# 默认 UI 语言：简体中文（通过 CMD 的 --locale=zh-cn 启用，见下面）
# 之前在 User/settings.json 里写 locale 字段无效 —— code-server 4.x 只认
# --locale 命令行参数（VSCODE_LOCALE env 也不生效，见 issue #4333）
#
# HTTPS 证书：build 时先放一份默认的（CN=localhost SAN=127.0.0.1）在
# /etc/code-server/cert.pem。运行时 portal 会按浏览器访问的 Host 动态生成
# 一份带正确 SAN 的证书通过 bind mount 覆盖这两个文件，并通过 CERT_FILE /
# KEY_FILE env var 让 entrypoint.sh 传给 code-server --cert / --cert-key。
# 覆盖路径而不是改 /etc/code-server 目录属性，避免碰 root 拥有的目录。

# 3. 准备工作区
RUN mkdir -p /workspace \
 && chown -R node:node /workspace

# 自签 HTTPS 证书：VS Code webview 依赖 crypto.subtle，HTTP + 非 localhost 是
# non-secure context → webview 不能渲染。自签证书让浏览器认为是 secure context，
# 首次访问会弹"不安全"警告（内网自用可接受）。
# 必须在 USER root 阶段做（/etc/code-server 需要 root 写权限）
RUN mkdir -p /etc/code-server \
 && openssl req -x509 -nodes -newkey rsa:2048 \
    -keyout /etc/code-server/key.pem \
    -out /etc/code-server/cert.pem \
    -days 3650 \
    -subj "/CN=localhost" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:0.0.0.0" \
 && chmod 644 /etc/code-server/key.pem /etc/code-server/cert.pem

WORKDIR /workspace

# 入口脚本：先装预置的 .vsix 扩展，再 exec code-server 让它接管 PID 1
COPY --chmod=755 entrypoint.sh /usr/local/bin/entrypoint.sh

# 入口：code-server 启动后自动打开 multi-root workspace 文件（/workspace 共享代码库 +
# /home/node/scratch 临时区两个根；详见 entrypoint.sh 生成 .code-workspace 段）
# --locale=zh-cn 关键：code-server 4.x 不读 User/settings.json 的 locale 字段，
# 必须命令行参数；VSCODE_LOCALE env 也不生效（issue #4333）。
# --cert/--cert-key 由 entrypoint.sh 拼接（路径从 CERT_FILE / KEY_FILE env 拿，
# portal 通过 bind mount 注入运行时生成的证书，覆盖默认的 localhost cert）
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["--bind-addr", "0.0.0.0:8080", "--auth", "password", "--locale", "zh-cn", "/home/node/workspace.code-workspace"]

