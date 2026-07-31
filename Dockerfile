# 注：不写 # syntax=docker/dockerfile:1.7 —— BuildKit 走 frontend image 模式会
# 每次 build 都拉 docker.io/docker/dockerfile:1.7（国内 40+ 秒/次）。省掉这行让
# Docker daemon 用内建 frontend，省一次大镜像拉取。功能上 1.7 特性已用不到。

FROM node:22-slim

# 默认走清华源；海外/直连环境构建时传 `--build-arg APT_MIRROR=deb.debian.org` 切回官方
# 注意：用 http:// 而不是 https:// —— node:22-slim 默认没装 ca-certificates，
# 第一次 apt update 时 HTTPS 握手证书验证失败。清华源 HTTP 也通且一样快（内网源不分 HTTP/HTTPS 性能）
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
# npm 默认已经是 npmmirror；想用官方源传 --build-arg NPM_REGISTRY=https://registry.npmjs.org/
ARG NPM_REGISTRY=https://registry.npmmirror.com/

# 1. 配置 APT 镜像源
# debian bookworm 的 sources 格式可能是：
#   - 旧格式 /etc/apt/sources.list: `deb http://deb.debian.org/debian bookworm main`
#   - 新格式 /etc/apt/sources.list.d/debian.sources: `URIs: https://deb.debian.org/debian`
# 用 sed -E 只替换主机名（保留 scheme + path），两种格式都能命中
RUN if [ -n "${APT_MIRROR}" ]; then \
      sed -i -E "s|https?://deb\.debian\.org|http://${APT_MIRROR}|g; \
                 s|https?://security\.debian\.org|http://${APT_MIRROR}|g" \
        /etc/apt/sources.list \
        /etc/apt/sources.list.d/*.list \
        /etc/apt/sources.list.d/*.sources 2>/dev/null || true; \
      echo "[apt-mirror] APT_MIRROR=${APT_MIRROR}"; \
      cat /etc/apt/sources.list.d/debian.sources 2>/dev/null | head -3; \
    fi

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        curl \
        openssl \
        # === 压缩解压（node:22-slim 基础镜像没带）===
        unzip zip xz-utils bzip2 p7zip-full \
        # === 文档/PDF 工具链（anthropics/skills 的 docx/pptx/xlsx/pdf 共用）===
        poppler-utils qpdf pandoc \
        # === OCR（pdf skill 做扫描件识别）===
        tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng \
        # === 图像处理 ===
        imagemagick \
        # === LibreOffice：docx/pptx/xlsx 互转 + xlsx 公式重算必备 ===
        libreoffice \
        # === Python 运行时：anthropics/skills 几乎所有脚本都是 Python ===
        python3 python3-pip \
        # === 文本 / JSON 处理 ===
        jq \
        # === 字体：让 soffice 渲染的 PDF 不出现 □□ 方块（Noto CJK = 中文）===
        fonts-noto-cjk fonts-liberation fonts-dejavu \
 && rm -rf /var/lib/apt/lists/*

# 2. Python pip: anthropics/skills 运行时依赖
# 用 --break-system-packages：Debian bookworm 的 python3 是 EXTERNALLY-MANAGED（PEP 668）
# 用 --no-cache-dir：避免 .cache 占镜像层
# 国内构建环境拉 pypi.org 经常超时 → 默认走清华源 + 加重试/超时。
# 海外/直连环境想退回官方源，把 --index-url 这一行删掉即可。
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir --break-system-packages \
        --index-url "${PIP_INDEX_URL}" \
        --retries 10 --timeout 100 \
        # pdf skill
        pypdf pdfplumber pdf2image pytesseract reportlab pypdfium2 \
        # pptx/docx skill（validate.py / helpers 都靠 defusedxml + lxml）
        "markitdown[pptx]" python-pptx defusedxml lxml Pillow \
        # xlsx skill（openpyxl 读写 xlsx，pandas 给 markitdown 兜底）
        openpyxl pandas

# 3. 配置 NPM 镜像源 + 全局安装 Claude Code + skills 用的 Node 包
RUN npm config set registry "${NPM_REGISTRY}"

# 让 `node script.js`（Claude Code 跑 skill 脚本时）能找到全局 npm 包
# 路径必须是 `npm root -g` 的真实输出（Debian 上是 /usr/local/lib/node_modules，
# 不是 /usr/lib/node_modules；某些 sandbox 会清 env，显式设一次最稳）
ENV NODE_PATH=/usr/local/lib/node_modules

RUN npm install -g \
        @anthropic-ai/claude-code@latest \
        # docx skill：用 docx-js 创建新文档
        docx \
        # pptx skill：pptxgenjs 生成新 deck；react/react-dom/react-icons 给 react-icons 图标走 ReactDOMServer.renderToStaticMarkup；sharp 给 SVG 栅格化
        pptxgenjs react react-dom react-icons sharp \
 && npm cache clean --force

# 安装 code-server（浏览器里的 VS Code），启动后用户在终端里手动跑 `claude`
# 走 github release deb 包（npm 包装法会拉 git+ssh://github.com 依赖，国内构建环境拉不到）
# github.com 在构建环境里不通，本地预下载到 ./vendor/ 后 COPY 进来，省掉单次构建 9 分钟等镜像下载
#   - 文件缺失时改用下方 curl + ghproxy.net 兜底（ARG CODE_SERVER_VERSION 需保持一致）
#   - vendor/ 已加入 .gitignore，不入库
ARG CODE_SERVER_VERSION=4.131.0
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

# 5. 准备工作区
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

