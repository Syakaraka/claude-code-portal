# syntax=docker/dockerfile:1.7

FROM node:22-slim

ARG APT_MIRROR=
ARG NPM_REGISTRY=https://registry.npmmirror.com/

# 1. 配置 APT 镜像源 (安装 git, ca-certificates 和 libaio1)
# libaio1: Oracle Instant Client 必须的系统异步 IO 库
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
        libaio1 \
 && rm -rf /var/lib/apt/lists/*

# 2. 配置 NPM 镜像源并安装 Claude Code 和 tsx
RUN npm config set registry "${NPM_REGISTRY}"

RUN npm install -g \
        @anthropic-ai/claude-code@latest \
        mcp-remote@latest \
        tsx \
 && npm cache clean --force

# 3. 准备工作区和 MCP 代码目录
RUN mkdir -p /workspace /mcp_servers \
 && chown -R node:node /workspace /mcp_servers

# 切换到 node 用户安装 MCP 依赖，避免权限问题
USER node
WORKDIR /mcp_servers

# 4. 初始化 MCP 项目的 package.json 并安装依赖
RUN npm init -y \
 && npm install \
        @modelcontextprotocol/sdk \
        zod \
        mysql2 \
        oracledb \
 && npm cache clean --force

# 5. 将 TypeScript 脚本复制到容器中
COPY --chown=node:node unified_db_mcp.ts /mcp_servers/unified_db_mcp.ts

WORKDIR /workspace

ENTRYPOINT ["claude"]
CMD []

