# Claude Code 多用户部署 - 架构设计

> 状态：**Phase 1 ✅ 完成**，Phase 2 完成，Phase 3 部分完成
> 最近更新：2026-08-01（admin 重建面板 checkbox 5s 修复；approved 注入 + .env 默认值 + 删密码规则说明；关 code-server telemetry + update check；approved 位数 16→20 对齐 Claude Code vZ(-20)）
> 上次：2026-07-31（code-server 4.129.0 → 4.131.0 / claude-code 2.1.217 → 2.1.220 / zh-hans 1.129.0 → 1.131.0；§11）+ 一键禁用 VS Code / Copilot 内置 AI（§23）
> 后续：2026-07-19 多 worker session 修复（FLASK_SECRET 跨 worker 一致）；2026-07-19 中文语言包 v1.129.0 替换

---

## 1. 背景与目标

### 现状
- 单个 `claude-code:local` 容器部署在服务器
- 用户 SSH 进服务器启动容器使用
- 文件交互困难：本地文件无法被容器访问，临时文件传输繁琐
- 单用户场景设计

### 目标
- 支持 4-8 人并发使用
- 每人使用自己的 `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` + **3 个模型名**（`opusModel` / `sonnetModel` / `haikuModel`，团队用了第三方 LLM 网关，可分别路由）
- 浏览器访问，零 SSH 依赖
- `<项目>/volumes/code/`（团队知识库）**只读共享**，不修改
- 每人独立工作区（`volumes/users/<uid>/`）存放问答临时文件
- 自助式：用户不需要 admin 介入即可访问
- 凭据存在浏览器，服务器不持久化完整 API Key

### 非目标
- 公共互联网暴露（HTTPS、域名等）
- 复杂的用户管理（角色、权限、配额）
- 自动清理长期未用容器（作为后续优化项）

---

## 2. 架构总览

```
[浏览器]                                       (localStorage 存凭据)
   │
   │ baseUrl / apiKey / opusModel / sonnetModel / haikuModel
   ▼
http://server/  ─────►  [Claude Portal :80]
                            │
                            │ POST /api/start
                            │ GET  /install-cert   (首次下载内部 CA)
                            ▼
                   ┌────────────────────┐
                   │  Flask App         │
                   │                    │
                   │  hash(apiKey) ──► user_id
                   │       │            │ (不持久化完整 apiKey)
                   │       ▼            │
                   │  查/起 claude-<uid>│
                   └─────────┬──────────┘
                             │
              ┌──────────────┴───────────────────────────┐
              ▼                              ▼            ▼
       Docker socket              <项目>/volumes/users/<uid>/  volumes/certs/
       (宿主机 docker)                    ▲                          ▲
              │                          │                          │ (内部 CA)
              │ docker run               │  bind mount              │
              │ -e ANTHROPIC_*           │  (scratch 独立 bind)     │ portal 启动时签
              │ -e CERT_FILE / KEY_FILE   │                          │ 用户 cert
              │ -v users/<uid>:/home/node │                          │ (含正确 SAN)
              │ -v users/<uid>/scratch:/home/node/scratch
              │ -v users/<uid>/code-server-certs:/home/node/.local/share/code-server
              │ -v code:/workspace:ro
              ▼                          │                          │
      ┌────────────────┐                 │                          │
      │ claude-<uid>   │ ────────────────┘                          │
      │ :8081 / :8082  │                                            │
      │                │ ◄──────────────────────────────────────────┘
      │ code-server 4.131.0 (HTTPS)
      │                │   默认打开 multi-root workspace:
      │ /workspace (RO)│     • /workspace      (RO 共享代码库)
      │ /home/node     │     • /home/node/scratch (per-user RW 临时区)
      │   ├── scratch  │
      │   └── .local/  │
      │       share/code-server/  ← portal 注入 cert + argv.json + (zh-cn)languagepacks.json
      └────────────────┘
```

**核心组件**：
- **Claude Portal**：单例 Flask 应用，负责登录、用户识别、容器编排、CA 签发
- **claude-\<uid\> 容器**：按需启动的 per-user 容器，跑 code-server + claude CLI（含 Claude Code VS Code 扩展 + 简体中文语言包）
- **宿主挂载点**（项目本地 `volumes/`）：
  - `volumes/code/` → 容器 `/workspace` (RO，团队知识库)
  - `volumes/users/<uid>/` → 容器 `/home/node` (RW，首次创建时从 `volumes/node/` 模板复制)
  - `volumes/users/<uid>/scratch` → 容器 `/home/node/scratch` (RW，per-user 临时区，独立 bind)
  - `volumes/users/<uid>/code-server-certs` → 容器 `$HOME/.local/share/code-server` (portal 注入用户 cert + 配置文件)
  - `volumes/certs/{ca.crt,ca.key,ca.srl}` → Portal 内部 CA，签发用户 cert 时用
- **`vendor/`**：构建环境里预下载的二进制（code-server deb + vsix 扩展）

---

## 3. 设计决策

### 决策 1：单入口 Portal + 动态容器
浏览器先访问 Portal，Portal 动态拉起/复用 claude 容器。

**为什么不直接每人一容器（端口分配）？**
- 4-8 个常驻容器占内存，每人都吃 1-2GB
- 添加/删除用户要改 compose
- 用户改 API Key 要重启容器

**为什么不用 Coder OSS？**
- 太重、学习成本高
- 当前需求简单，不需要完整平台

### 决策 2：Docker socket 挂载（不是 DinD）
Portal 容器挂载宿主机 `/var/run/docker.sock`，获得完整 Docker 控制权。

**为什么不 DinD？**
- 配置复杂、网络卷都要重配
- socket 方案已够用

**安全代价**：Portal 容器有 root 等价权限  
**缓解**：
- 用户 ID 严格正则校验（防路径穿越）
- API Key 不写日志
- 仅内网访问
- 代码量小、可审计

### 决策 3：用户 ID = `sha256(apiKey)[:16]`
- 不持久化完整 API Key
- 用户改 API Key → 新 user_id → 起新容器（旧容器可手动停）
- 改 baseUrl/model（同一 apiKey）→ 需重启容器

### 决策 4：每人一个容器，端口范围可配 + 每用户固定（§19）
- 简单，无须 Nginx 反代
- 文件天然隔离（每个容器独立 home 目录）
- 端口范围 `CLAUDE_PORT_MIN`–`CLAUDE_PORT_MAX`（默认 9901–9999，§15）；不够时调 `MAX_ACTIVE_CONTAINERS` 或扩范围
- 端口**首次分配后固定**：`volumes/users/<uid>/.port` 存用户的 host port；rebuild / 重启 portal 都复用同一个端口（§19）。分配策略：先读 `.port` → 校验范围内+未被占用 → 用之；否则 `next_free_port()` 降级
- Portal 给出 URL 后用户书签就行，不用记端口
- admin 表格：实际端口后跟 ✓（pinned 一致）/ ⚠（pinned 被占已重摇）/ 无标记（升级前老用户无 .port）

### 决策 5：浏览器 localStorage 存凭据
- 用户无需每次输入
- 自助式
- 换浏览器/清缓存才需重新填

### 决策 6：每人独立 `<项目>/volumes/users/<user_id>/` 目录
- 隔离、持久化都自然解决
- 不需要 git 概念
- bind mount 而非 docker volume（调试直观）
- 首次创建时从 `volumes/node/` 模板复制（决策 10）

### 决策 7：`volumes/code/` 全员只读
- 避免用户不小心改源
- 临时文件去各自的 home

### 决策 8：code-server password = user_id（确定性，非随机）
最初用 `secrets.token_urlsafe(16)` 随机生成，但用户反映"找不到密码"。改为 `password = user_id`：
- 用户能从 apiKey 自己算出来（hash 前 16 位）
- 浏览器 localStorage 记录一份方便查看
- 同一用户多次启动密码一致，记忆成本为零

### 决策 9：扩展通过 vendor .vsix 本地装（不用运行时市场）
详见 §11。code-server v4.131.0 不再支持 config.yaml 切市场，且国内构建环境访问 github.com / MS Marketplace 都不通，本地预下载 .vsix 离线装是唯一稳的路径。

### 决策 10：项目本地 volumes/ 目录（替代系统路径 /home/users、/workspace）
所有 claude 容器的 bind mount 源都放在项目内 `volumes/`，不再散在系统目录：
- 便于迁移（整个项目是个独立单元，scp 走就行）
- 便于备份（一个 tar 解决）
- 权限清晰（dev 用户能直接 ls/编辑 volumes/ 内容）

对应关系：
- `volumes/code/`     → 容器 `/workspace` (RO，团队知识库)
- `volumes/node/`     → 用户 home 模板（首次创建用户时复制）
- `volumes/users/<uid>/` → 容器 `/home/node` (RW，每用户独立)

详见 §4、§12。

### 决策 11：Multi-root workspace（/workspace RO + scratch per-user）
`/workspace` 是 RO 共享代码库，Claude Code 跑起来可能会产生临时文件（Plan 文件、scratch 脚本、调试输出）。原始方案 D：**VS Code 多根工作区**。

**为什么不在 /workspace 下挂个 RW 子目录？**
- Docker bind mount 不能嵌套在已挂载的路径下（`/workspace` 已经是 mount，`/workspace/scratch` 作为独立 source 会出错或覆盖）
- 子目录权限精细控制太脆弱（要确保 demo/ 仍 RO，scratch/ 是 RW，每次新增 RO 子目录都要改 mount 配置）

**为什么不用 VS Code 自带"文件 → 将文件夹添加到工作区"？**
- 需要用户每次手动点，Claude Code 默认 workspace 还是只有 /workspace，工具调用以 /workspace 为 context 没问题
- 但 scratch 文件散落在侧栏不如打开一个根直观
- 多根工作区可以一次配置、终身使用

**方案**：`entrypoint.sh` 在首次启动时生成 `$HOME/workspace.code-workspace`，包含两个根：
```json
{
  "folders": [
    { "path": "/workspace",    "name": "Workspace" },
    { "path": "$HOME/scratch", "name": "Scratch" }
  ],
  "settings": { "security.workspace.trust.enabled": false }
}
```
- /workspace 放第一位 → VS Code integrated terminal CWD = /workspace → Claude Code 默认 CWD 保持 /workspace
- $HOME/scratch 是 per-user bind mount（portal 在 start_container 时 mkdir + chown 1000:1000 + chmod 0777，独立 bind `users/<uid>/scratch` → `/home/node/scratch`）
- 用户也可以在 VS Code 里手动加第三个根

**关键约束**：sticky bit (1777) 不能在容器内设——portal 创建的目录 owner=root，node 用户无权 chmod → 容器 Exited(1)。per-user 容器里也没必要 sticky（一个容器只有一个 uid=1000 用户访问）。详见 §决策 13。

---

## 4. 目录结构

```
claude_web_images/
├── Dockerfile                  # claude-code 镜像构建（含 vendor 文件 COPY + 入口脚本）
├── entrypoint.sh               # 容器启动入口：装 vsix → exec code-server
├── docker-compose.yml          # 只跑 portal；不再直接跑 claude
├── .env                        # （保留，可选）
├── .gitignore                  # 排除 vendor/（不入库）
├── .dockerignore               # 排除 volumes/ portal/ ARCHITECTURE.md
├── ARCHITECTURE.md             # ← 本文档
├── vendor/                     # ⚠️ 不入库，构建环境预下载的二进制
│   ├── code-server_4.131.0_amd64.deb
│   ├── Anthropic.claude-code-2.1.220@linux-x64.vsix
│   └── MS-CEINTL.vscode-language-pack-zh-hans-1.131.0.vsix
├── volumes/                    # ⚠️ 不入库（含用户数据 + 知识库）
│   ├── code/                   # 团队知识库（→ 容器 /workspace, RO）
│   │   └── demo/
│   ├── node/                   # 用户 home 模板（首次创建用户时复制）
│   │   ├── .claude/
│   │   └── .claude.json
│   └── users/                  # 每用户独立 home（→ 容器 /home/node, RW）
│       ├── .portal_users.json  # portal 缓存（user_id → container_id 等）
│       └── <uid>/
└── portal/                     # 新增
    ├── Dockerfile
    ├── requirements.txt
    ├── app.py
    ├── templates/
    │   └── login.html
    └── static/
        └── app.js
```

**宿主机路径**（都在项目本地 `volumes/`，portal 通过 `./volumes:/volumes` 整体挂载访问）：
- `volumes/code/` - 只读知识库 → portal 容器内 `/volumes/code`
- `volumes/node/` - 用户 home 模板 → portal 容器内 `/volumes/node`
- `volumes/users/<uid>/` - 每用户独立 home → portal 容器内 `/volumes/users/<uid>`
- `/var/run/docker.sock` - Docker 控制通道

---

## 5. 关键路径与配置

### 5.1 宿主机需预创建
```bash
mkdir -p volumes/code volumes/node volumes/users
# volumes/node/ 放用户 home 初始内容（至少 .claude/、.claude.json 等）
# volumes/code/ 放团队知识库（README、文档等）
```

### 5.2 Portal 容器常量（全部从 `.env` 读，默认值在 `app.py`）

**两种视角并存**——这是踩过的坑，必须区分清楚：

| 视角 | 用途 | 路径样例 |
|------|------|----------|
| **Portal 容器内视角**（通过 `${HOST_VOLUMES_DIR}:${PORTAL_VOLUMES_MOUNT}` bind mount 看到） | portal 自己用 `shutil` / `os` 读写 volumes 内容（复制模板、判断目录、写用户缓存） | `/volumes/users/<uid>`、`/volumes/node` |
| **Host 视角**（docker daemon 解析时按 host 真实绝对路径找） | 传给 `docker.run()` 作为 bind source；portal 容器内 `/volumes/...` 不能直接给 docker.run()，daemon 会按 host 上不存在的位置处理 | `${HOST_USERS_DIR}/<uid>` |

```python
# 所有常量都从环境变量读，默认值兼容旧部署
HOST_PROJECT_DIR      = _env("HOST_PROJECT_DIR",      "/home/thomas/workspace/code/claude_web_images")
HOST_VOLUMES_DIR      = _env("HOST_VOLUMES_DIR",      "${HOST_PROJECT_DIR}/volumes")
HOST_USERS_DIR        = _env("HOST_USERS_DIR",        "${HOST_VOLUMES_DIR}/users")
HOST_CODE_DIR         = _env("HOST_CODE_DIR",         "${HOST_VOLUMES_DIR}/code")
HOST_TEMPLATE_DIR     = _env("HOST_TEMPLATE_DIR",     "${HOST_VOLUMES_DIR}/node")
HOST_CERTS_DIR        = _env("HOST_CERTS_DIR",        "${HOST_VOLUMES_DIR}/certs")
PORTAL_VOLUMES_MOUNT  = _env("PORTAL_VOLUMES_MOUNT",  "/volumes")
CONTAINER_VOLUMES     = PORTAL_VOLUMES_MOUNT         # 容器内视角
USER_DATA_BASE        = os.path.join(CONTAINER_VOLUMES, "users")
WORKSPACE_PATH        = os.path.join(CONTAINER_VOLUMES, "code")
USER_TEMPLATE         = os.path.join(CONTAINER_VOLUMES, "node")
HOST_USER_DATA_BASE   = HOST_USERS_DIR               # host 视角
HOST_WORKSPACE_PATH   = HOST_CODE_DIR
CLAUDE_IMAGE_NAME     = _env("CLAUDE_IMAGE_NAME",     "claude-code:local")
PORTAL_LABEL          = _env("PORTAL_LABEL",          "managed-by")
CLAUDE_PORT_MIN       = _env_int("CLAUDE_PORT_MIN",   9901)
CLAUDE_PORT_MAX       = _env_int("CLAUDE_PORT_MAX",   9999)
```

> ⚠️ **为什么要双视角而不是只用一个？** Portal 容器没有 host 根目录可访问，唯一能访问 host 上 `volumes/` 内容的途径就是 `${HOST_VOLUMES_DIR}:${PORTAL_VOLUMES_MOUNT}` 这个 bind mount（得到容器内 `${PORTAL_VOLUMES_MOUNT}/...`）。但 docker.run() 的 bind source 必须用 host 视角的绝对路径，否则 docker daemon 按 host 上不存在的路径处理，会静默失败或 bind 到自动创建的空目录。

> 完整 env var 清单见 `.env.example` 和 §15。

### 5.3 Claude 容器环境变量
由 Portal 注入：

| 变量 | 来源 | 说明 |
|------|------|------|
| `ANTHROPIC_BASE_URL` | 用户填 | LLM 网关地址 |
| `ANTHROPIC_API_KEY` | 用户填 | 敏感，不写日志 |
| `ANTHROPIC_MODEL` | Portal 注入 | = `opusModel`（Claude Code 默认 model 用这个）|
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | 用户填（`opusModel`） | 网关路由 opus 请求用 |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | 用户填（`sonnetModel`） | 网关路由 sonnet 请求用 |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | 用户填（`haikuModel`） | 网关路由 haiku 请求用 |
| `PASSWORD` | Portal 注入 | code-server 登录密码（= user_id，确定性）|
| `CERT_FILE` / `KEY_FILE` | Portal 注入 | 用户专属 HTTPS 证书路径（portal 用内部 CA 签发）|

> 浏览器侧只让用户填 **3 个模型名**（`opusModel` / `sonnetModel` / `haikuModel`）；portal 内部把 `opusModel` 同时复制到 `ANTHROPIC_MODEL` 和 `ANTHROPIC_DEFAULT_OPUS_MODEL` 两个 env 里（兼容不同版本的 Claude Code）。

> `HOME` **不显式注入**——容器以 `user="node:node"` 启动，`$HOME` 默认就是 `/home/node`，正好对齐 bind mount 到 host `volumes/users/<uid>/` 的目标。之前用 `user="0:0"` 时 `$HOME=/root` 是错位的，需要显式注入；改成 node 后就不必了，也更符合最小权限原则。

补充：容器以 `user="node:node"`（uid/gid=1000）跑，host `volumes/users/<uid>/` 在 portal 端 `chown 1000:1000`（host 上 `thomas` 也是 uid/gid 1000，自动对齐） + chmod 0o777，node 用户可读写。Claude Code 扩展内的 native binary（`claude` CLI + `audio-capture.node`）由 `entrypoint.sh` 装完 vsix 后显式 `chmod +x`。

### 5.4 端口分配
- Portal：`PORTAL_HOST_PORT`（默认 9900，对外）/ `PORTAL_CONTAINER_PORT`（容器内）
- Claude 容器：`CLAUDE_PORT_MIN`–`CLAUDE_PORT_MAX`（默认 9901–9999），**每用户固定**（§19）：
  - 首次启动 `next_free_port()` 从 MIN 扫到 MAX 找第一个空闲的，写到 `volumes/users/<uid>/.port`
  - 后续 rebuild / portal 重启读 `.port` 复用；不合法（损坏 / 范围外 / 被其他用户占用）自动降级重摇并覆写
  - `wipe_home` 会连带删 `.port`（语义：用户目录重置 → 端口也重置；想保留手动 cp 出去）

---

## 6. 流程

### 6.1 首次访问（新用户）
```
[1] 用户浏览器访问 http://server/
[2] Portal 返回登录表单（含 CA 安装 banner，首次使用必装）
[3] 用户下载并安装内部 CA 证书 → 勾选"我已安装 CA 证书" → localStorage 标记
[4] 用户输入 baseUrl / apiKey / opusModel / sonnetModel / haikuModel
[5] 浏览器 JS：
    - localStorage.setItem(STORAGE_KEY, ...)
    - POST /api/start
[6] Portal Flask：
    - user_id = sha256(apiKey)[:16]
    - ensure_user_dir：从 volumes/node/ 复制模板 + chown 1000:1000
    - ensure_user_cert：用内部 CA 签一份带正确 Host SAN 的证书写到 users/<uid>/code-server-certs/
    - 创建 users/<uid>/scratch/ 临时区目录
    - 查 claude-<user_id> 容器 → 不存在
    - password = user_id（确定性）
    - docker run，端口 PORT_BASE + n
        - bind mounts: users/<uid> → /home/node, users/<uid>/scratch → /home/node/scratch,
          users/<uid>/code-server-certs → $HOME/.local/share/code-server,
          code → /workspace:ro
        - env: 4 个 ANTHROPIC_*_MODEL + CERT_FILE / KEY_FILE
    - 等容器健康（约 1-3 秒，entrypoint.sh 装 vsix 需再 +5-10 秒）
    - 返回 {port, password, user_id, reused: false}
[7] 浏览器显示成功面板：复制 URL + 复制 password + "打开 Code Server" 链接
[8] 用户在新标签页粘贴 password 登录 code-server
[9] UI 默认中文；活动栏有 Claude Code 图标；默认打开 multi-root workspace（/workspace + scratch）
```

### 6.2 后续访问（同 apiKey）
```
[1] 浏览器从 localStorage 读取 5 个字段 → 自动 POST /api/start
[2] Portal 找到运行中的 claude-<user_id>，复用（reused: true）
[3] 浏览器直接显示已有 URL，跳过启动步骤
```

### 6.3 用户改 baseUrl/模型名（apiKey 不变）
```
[1] 浏览器 localStorage 中的值变了
[2] POST /api/start（hash(apiKey) 不变）
[3] Portal 发现旧容器在跑但环境不同
   → TODO Phase 2：自动重启
   → 当前：返回现有，env 仍是旧值
```

### 6.4 用户改 API Key
```
[1] 新 hash ≠ 旧 hash → 系统视作新用户
[2] 启动新容器（旧容器仍在运行，admin 可手动 stop）
```

### 6.5 claude 容器首次启动（entrypoint.sh 流程）
```
[1] 容器被 docker run 拉起 → /usr/local/bin/entrypoint.sh
[2] 遍历 /tmp/extensions/*.vsix，code-server --install-extension <本地路径>
   - Claude Code 扩展（84MB）~ 3 秒
   - 中文语言包（600KB）~ 1 秒
[3] rm /tmp/extensions（省内存）
[4] 写 User/settings.json + User/argv.json + <userDataDir>/languagepacks.json
   （zh-cn 兜底，让 vscode 内核启动时 NLS 直接命中中文包；详见 §11.4）
[5] exec code-server "$@"（让 code-server 接管 PID 1）
[6] code-server 监听 0.0.0.0:8080，应用 locale=zh-cn 加载已装中文包
```

---

## 7. API 协议

### POST /api/start

**请求**：
```json
{
  "baseUrl":     "https://gateway.company.com",
  "apiKey":      "sk-ant-xxx",
  "opusModel":   "claude-opus-4",
  "sonnetModel": "claude-sonnet-4",
  "haikuModel":  "claude-haiku-4"
}
```

**成功响应**：
```json
{
  "port": 8083,
  "password": "a1b2c3d4e5f6g7h8",
  "user_id": "a1b2c3d4e5f6g7h8",
  "reused": false
}
```
（`password === user_id`）

**失败响应**：
```json
{ "error": "缺少必要字段 baseUrl/apiKey/opusModel/sonnetModel/haikuModel" }   // 400
{ "error": "启动容器失败: ..." }                                            // 500，含容器日志
```

portal 内部把 `opusModel` 复制到 `ANTHROPIC_MODEL`（兼容老版 Claude Code） + `ANTHROPIC_DEFAULT_OPUS_MODEL`，并分别透传 `sonnetModel` / `haikuModel` 到 `ANTHROPIC_DEFAULT_SONNET_MODEL` / `ANTHROPIC_DEFAULT_HAIKU_MODEL`。详见 §5.3。

### GET /api/status/\<user_id\>
返回用户容器状态（仅查询，不返回敏感信息）。

### GET /api/users
列出所有由 portal 启动的容器（管理用）。返回字段：`user_id, container_name, status, port, created`。

---

## 8. 安全考虑

| 风险 | 缓解 |
|------|------|
| **路径穿越**（user_id 拼到 volume 路径） | 严格正则：`re.fullmatch(r'[a-f0-9]{16}', user_id)` |
| **API Key 泄露到日志** | 日志中间件过滤（`SecretFilter` regex `sk-ant-...` → `[REDACTED]`） |
| **API Key 出现在 URL** | 一律 POST，不 GET |
| **Docker socket = root 等价** | 代码可审计 + 输入严格校验 + 仅内网 |
| **弱 code-server 密码** | password = user_id（16 hex），足够强度 |
| **明文 HTTP** | 内网场景明确；公网需另行加固（HTTPS） |
| **vendor/*.deb / *.vsix 带恶意代码** | 仅维护者手动下载；sha256 可在 Dockerfile 加校验 |

---

## 9. 实施阶段

### Phase 1：Portal 骨架 ✅ 完成
- [x] portal/Dockerfile + requirements.txt + app.py
- [x] portal/templates/login.html（登录表单 + 成功面板）
- [x] portal/static/app.js（localStorage 凭据持久化 + 密码复制）
- [x] docker-compose.yml 改造为只跑 portal
- [x] 浏览器可填表 + 重定向到容器
- [x] hash(apiKey) 用户识别 + 端口递增 + 容器复用
- [x] 确定性密码（= user_id）+ 自动复制 + 成功面板 UX

### Phase 2：体验完善 ✅ 大部分完成
- [x] localStorage 自动凭据填充（已保存则跳过表单）
- [x] 浏览器记住 code-server password（`PASSWORD_MAP`）
- [x] 容器启动失败时把日志带回来便于排查
- [x] **Claude Code 镜像优化**（见 §11）：
  - [x] 预装 Claude Code VS Code 扩展（2.1.220 linux-x64）
  - [x] 预装简体中文语言包（1.131.0，兼容 code-server 自带 VS Code 1.131.0）
  - [x] UI 默认中文（`locale: zh-cn`，含 argv.json + userDataDir/languagepacks.json 兜底）
  - [x] code-server 默认打开 multi-root `/home/node/workspace.code-workspace`（/workspace + scratch）
- [x] **4 个 ANTHROPIC 模型 env vars**（§5.3）
- [x] **Multi-root workspace**（决策 11，per-user scratch 独立 bind）
- [x] **内部 CA + 用户 HTTPS 证书**（§13，/install-cert 引导）
- [x] **权限映射 chown 1000:1000**（§14）
- [x] **CA 安装 banner 在 login.html**（首次使用必装，localStorage 标记后隐藏）
- [ ] 启动状态轮询（容器尚未健康时显示"正在启动..."）
- [ ] 改 baseUrl/模型名 自动重启容器
- [ ] 错误提示友好化（端口冲突、镜像不存在等）

### Phase 3：运维
- [ ] 长期未用容器自动清理（cron + 心跳检查）
- [x] 用户列表 Web UI（GET /api/users，已实现）
- [ ] 单用户删除接口（DELETE /api/users/\<user_id\>）
- [ ] 资源监控（每容器 CPU/内存上限）

---

## 10. 已知限制与未来工作

| 限制 | 缓解/未来 |
|------|----------|
| 容器清理靠手动 | Phase 3 自动清理 |
| HTTPS 用内部 CA，浏览器首次必须装 CA | §13 流程，login.html 引导 + 强制完全退出浏览器再开 |
| 改 API Key 不回收旧容器 | 待实现，可加 DELETE /api/users |
| phase 1 启动容器阻塞请求（同步） | 后续可改异步 + WebSocket 推送状态 |
| Portal 单点 | 后续可两实例 + keepalived（内网场景不必要） |
| vendor 文件需手动维护 | 写脚本定期检查版本更新；CI 可加自动下载 |
| code-server 扩展市场无法切换 | v4.131.0 已移除 config.yaml 支持，等上游修复或升级 |
| UI 中文靠 entrypoint 手动写 `<userDataDir>/languagepacks.json` 兜底（路径 + key 都是 vp()/KW() 实际读取的格式） | code-server 4.x exthost 加载 bug 仍可能让 languagePacks service 不写 file；目前 manual override 顶住，等上游修复后可去掉这个兜底 |

---

## 11. claude-code 镜像构建（vendor 模式）

### 11.1 为什么需要 vendor

构建环境实测：
- ❌ `github.com` 直连：TCP timeout（被墙）
- ❌ `ghproxy.com` / `mirror.ghproxy.com`：同上
- ⚠️ `ghproxy.net`：可达但 ~370 KB/s，下 196MB 需 9 分钟
- ❌ `open-vsx.org`（code-server 默认扩展市场）：hang
- ❌ `marketplace.visualstudio.com` API：SSL 握手失败
- ❌ code-server `config.yaml` 的 `extensions-gallery` 字段：v4.131.0 已移除，`Unknown option`
- ⚠️ `EXTENSIONS_GALLERY` 环境变量：源码里只 log，不实际传给 VS Code

结论：构建时联网装扩展、运行时联网装扩展都不可靠。**离线 vendor 是唯一稳的路径**。

### 11.2 vendor 文件清单

放在项目根 `vendor/` 目录（不入库）：

| 文件 | 来源 | 必需 |
|------|------|------|
| `code-server_4.131.0_amd64.deb` | github releases (`ghproxy.net` 镜像拉) | ✅ |
| `Anthropic.claude-code-2.1.220@linux-x64.vsix` | MS Marketplace（**必须 linux-x64**）| ✅ |
| `MS-CEINTL.vscode-language-pack-zh-hans-1.131.0.vsix` | MS Marketplace | ✅（要求 `engines.vscode: ^1.131.0`，因 code-server 4.131.0 自带 VS Code 1.131.0）|

### 11.3 vendor 文件下载指引

> ⚠️ **构建机下载不动的话，在另一台电脑下完拷贝过来即可**。详见 `vendor/README.md`（TODO：待补）。

**code-server deb**（在构建机用 `ghproxy.net`，约 9 分钟）：
```bash
curl -o vendor/code-server_4.131.0_amd64.deb \
  "https://ghproxy.net/https://github.com/coder/code-server/releases/download/v4.131.0/code-server_4.131.0_amd64.deb"
```

**Claude Code 扩展**（**必须带 `?targetPlatform=linux-x64`**，否则下到 arm64 或 win 版）：
```
https://marketplace.visualstudio.com/_apis/public/gallery/publishers/anthropic/vsextensions/claude-code/latest/vspackage?targetPlatform=linux-x64
```

**中文语言包**（**用 1.131.x 系列**，最新 1.131.0 要求 `^1.131.0`，与 code-server 自带 VS Code 1.131.0 严格匹配）：
```
https://openvsx.eclipsecontent.org/MS-CEINTL/vscode-language-pack-zh-hans/1.131.0/MS-CEINTL.vscode-language-pack-zh-hans-1.131.0.vsix
```

**验证语言包版本兼容**：
```bash
unzip -p vendor/MS-CEINTL.vscode-language-pack-zh-hans-1.131.0.vsix extension/package.json | python3 -m json.tool | grep engines
# 应该看到 "vscode": "^1.131.0"
```

### 11.4 构建时怎么用

Dockerfile 关键段：
```dockerfile
# 1. 装 code-server（从 vendor deb，dpkg -i）
ARG CODE_SERVER_VERSION=4.131.0
COPY vendor/code-server_${CODE_SERVER_VERSION}_amd64.deb /tmp/code-server.deb
RUN dpkg -i /tmp/code-server.deb && rm /tmp/code-server.deb && code-server --version

# 2. COPY 所有 vsix 到镜像（运行时装）
# 必须 --chown=node:node：entrypoint.sh 装完后要 rm -rf 这些文件，
# root 拥有会让 node 删不掉，set -e 下退出、容器 Exited(1)
COPY --chown=node:node vendor/*.vsix /tmp/extensions/

# 3. 入口脚本：装 vsix + 写配置 → exec code-server
COPY --chmod=755 entrypoint.sh /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# 入口：code-server 启动后自动打开 multi-root workspace 文件
# （/workspace 共享代码库 + /home/node/scratch 临时区两个根；详见 entrypoint.sh 生成 .code-workspace 段）
# --locale=zh-cn 关键：code-server 4.x 不读 User/settings.json 的 locale 字段，
# 必须命令行参数；VSCODE_LOCALE env 也不生效（issue #4333）。
# --cert/--cert-key 由 entrypoint.sh 拼接（路径从 CERT_FILE / KEY_FILE env 拿，
# portal 通过 bind mount 注入运行时生成的证书，覆盖默认的 localhost cert）
CMD ["--bind-addr", "0.0.0.0:8080", "--auth", "password", "--locale", "zh-cn", "/home/node/workspace.code-workspace"]
```

入口脚本 (`entrypoint.sh`) 流程（详见 entrypoint.sh 头部注释）：
1. 装 /tmp/extensions/*.vsix（Claude Code + 中文语言包）→ chmod +x native binary
2. 写 `User/settings.json`：禁用 workspace trust + 预信任 Claude Code / MS-CEINTL publisher
3. 写 `User/argv.json`：`{"locale": "zh-cn"}`（argvResource = appSettingsHome + "argv.json" = userDataDir + "User/argv.json"；vscode 内核按 file > cli > cookie > accept-language 决定 locale，cli flag 不透传）
4. 写 `<userDataDir>/languagepacks.json`（**根目录，不是 User/ 子目录**）：完整 schema `{hash, extensions:[{extensionIdentifier:{id:"publisher.name"}, version}], label, translations:{vscode:<abs path>}}` —— **必须包含 `extensions[]` 和 `label` 字段**，否则 `La.getInstalledLanguages` (server-main.js:680345) 在 `scanExtensions` 阶段抛 `TypeError: Cannot read properties of undefined (reading '0')`，整条扩展解析流程被中断，所有第三方扩展（包括 Claude Code）都被过滤掉。字段从 ms-ceintl 的 `package.json` 动态读（version / localizedLanguageName / publisher / name），避免硬编码跟语言包版本脱钩。zp() (server-main.js 的 generateNls) 读这个文件 + KW() 按 locale 字符串作 key 查条目 → 找到就加载中文包。entrypoint 手动写是为了兜底：code-server 4.x 在某些场景下不会把中文扩展加载到 exthost → languagePacks service 不写 file → UI 永远英文
5. 生成 multi-root `~/workspace.code-workspace`（/workspace + ~/scratch 两个根，详见 §决策 11）
6. 拼 `--cert/--cert-key`（从 `CERT_FILE` / `KEY_FILE` env 拿）
7. `exec code-server "$@"`（用 exec 让 code-server 接管 PID 1，entrypoint 退出）

### 11.5 验证

构建完后手动测一次：
```bash
docker run --rm -u 0 --entrypoint /usr/local/bin/entrypoint.sh claude-code:local \
    --bind-addr 0.0.0.0:8089 --auth password \
    /home/node/workspace.code-workspace 2>&1 | head -20
```

应该看到（典型输出，env 没 CERT_FILE 所以是 HTTP）：
```
[entrypoint] installing local VS Code extensions:
  - MS-CEINTL.vscode-language-pack-zh-hans-1.131.0.vsix
  - Anthropic.claude-code-2.1.220@linux-x64.vsix
[entrypoint] chmod +x: claude
[entrypoint] chmod +x: audio-capture.node
[entrypoint] wrote User/settings.json (workspace trust disabled, publishers pre-trusted)
[entrypoint] wrote User/argv.json (locale=zh-cn)
[entrypoint] wrote /home/node/.local/share/code-server/languagepacks.json (zh-cn manual override, ext=..., id=ms-ceintl.vscode-language-pack-zh-hans, ver=1.131.0)
[entrypoint] wrote workspace.code-workspace (multi-root: Workspace + Scratch)
[entrypoint] WARN: cert not found at $CERT_FILE / $KEY_FILE, starting without HTTPS
[entrypoint] starting code-server: code-server --bind-addr 0.0.0.0:8089 --auth password /home/node/workspace.code-workspace
HTTP server listening on http://0.0.0.0:8089/
```

---

## 12. volumes/ 目录语义（决策 10 详解）

### 12.1 三个子目录角色

```
volumes/
├── code/         RO 模板，bind → 容器 /workspace
│                 团队知识库，所有 claude 容器共用、内容一致、容器内只读
│
├── node/         RO 模板（不挂到任何容器）
│                 新用户的 home 初始内容，每次有 user_id 首次出现时被复制到 users/<uid>/
│                 用 shutil.copytree 整目录复制（含隐藏文件）
│
└── users/        RW，由 portal 管理写入
    ├── .portal_users.json   portal 缓存（user_id → {port, container_id, last_seen}）
    └── <uid>/               每用户独立 home，bind → 容器 /home/node
```

### 12.2 模板复制流程

```python
# portal/app.py ensure_user_dir()
def ensure_user_dir(user_id: str) -> str:
    path = os.path.join(USER_DATA_BASE, user_id)  # 容器内视角：/volumes/users/<uid>
    if not os.path.exists(path):
        if os.path.isdir(USER_TEMPLATE):           # 容器内视角：/volumes/node
            shutil.copytree(USER_TEMPLATE, path)   # ← 整目录复制
        else:
            os.makedirs(path, exist_ok=True)
            app.logger.warning("USER_TEMPLATE missing, created empty dir")
    os.chmod(path, 0o777)  # 容器内 root 也能写
    return path


def host_user_dir_for(user_id: str) -> str:
    """容器内视角 → host 视角，给 docker.run() 的 bind source 用。"""
    return os.path.join(HOST_USER_DATA_BASE, user_id)
```

- **首次创建**：从 `volumes/node/` 整目录复制到 `volumes/users/<uid>/`
- **后续启动**：跳过复制（`os.path.exists` 判断）
- **用户改 templates/node/** 不会影响已创建的用户，只影响"未来首次访问的新用户"
- **想让现有用户拿到新模板**：手动 `rm -rf volumes/users/<uid>/` 后下次访问

### 12.3 升级到 volumes/ 之前的旧路径

旧实现 `/workspace` + `/home/users` 仍然在宿主机上存在（避免破坏性升级），但 portal 已不再使用它们。要清理：

```bash
# 等所有 claude 容器关掉后：
rm -rf /home/users/<uid>...   # 旧用户数据
rm -rf /workspace/*           # 旧共享知识库（如已迁到 volumes/code/）
```

确认新方案稳定运行一段时间后再清理。

---

## 13. 内部 CA 与用户 HTTPS 证书

### 13.1 为什么需要内部 CA（不能用 self-signed）

code-server 跑在 HTTPS 上是硬需求：
- VS Code webview 依赖 `crypto.subtle` API
- HTTP + 非 localhost 是 non-secure context → webview 不能渲染 → Claude Code 扩展空白

最简方案是给每个用户启一个 `openssl req -x509 ... -subj /CN=localhost`，让用户浏览器接受 self-signed 警告。但实际跑下来**不能用**：

- VS Code 内部用 Service Worker fetch 资源
- Chrome 对 Service Worker fetch **严格校验证书**（即使主页面 accept 了警告）
- → `chrome-extension://.../service-worker.js` 直接拒绝加载
- → Claude Code 扩展 / webview 完全空白，UI 都看不到

**结论**：必须用受信任的证书签发 → 装 CA 到系统 trust store 一次 → 所有用这个 CA 签的 cert 都被信任。

### 13.2 portal 自签内部 CA

`volumes/certs/` 首次启动时自动生成（root 拥有，因为 portal 用 root 跑）：

| 文件 | 用途 |
|------|------|
| `volumes/certs/ca.crt` | CA 公钥证书，下载给用户装到 trust store |
| `volumes/certs/ca.key` | CA 私钥，签用户 cert 用（**不外传**）|
| `volumes/certs/ca.srl` | 序列号文件 |

CA 主题：`Claude Code Portal Internal CA`，有效期 10 年。

### 13.3 每用户 cert 流程（ensure_user_cert）

每次 `POST /api/start` 在容器启动前调用：

1. 取浏览器访问 Portal 时的 `Host` 头（`request.host`，如 `192.168.1.10:8080` 或 `claude.corp.com`）
2. 解析出 hostname 和（可选）port
3. 用内部 CA 签一份 cert：
   - Subject CN = hostname
   - SAN = `[DNS:hostname, IP:<解析出的 IP>, IP:127.0.0.1, IP:0.0.0.0]`（覆盖用户所有可能的访问方式）
   - 有效期 365 天
4. 写到 `volumes/users/<uid>/code-server-certs/cert.pem` 和 `key.pem`
5. 把整个目录 bind mount 进容器：`users/<uid>/code-server-certs → $HOME/.local/share/code-server`
6. 通过 `CERT_FILE` / `KEY_FILE` env 告诉 entrypoint.sh 用哪两个文件
7. entrypoint.sh 拼到 code-server 启动参数：`--cert $CERT_FILE --cert-key $KEY_FILE`

**为什么 bind 整个 `.local/share/code-server` 而不是只 bind cert？**
- cert 必须 chown 1000:1000 让容器内 node 用户能读
- 顺便也把 portal 写好的 `argv.json` / `languagepacks.json` 一并注入（决策 §11.4）
- 一个 bind mount 路径覆盖三件事，比维护多个 sub-bind 简单

### 13.4 用户安装 CA 流程

Portal `/install-cert` 路由直接返回 `volumes/certs/ca.crt` 作为文件下载。login.html 在表单上方展示安装步骤：

- **Windows**：双击 .crt → "安装证书" → 当前用户 → "受信任的根证书颁发机构" → 完成
- **macOS**：双击 .crt → 钥匙串访问弹出 → 添加到"系统"钥匙串（不是"登录"）→ 右键 cert → "显示简介" → "信任"展开 → "使用此证书时" 改为"始终信任"
- **Linux**：`sudo cp ~/Downloads/claude-code-portal-ca.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates`

**关键**：装完 CA 后必须**完全退出浏览器再打开**（macOS `Cmd+Q`、Windows `Ctrl+Shift+Q`、Linux `pkill -f chrome`）。Chrome 的 background 进程（service worker / GPU）会缓存 trust store，单纯刷新页面不生效。

装好后勾选 "我已安装 CA 证书"，localStorage 标记 `claude-code-ca-installed=1`，banner 不再显示。

### 13.5 重新生成 CA（极端情况）

如果 CA 私钥泄露或想彻底换一套：
```bash
# 停 portal
docker compose down
# 删 CA + 所有用户 cert
sudo rm -rf volumes/certs/{ca.crt,ca.key,ca.srl} volumes/users/*/code-server-certs
# 启动 portal → 重新生成 CA
docker compose up -d
# 通知所有用户重装 CA（user cert 必须重新签，否则浏览器还是会报警）
```

---

## 14. 权限映射（chown 1000:1000）

### 14.1 uid/gid 对齐

容器以 `user="node:node"`（uid/gid=1000）跑。host 上 dev 用户 `thomas` 正好也是 uid/gid=1000（典型 Linux 桌面用户），所以：

- container `node` (uid 1000) ↔ host `thomas` (uid 1000)
- 容器内创建的文件，host 上自动属于 thomas，不需要 root
- host 上创建的文件，容器内自动属于 node，不需要 root
- 两个角色都不需要 sudo 介入

### 14.2 portal 端必须 chown 的目录

`ensure_user_dir` / `ensure_user_cert` / `start_container`（创建 scratch 时）创建的所有目录都必须 `os.chown(path, 1000, 1000)`：

```python
# ensure_user_dir 末尾：
_chown_recursive(user_dir, uid=1000, gid=1000)

# ensure_user_cert 写完 cert 文件后：
os.chown(container_cert_path, 1000, 1000)
os.chown(container_key_path,  1000, 1000)
os.chown(container_cert_dir,  1000, 1000)
os.chmod(container_cert_dir,  0o777)

# start_container 创建 scratch 时：
os.makedirs(scratch_dir, exist_ok=True)
os.chown(scratch_dir, 1000, 1000)
os.chmod(scratch_dir, 0o777)
```

**为什么 portal 端做而不是容器内做？**
- portal 以 root 跑，能 chown / chmod 任何目录
- 容器以 node 跑，对 root 拥有的目录无权 chmod（`Operation not permitted`）
- 一旦 entrypoint 里写了 `chmod 1777 /home/node/scratch`，容器立刻 Exited(1)
- per-user 容器里也没必要 sticky（每个容器只有一个 uid=1000 用户访问）

### 14.3 历史遗留清理

之前的版本 portal 写完文件不 chown，留下一堆 root-owned 文件；用户手动 `sudo rm -rf volumes/users/<uid>/...` 才能清掉。现版本 portal 在所有创建路径都加了 chown，新部署是干净的。

---

## 15. Configuration（端口 + 路径全部可配置）

### 15.1 设计目标

让部署者只改 `.env` 就能切换：
- Portal HTTP 端口（默认 9900，避开 80 端口冲突）
- Claude 容器端口范围（默认 9901-9999，避开 8081）
- 所有 host 路径（项目根、volumes 子目录、Docker socket）
- 所有 claude 容器内 bind target（⚠️ 改这些要 rebuild 镜像）

`.env` 不入库（见 `.gitignore`），`.env.example` 文档化全部可配置项。

### 15.2 env var 分组（按改动频率）

#### A. 端口（最常改）

| Var | 默认 | 改后动作 |
|-----|------|----------|
| `PORTAL_HOST_PORT` | 9900 | restart portal |
| `PORTAL_CONTAINER_PORT` | 9900 | rebuild portal image |
| `CLAUDE_PORT_MIN` / `CLAUDE_PORT_MAX` | 9901 / 9999 | restart portal |

#### B. Host 路径（次常改）

| Var | 默认 | 说明 |
|-----|------|------|
| `HOST_PROJECT_DIR` | `/home/thomas/workspace/code/claude_web_images` | 项目根（开发机路径） |
| `HOST_VOLUMES_DIR` | `${HOST_PROJECT_DIR}/volumes` | volumes 总目录 |
| `HOST_USERS_DIR` / `HOST_CODE_DIR` / `HOST_TEMPLATE_DIR` / `HOST_CERTS_DIR` | `${HOST_VOLUMES_DIR}/{users,code,node,certs}` | 四个子目录 |
| `HOST_DOCKER_SOCK` | `/var/run/docker.sock` | Docker socket（macOS 改这里） |

#### C. Portal 容器内 mount point

| Var | 默认 | 说明 |
|-----|------|------|
| `PORTAL_VOLUMES_MOUNT` | `/volumes` | portal 看到 volumes 的容器内路径 |

#### D. ⚠️ Claude 容器内 bind target（改要 rebuild claude-code 镜像）

| Var | 默认 | 镜像内对应位置 |
|-----|------|----------------|
| `CLAUDE_WORKSPACE_BIND` | `/workspace` | Dockerfile WORKDIR / CMD |
| `CLAUDE_HOME_BIND` | `/home/node` | Dockerfile USER node |
| `CLAUDE_SCRATCH_BIND` | `/home/node/scratch` | app.py bind target + entrypoint.sh |
| `CLAUDE_CERT_DIR_BIND` | `/etc/code-server` | Dockerfile mkdir + cert gen |
| `CLAUDE_INTERNAL_PORT` | 8080 | Dockerfile CMD `--bind-addr` + app.py `ports=` |

#### E. 镜像 / 标签

| Var | 默认 | 说明 |
|-----|------|------|
| `CLAUDE_IMAGE_NAME` | `claude-code:local` | portal `docker.run` 拉的镜像 tag |
| `PORTAL_LABEL` | `managed-by` | Docker 容器 label key（管理 / 过滤用） |

#### F. 文件 / 子目录名（极端定制，一般不动）

| Var | 默认 | 说明 |
|-----|------|------|
| `USERS_CACHE_FILENAME` | `.portal_users.json` | portal 内部 cache 文件 |
| `CERT_DIR_NAME` | `code-server-certs` | per-user cert 目录名 |
| `CA_CERT_FILENAME` / `CA_KEY_FILENAME` | `ca.crt` / `ca.key` | 内部 CA 文件名 |
| `CLAUDE_CERT_FILENAME` / `CLAUDE_KEY_FILENAME` | `cert.pem` / `key.pem` | per-user cert/key 文件名 |
| `SCRATCH_DIR_NAME` | `scratch` | per-user 临时区目录名 |
| `WORKSPACE_FILE_NAME` | `workspace.code-workspace` | 多根 workspace 文件名 |
| `SAN_CONFIG_NAME` / `CSR_FILENAME` | `san.cnf` / `cert.csr` | openssl 临时文件 |

### 15.3 文件分发

| 文件 | 读什么 env |
|------|------------|
| `docker-compose.yml` | `PORTAL_*`, `CLAUDE_PORT_*`, `HOST_DOCKER_SOCK`, `HOST_VOLUMES_DIR`, `PORTAL_VOLUMES_MOUNT` |
| `portal/Dockerfile` | `PORTAL_CONTAINER_PORT`（ARG，build time） |
| `portal/app.py` | 全部 host 路径 + claude 容器路径 + 端口 + 文件名 |
| `entrypoint.sh` | `CLAUDE_WORKSPACE_BIND`, `CLAUDE_HOME_BIND`, `CLAUDE_SCRATCH_BIND`, `CLAUDE_SCRATCH_DIR_NAME`, `CLAUDE_WORKSPACE_FILE_NAME`, `CERT_FILE`, `KEY_FILE`（后两个由 portal 注入绝对路径） |

### 15.4 改动流程

| 改动 | 操作 |
|------|------|
| 端口（`PORTAL_*`, `CLAUDE_PORT_*`） | 改 `.env` → `docker compose up -d` |
| Host 路径 | 改 `.env` → `docker compose up -d` |
| 容器内 bind target（`CLAUDE_*_BIND` 等） | 改 `.env` → `docker build . -t claude-code:local` → `docker compose up -d` |
| `PORTAL_CONTAINER_PORT` | 改 `.env` → `docker compose build portal` → `docker compose up -d` |

### 15.5 默认值变更历史

| 日期 | 旧默认 | 新默认 | 原因 |
|------|--------|--------|------|
| 2026-07-19 | 80 / 8081 | 9900 / 9901-9999 | 用户要求避开 80/8081（与其他常用服务冲突）|

---

## 16. 用户自助重建容器（/api/rebuild）

### 动机
凭据存浏览器，本地可改。但 Claude Code 容器里的环境变量是启动时一次性读入的——
用户改了 `baseUrl` / `opusModel` 等任何字段，旧容器的 env 仍是旧值，必须重建。

旧版要"清浏览器 localStorage 再来一遍"才能改 → 用户抱怨不便。
新版在 step2 提供 **"⚠ 重建容器"** 入口，浏览器侧检测到凭据 hash 变了 → 自动走 `/api/rebuild`。

### 检测机制（浏览器侧）
- 提交时计算 `hashCreds(creds)`（5 字段稳定字符串 + 简单 djb2 hash）
- 与 `localStorage["claude-code-creds-hash"]` 比对
- 不同 → `POST /api/rebuild`；相同 → `POST /api/start`（后者会复用现有容器，更便宜）

### 服务端流程
```
POST /api/rebuild { baseUrl, apiKey, opusModel, sonnetModel, haikuModel, resetHome?, syncTemplate?, resetScratch? }
  → 1) 旧容器 stop(timeout=10) + remove(force=True)
  → 2) reset_home=True  → ensure_user_dir(user_id, wipe=True) [rmtree 整个 home → copytree 模板]
     否则若 sync_template=True → sync_template_to_user(user_id) [增量覆盖：模板有→覆盖，模板无→不动]
  → 3) start_container(..., wipe_scratch=resetScratch) [内部删 scratch]
  → 返回 { port, password, user_id }
```

**顺序关键**：先 stop+remove 容器，再删/同步目录。反过来容器还在跑就会写到被删的目录。

### reset 选项语义
| 选项 | 删什么 | 改什么 | 影响 |
|------|--------|--------|------|
| 不勾 | — | — | 用户 home settings / extensions / scratch / 对话历史全部保留 |
| `resetScratch` | `users/<uid>/scratch/` | — | 清临时区，保留 home（含 settings / 扩展 / 对话历史） |
| `syncTemplate` | — | `volumes/node/` 的文件**增量**覆盖到 `users/<uid>/` 同相对路径 | 模板里有的被覆盖（更新/新增），模板里没有的保留（用户的扩展缓存、对话历史、settings 全部不动） |
| `resetHome` | 整个 `users/<uid>/` | 从 `volumes/node/` 全量重建 | Code Server settings、扩展缓存、对话历史、displayName meta、固定端口 .port 全部清空 |

**包含关系（互斥语义）**：
- `resetHome` 隐含 `resetScratch`（scratch 是 home 子目录，rmtree 会一起删）
- `resetHome` 隐含 `syncTemplate`（copytree 是 sync 的超集——删完整个再拷全部，效果就是 sync 全部覆盖）
- `syncTemplate` 和 `resetScratch` 语义正交（一个动 home，一个动 scratch 子树），但前端三选一互斥避免误操作

**典型用例**：
- 管理员改了 `volumes/node/.claude/hooks/foo.sh` 想推给所有用户 → 用户/管理员点"同步模板"，对话历史和扩展缓存都保留
- 用户的 Claude Code 状态错乱 → 点"重置用户目录"，从模板全新来
- 用户报"scratch 里临时文件太多" → 点"重置临时目录"，只清 scratch，settings 和对话历史保留

---

## 17. 管理员面板（/admin）

### 动机
portal 编排的所有容器都得能在服务器端被管理（容器卡死 / 磁盘满 / 误操作 / 离职）。
原本 `/api/users` 是只读的，运维只能 SSH 上去 docker rm。
新版提供独立 `/admin` 页面 + `ADMIN_PASSWORD` 鉴权。

### 鉴权
- 环境变量 `ADMIN_PASSWORD`：管理员密码（**不要与 per-user 密码混用**）
  - 留空 / 未设 → 整个 `/admin` 路由 + `/api/admin/*` 全 404（关掉管理面板）
- **登录机制**：用 Flask 的有签名 cookie session（不是 in-memory token）
  - `POST /api/admin/login { password }` → server 写 `session["admin"] = True; session.permanent = True`
  - 后续请求浏览器自动带 cookie，server 校验 `session.get("admin") is True`
  - **关键**：必须显式设 `FLASK_SECRET` env（或由 portal 从 `HOST_PROJECT_DIR` 派生）——
    gunicorn `-w N` 默认 `preload_app=False`，每个 worker 各自 import app → 如果 secret_key
    来自 `secrets.token_hex(32)` 之类的"启动时随机"源，每个 worker 会拿到不同 key，
    cookie 签名校验失败 → "登录后随机 401"（详见变更日志）
- Session 有效期：默认 8h（`ADMIN_SESSION_MAX_AGE` env 可调）
- Cookie 清空：`session.clear()` —— 浏览器立即失效，无须等服务端
- 失败计数：按客户端 IP 5 次失败 → 60 秒冷却（仅防手动爆破；多 worker 下每 worker 各一份，不是分布式严格限流）

### 路由清单

| 路径 | 方法 | 用途 |
|------|------|------|
| `/admin` | GET | 登录页 / 容器列表页（HTML） |
| `/api/admin/login` | POST | 登录，返回 token |
| `/api/admin/logout` | POST | 退出（清空所有 token） |
| `/api/admin/containers` | GET | 列出所有 portal 编排的容器（按 last_seen 倒序） |
| `/api/admin/stop` | POST | 停止单个容器 `{ userId }` |
| `/api/admin/delete` | POST | 删容器 `{ userId, wipeHome?, wipeScratch? }` |
| `/api/admin/rebuild` | POST | 重建容器 `{ userId, resetHome?, syncTemplate?, resetScratch? }` —— 从原容器 env 读 apiKey/baseUrl/models，无需用户提供 |

### "重建"为什么 admin 端现在也能做（§20）

以前：admin 路径只能 stop/delete，因为没有 apiKey 也故意不持久化。
现在：admin rebuild 不需要 apiKey —— `start_container()` 启动时把 apiKey/baseUrl/models
写进容器 env（`Config.Env`），admin 通过 `docker inspect`（实际只读已在内存的
`container.attrs`，不发网络请求）即可提取，复用给 `rebuild_container()`。

约束：
- **原容器必须存在**（哪怕已 exited）—— env 在 `attrs["Config"]["Env"]`，跟容器状态无关
- 找不到原容器 → 400 让 admin 引导用户重新登录一次（用户登录时 portal 把 apiKey
  写进 env，下次 admin 就能 rebuild）
- **cache 仍不存 apiKey** —— admin rebuild 用完即弃，container 删了 env 也跟着没了

### UI
- 与用户门户同款 workshop-ticket 美学（ticker strip / mono 标签 / hairline 表格）
- 登录态 5 秒自动刷新一次
- 单行三个动作：**重建** / 停止 / 删除 ▼
- 重建面板：勾选 resetHome / resetScratch 跟用户门户完全等价（reset_home=True 会
  把 .portal_meta.json 一起删，displayName 丢失，跟用户自己 rebuild 行为一致）
- 重建 / 删除 面板互斥展开（展开一个先收起另一个），防止误操作

---

## 18. 用户辅助标识 `displayName`

### 动机
管理员面板原只能看到 16 位 hex user_id，难以识别"这是谁"。引入一个**纯辅助**显示名让管理员一眼对应真人。

### 设计原则
**不影响用户隔离机制**。`displayName` 是纯元数据：
- `user_id = sha256(apiKey)[:16]` 仍是唯一的鉴权 / 隔离 / 路径分段依据
- displayName 不参与 hash、不写路径、不参与容器名 / 端口分配
- 留空合法（admin 表格里显示 `—`）
- 唯一性：**不强制**。两个用户选同一个名字是允许的（只是 admin 看到的标签撞了，不影响系统）

### 存储
- 文件：`volumes/users/<uid>/.portal_meta.json`（每个用户目录一个）
- 格式：
  ```json
  {"display_name": "张伟（产品组）", "display_name_updated_at": 1784453412}
  ```
- 文件 0666 权限 + portal 已 chown 1000:1000（继承自 `ensure_user_dir` 的递归 chown）
- rebuild 时若勾选 `resetHome`，meta 文件跟整个用户目录一起被删 —— 客户端必须在 rebuild 请求里**重传** displayName 保留名字

### 服务端 API
| 端点 | 方法 | 行为 |
|------|------|------|
| `/api/start` | POST | 接受可选 `displayName`。**仅在新建容器时**写 meta（reused 分支也允许更新） |
| `/api/rebuild` | POST | 接受可选 `displayName`。resetHome=True 时 meta 被删，客户端应重传 |
| `/api/profile` | PATCH | **改名专用**（不动容器）。鉴权：必须出示对应 apiKey → `user_id = sha256(apiKey)[:16]` 是单射 |
| `/api/status/<uid>` | GET | 返回 `display_name`（供前端在 reload 时回填） |
| `/api/admin/containers` | GET | 每行带 `display_name` 字段 |

### 输入清洗 `_sanitize_display_name`
- 剥控制字符（保留空格）
- 截断到 40 字符
- 白名单字符类：`A-Za-z0-9` / 中文（`一-鿿` + 日文假名） / `-._()` / 全角标点 `，。：；！？、《》""''【】「」` / 空格
- 不在白名单的字符（特别是 `<` `>` `&` `"` `'` 反引号等 HTML / shell 注入字符）会被剥掉
- **不**做唯一性检查；admin 端只做 HTML escape 渲染

### 客户端 UI
- Step 1 表单加一行"显示名称（可选）"，placeholder `张伟（产品组）`
- Step 2 ticket strip 多一段 `TICKET #xxxxxxxx · 张伟（产品组）`（名字为空时不显示分隔符）
- Step 2 底部多一个 `✎ 改名` 链接，弹出 mini panel 调 `/api/profile` PATCH（不动容器，秒级完成）
- localStorage 存 displayName，下次 start 自动重传（rebuild 也带）—— **resetHome 后名字会丢**，客户端应在重建请求中带 displayName 保留

### Admin 表格
表头新增 NAME 列：
- 网格列宽 `180px 160px 70px 100px 120px 1fr`（uid 收窄一点给名字让位）
- 未设名字显示 `—`（灰色斜体）

---

## 19. 每用户端口固定（`.port` 文件）

### 动机
原端口分配是"next free"——同一个用户每次 rebuild / portal 重启都可能落到不同端口。带来的问题：
- 用户书签失效（`https://server:9905/` → rebuild 后变 9912）
- 防火墙规则 / Nginx 反代 / 文档链接需要跟着变
- 心理上不"专一"，每个用户应该有稳定身份

引入**首次分配后固定**：每个用户的 host port 一旦分配就一直跟着他；rebuild / 重启 portal / 重建 claude 镜像都不变。

### 设计原则
- **简单持久化**：用文件 `volumes/users/<uid>/.port`，跟用户数据同寿（同一个目录）。副本即备份，跟着 `volumes/` 整体迁移
- **零配置默认**：不需要建表 / 不需要全局 manifest；每个 `.port` 是独立的、单值文件
- **失败降级**：`.port` 损坏 / 范围外 / 被其他用户占着 → 自动 `next_free_port()` 重摇并覆写，**不阻断**用户登录
- **不破坏现有用户**：升级前已建的用户没 `.port` → 自动走 `next_free_port()`，首次重启后写入；过渡期没有数据迁移成本
- **wipe_home 连带**：rebuild 勾"重置用户目录"会删 `.port`（语义一致：完全清空 = 重摇端口）；想保留端口仅重置内容就手动 `cp volumes/users/<uid>/.port /tmp/ && rebuild && cp /tmp/.port back/`
- **不参与隔离**：端口是元数据，与 `user_id = sha256(apiKey)[:16]` 完全正交；切端口不会改变鉴权

### 存储格式
- 路径：`volumes/users/<uid>/.port`
- 内容：`ASCII int + \n`（例 `9903\n`），由 `_write_pinned_port` 用 `<path>.tmp + os.replace` atomic 写
- 权限：`0666` + chown `1000:1000`（继承自 `ensure_user_dir` 的递归 chown；容器内 node 进程理论上不会碰它，但防御一下）
- portal 写、host 看；容器内看不到这文件（也没必要）

### 核心函数 `alloc_port_for_user(user_id)`
取代之前 `next_free_port()` 在 `start_container` 里的直接调用：

```python
def alloc_port_for_user(user_id: str) -> int:
    used = get_used_ports()
    saved = _read_pinned_port(user_id)
    if saved is not None and MIN <= saved <= MAX and saved not in used:
        return saved
    # 降级日志：记到 portal log，admin 出问题能溯源
    if saved is not None:
        app.logger.warning(f"alloc_port: saved {saved} for {user_id[:8]} <reason>, re-allocating")
    p = next_free_port()
    _write_pinned_port(user_id, p)
    return p
```

降级路径（每条都在 log 打 WARNING）：

| `.port` 状态 | 行为 |
|---|---|
| 文件不存在 | 首次分配 → `next_free_port()` → 写回 |
| 文件存在 + 合法 + 未被占用 | **用 saved**（主要路径）|
| 文件存在 + 合法 + **被其他容器占用** | 重摇 + 覆写（WARNING: `taken by another container`）|
| 文件存在 + 范围外（`saved < MIN` 或 `> MAX`）| 重摇 + 覆写（WARNING: `out of range`）|
| 文件内容损坏（非 int / 空 / 多行）| 重摇 + 覆写（WARNING: `invalid`）|

### 与现有路径的交互
- **stop 容器**（admin stop）：不删 `.port`；下次 start 同端口
- **admin delete**（不带勾）：不删 `.port`；下次 user 登录同端口
- **admin delete + wipeHome**：删整个用户目录 → `.port` 一并消失；下次 user 登录新分配
- **admin delete + wipeScratch**：只删 scratch；`.port` 保留
- **rebuild（不带 reset_home）**：`.port` 保留 → 同端口
- **rebuild + resetHome=True**：删整个用户目录 → 重摇
- **portal 重启 / 容器重建**：`.port` 都在（host 卷持久化）→ 同端口

### Admin UI
`/api/admin/containers` 返回每个容器：
- `port` — 实际绑定 host port（来自 `NetworkSettings.Ports.8080/tcp[0].HostPort`）
- `pinned_port` — `.port` 文件里的值；可能 `null`（升级前老用户）

表格 PORT 列渲染：
```
9905 ✓    jade 绿色 — 固定端口与实际一致（稳定）
9905 ⚠    rust 红色 — 固定端口被占，已自动重摇（hover 看 .port=X）
9905      无标记   — 升级前用户没有 .port（首次 stop+start 后会落到下次 free）
—         灰色      — 容器 exited 没绑 port
```
CSS：`.port .pin-tag { display: inline-block; margin-left: 4px; font-weight: 700; }`，`.pin-ok { color: var(--jade); }` / `.pin-warn { color: var(--rust); }`

### 与 §15 env vars 关系
不改 `CLAUDE_PORT_MIN` / `CLAUDE_PORT_MAX` 语义（仍是分配范围）。**改了 MIN/MAX 后**：
- 旧 `.port` 文件落新范围外 → 自动重摇到新范围内
- 不需要任何迁移脚本

### 内部并发性
- `_read_pinned_port` → `_write_pinned_port` 是 read-modify-write；两个 worker 同时跑 alloc_for same uid 时可能都读到 saved、都判断合法、都返回 saved——OK 因为同一 uid 永远拿到同一端口（binding 在 docker 层竞争，第二个会失败）
- `_write_pinned_port` 用 `tmp + os.replace`：单值写入本身原子，不会写出半截文件
- 同 uid（= 同 apiKey）在 UI 上不会并发（只有一个用户点启动按钮）

### 边界行为已通过测试矩阵覆盖
1. 新用户 → `.port` 创建
2. stop + start → 同端口
3. rebuild → 同端口
4. rebuild + resetHome → `.port` 被删，自动重摇（落在最低空闲）
5. `.port` 在范围内 → 复用
6. `.port` 落范围外 → 重摇
7. `.port` 内容损坏 → 重摇
8. `.port` 被其他用户占着 → 重摇并覆写

---

## 20. 管理员重建容器（`/api/admin/rebuild`）

详见 §17 路由清单 + 下面 changelog 2026-07-22 条目。核心要点：

- **实现路径**：`rebuild_container()` 本来就吃 apiKey/baseUrl/models，admin 只需从
  原容器 env 读出来（不修改 `rebuild_container` 本身）。读取走 `container.attrs["Config"]["Env"]`，
  docker inspect 在容器创建时就已记录，**不发网络请求**，镜像被 rmi 也不影响。
- **不破坏"不持久化 apiKey"原则**：apiKey 从来不写进 cache；admin rebuild 用的是 env，
  env 跟着容器走，container 删了 env 也消失。
- **唯一短板**：原容器不存在（用户从未登录过，或已被外部 rm） → admin 没法重建。
  返回 400 让 admin 引导用户重新登录一次（登录会把 apiKey 写进新容器的 env，下次能 rebuild）。
- **resetHome / resetScratch 语义跟用户自己 rebuild 完全一致**：resetHome=True 会把
  `.portal_meta.json` 一起删（displayName 丢失），`.port` 跟着被删后下次登录重摇。

---

## 21. 门户访问密码（`PORTAL_PASSWORD`）

### 动机
portal 首页本身是无认证的：谁能访问到 9900 端口，谁就能拉起一个容器、消耗宿主资源。
内网部署时这没问题；一旦挂到公网 / 半开放网络上，就需要一道"谁能用这个 portal"的公共门。
注意这不是 per-user 认证 —— 用户身份仍由各自的 API key 派生（§2），门只解决"外人进不来"。

### 开关语义
- `PORTAL_PASSWORD` 留空 / 未设 → **门禁整个不生效**，所有请求原样放行（旧部署零改动升级）
- 设了值 → 未登录访问 `/` 得到密码门页面（HTTP 401 + `templates/gate.html`），
  未登录访问 `/api/*` 得到 `401 {"error": "未登录或会话已过期"}`

### 实现
- **全局 `@app.before_request`（`portal_gate`）**，不是逐路由装饰器：
  以后新增路由默认受保护，要放行必须显式写进 `_PORTAL_GATE_EXEMPT`——
  方向上比"新加路由忘了加装饰器"安全。
- **豁免前缀** `_PORTAL_GATE_EXEMPT`：
  - `/static/` — 页面自己的 css/js
  - `/api/portal/login`、`/api/portal/logout` — 登录入口本身（否则进不去）
  - `/admin`、`/api/admin/` — 自带 `ADMIN_PASSWORD` 认证（§17），不串两道密码
- **session 键与 admin 分开**：`session["portal"]` vs `session["admin"]`，同一套签名 cookie
  机制与 `FLASK_SECRET`（§17 的多 worker 注意事项在这里同样成立）。
- **`api_admin_login` 成功时顺带写 `session["portal"] = True`**：那里有一句
  `session.clear()`，不补这一条，管理员登录 admin 后回首页会被要求重新输门户密码。
- **防爆破**：与 admin 共用 `login_cooldown_check()` / `login_record_failure()`
  （5 次 / 60 秒），但 **各用一个 IP 计数桶** —— 门户密码被撞冷却时不该把管理员一起锁在门外。
- **有效期**：复用 `app.permanent_session_lifetime`，即 `ADMIN_SESSION_MAX_AGE`（默认 8h）。

### 前端
- `templates/gate.html`：独立页面，沿用 login.html 的设计 token，只带自己需要的样式。
  选独立页而不是在 login.html 里加遮罩，是为了**未登录用户拿不到门户页面的任何结构与文案**。
- `static/app.js` 的 `handleGate401(resp)`：session 过期后 API 回 401 → 直接 `location.reload()`，
  服务端这次渲染密码门。未设密码时后端不会产生 401，这条分支永不触发。

### 运维备注
- 改 `PORTAL_PASSWORD` **不会**踢掉已登录的人（cookie 由 `FLASK_SECRET` 签名，不含密码）。
  要强制全员重登，换 `FLASK_SECRET`。
- 密码门只挡 portal 自己（9900）。用户容器的 code-server 端口（9901+）是另一套认证
  （per-user 随机密码，§2），不受这里影响。

---

## 附录 A：环境变量速查

**Portal 容器**（从 `.env` 注入，详见 §15）：
- 端口：`PORTAL_HOST_PORT` / `PORTAL_CONTAINER_PORT`（默认 9900）
- Claude 端口范围：`CLAUDE_PORT_MIN` / `CLAUDE_PORT_MAX`（默认 9901-9999）；每用户固定持久化到 `volumes/users/<uid>/.port`（§19）
- 活跃容器上限：`MAX_ACTIVE_CONTAINERS`（默认 0 = 不限）
- 路径：完整列表见 `.env.example`

**Claude 容器（Portal 注入）**：
```
ANTHROPIC_BASE_URL              用户填
ANTHROPIC_API_KEY               用户填（敏感，不写日志）
ANTHROPIC_MODEL                 = opusModel（兼容老版 Claude Code）
ANTHROPIC_DEFAULT_OPUS_MODEL    用户填（opusModel）
ANTHROPIC_DEFAULT_SONNET_MODEL  用户填（sonnetModel）
ANTHROPIC_DEFAULT_HAIKU_MODEL   用户填（haikuModel）
PASSWORD                        = user_id（确定性，见决策 8）
CERT_FILE                       ${CLAUDE_CERT_DIR_BIND}/${CLAUDE_CERT_FILENAME}
KEY_FILE                        ${CLAUDE_CERT_DIR_BIND}/${CLAUDE_KEY_FILENAME}
CLAUDE_WORKSPACE_BIND           ${CLAUDE_WORKSPACE_BIND}
CLAUDE_HOME_BIND                ${CLAUDE_HOME_BIND}
CLAUDE_SCRATCH_BIND             ${CLAUDE_SCRATCH_BIND}
CLAUDE_SCRATCH_DIR_NAME         ${SCRATCH_DIR_NAME}
CLAUDE_WORKSPACE_FILE_NAME      ${WORKSPACE_FILE_NAME}
```

**Admin 鉴权**（§17）：
- `ADMIN_PASSWORD` — 管理员密码；空字符串 → 整个 admin 路由 404（关闭管理面板）
- `FLASK_SECRET` — Flask session 签名密钥；不设 portal 每次启动随机生成一个（仅 dev 可接受）

**门户访问密码**（§21）：
- `PORTAL_PASSWORD` — 门户公共密码；空字符串 → 免登录（默认，旧行为）
- session 有效期复用 `ADMIN_SESSION_MAX_AGE`（默认 8h），签名密钥复用 `FLASK_SECRET`

---

## 附录 B：变更日志

| 日期 | 变更 |
|------|------|
| 2026-07-18 | Phase 1 完成，Portal 上线 |
| 2026-07-18 | password 由随机改为 user_id（决策 8）|
| 2026-07-19 | claude-code 镜像优化：vendor/*.vsix + 中文 + Claude Code 扩展 + 默认 /workspace（决策 9、§11）|
| 2026-07-19 | bind mount 切到项目本地 volumes/（决策 10、§12）；新增 volumes/node 模板 + ensure_user_dir 自动复制 |
| 2026-07-19 | 修复 portal 看不到 host volumes/node 问题：明确"双视角路径"——portal 用容器内视角（`/volumes/...`）读写 volumes，docker.run() 用 host 视角（`${HOST_PROJECT_DIR}/volumes/...`）做 bind source（§5.2）|
| 2026-07-19 | 容器从 `user="0:0"`（root）改为 `user="node:node"`：`$HOME` 默认就是 `/home/node` 对齐 bind mount，更安全；同时 `entrypoint.sh` 加 chmod 修复 vendor vsix 内 native binary 丢失可执行位的问题（audio-capture.node 在 zip 内就是 0644）|
| 2026-07-19 | **4 个 ANTHROPIC 模型 env vars**：ANTHROPIC_MODEL + 3 个 ANTHROPIC_DEFAULT_*_MODEL；浏览器侧表单从单 model 字段拆为 3 个（opus/sonnet/haiku），portal 把 opusModel 复制到 ANTHROPIC_MODEL 兼容老版 Claude Code（§5.3、§7）|
| 2026-07-19 | **Multi-root workspace**（决策 11）：entrypoint.sh 生成 $HOME/workspace.code-workspace，包含 /workspace + $HOME/scratch 两个根；/workspace 放第一位保持 Claude Code 默认 CWD；scratch 是独立 bind `users/<uid>/scratch → /home/node/scratch`，per-user 隔离 |
| 2026-07-19 | **内部 CA + 用户 HTTPS 证书**（§13）：portal 自签 Claude Code Portal Internal CA，每次 /api/start 用 Host SAN 签用户 cert → bind 进容器；self-signed 不行因为 Chrome Service Worker 严格校验；用户首次访问在 /install-cert 下载 CA 装到系统 trust store |
| 2026-07-19 | **权限映射（§14）**：portal 端所有创建的目录都 chown 1000:1000 + chmod 0777（thomas 主机用户 = node 容器用户，都是 uid/gid 1000）；container 端不能 chmod root-owned 目录所以 portal 端做 |
| 2026-07-19 | **VS Code 中文 UI 兜底**：entrypoint.sh 手动写 `User/languagepacks.json` 指向中文包扩展的 `translations/main.i18n.json`；code-server 4.x 在某些场景下不会把中文扩展加载到 exthost → languagePacks service 不写 file → UI 永远英文（手动写绕开）。**注：2026-07-20 发现这个路径是错的**——zp() 读的是 `<userDataDir>/languagepacks.json`（根目录），不是 `User/` 子目录；且 key 当时写成了扩展 ID 而不是 locale 字符串 → 这个兜底其实从来都没生效，新用户容器默认就是英文；详见 2026-07-20 修复条目 |
| 2026-07-20 | **新容器默认中文 UI 修复**：entrypoint.sh 写 `languagepacks.json` 的路径 + key 改成正确的——路径从 `User/languagepacks.json` 改到 `<userDataDir>/languagepacks.json`（根目录，zp() 实际读取的位置），key 从扩展 ID `"ms-ceintl.vscode-language-pack-zh-hans"` 改成 locale 字符串 `"zh-cn"`（KW() 按 locale 查 key，找不到会回退 `zh-cn` → `zh`），`translations.vscode` 用绝对路径（zp() 检查 file exists）。新镜像 hash `61de3527`，浏览器实测默认中文 UI 生效。`User/argv.json` 路径本来就是对的（appSettingsHome 解析），保持不动 |
| 2026-07-19 | **端口 + 路径全可配置**（§15）：portal 9900 / claude 9901-9999 默认值；HOST_* / CLAUDE_*_BIND / CLAUDE_*_FILENAME 全 env-driven；改 bind 路径要 rebuild claude-code 镜像，其他纯 runtime |
| 2026-07-19 | **白主题门户 + 2 步向导 + 管理员面板**（§16 / §17）：portal/login.html 改为白色 workshop-ticket 美学 + 步骤导轨（01/02），step1 输入凭据 → step2 容器 URL + 密码 + 打开；用户改任意字段可"回到上一步"→ 自动检测改动 → 走 /api/rebuild 重建容器（带 reset_home / reset_scratch 选项）；独立 /admin 页面 + ADMIN_PASSWORD 鉴权 → 列出所有用户容器，可停止 / 删除（可选清数据）|
| 2026-07-19 | **admin 鉴权机制修正**：原方案用 in-memory `_admin_tokens: set` → gunicorn 多 worker 下 token 不共享；改为 Flask 签名 cookie session（无状态），同时强制 `FLASK_SECRET` 跨 worker 一致（`HOST_PROJECT_DIR` 派生兜底）—— 根除了"登录后 5 秒自动过期"症状 |
| 2026-07-19 | **中文语言包版本对齐**：vendor/MS-CEINTL.vscode-language-pack-zh-hans.vsix 从 v1.128.x 升到 v1.129.0（`engines: ^1.129.0`），与 code-server v4.129.0 内核的 VS Code 1.129.0 严格匹配 —— 此前版本错配可能是中文 UI 时有时无的根因之一；用户需 `docker build . -t claude-code:local` + 重建用户容器（保留 home dir 让 entrypoint 重写 argv.json） |
| 2026-07-19 | **CA 安装步骤重写**：portal CA banner 步骤按 Windows 实际证书导入向导流程重写（Open File 安全警告 → 用户/本地 store 选择 → "Trusted Root Certification Authorities" → Finish → **Security Warning 必点 Yes** → 完全退出 Chrome），加了 Edge / Firefox / iOS Safari 兼容性说明（Firefox 单独 trust store 不跟系统，iOS Safari 没法用）|
| 2026-07-19 | **端口 + 路径全可配置（§15）**：新增 .env，所有端口/路径/文件名 env-driven；默认 portal 9900 / claude 9901-9999（旧 80/8081 写为 fallback）；portal/app.py / docker-compose.yml / portal/Dockerfile / entrypoint.sh / .env.example 全部改完，验证 claude 容器启动后 `ports={"9901/tcp": ...}` 和内部 8080 正确 |
| 2026-07-19 | **displayName 辅助标识（§18）**：用户可填一个名字（"张伟（产品组）"），纯辅助不参与 hash/隔离/路径；存 `volumes/users/<uid>/.portal_meta.json`；新接口 `/api/profile` PATCH（不动容器）；`/api/start` / `/api/rebuild` 接受 displayName 参数；admin 表格新增 NAME 列。后端 sanitize（白名单字符 + 40 字截断 + 去 control char），HTML escape 渲染防 XSS |
| 2026-07-20 | **每用户端口固定（§19）**：首次分配后写到 `volumes/users/<uid>/.port`，rebuild / 重启 portal / 重建镜像都复用同端口；新函数 `alloc_port_for_user` 取代 `next_free_port()`，覆盖 5 条降级路径（破坏/范围外/占用/范围外/不存在）每条都 WARNING 日志；admin PORT 列加 ✓/⚠/无标记 反映 pinned vs 实际；wipeHome 连带删 .port（语义：完全重置）。实测 8 项矩阵全绿 |
| 2026-07-21 | **`languagepacks.json` 必填字段补齐 → Claude Code 扩展激活修复**：上一条 2026-07-20 修复后用户报"claude code 扩展加载不出来"——F12 console 看 `getInstalledLanguages` 抛 `TypeError: Cannot read properties of undefined (reading '0')`，阻塞 `scanExtensions → _resolveExtensionsDefault` 流程（server-main.js:680345）。根因是 entrypoint 写的 `languagepacks.json` 缺两个必填字段：`extensions[]`（getInstalledLanguages 取 `i.extensions[0].extensionIdentifier.id`）和 `label`（createQuickPickItem 用）—— 没有 `extensions` 字段不只是"语言包加载失败"，会让 code-server 4.129.0 **整条扩展解析中断**，anthropic.claude-code 等所有第三方扩展被过滤掉（不止语言包）。修复：从 ms-ceintl 扩展的 `package.json` 动态读 `version` + `localizedLanguageName`/`languageName` + `publisher`/`name` 拼出完整格式 `{hash, extensions:[{extensionIdentifier:{id:"..."}, version:"..."}], label, translations}`。新镜像 hash `259db81f`，浏览器实测 Claude Code 扩展图标出现、点击能正常激活。**经验**：vscode 扩展 service 写出的 cache file 格式比 generateNls 的最小需求更严格，自己手写必须按完整 schema 来。 |
| 2026-07-22 | **管理员支持给用户重建容器（§17/§20）**：新增 `POST /api/admin/rebuild { userId, resetHome?, resetScratch? }`。admin 不需要 apiKey —— `start_container()` 启动时把 apiKey/baseUrl/models 写进容器 env（`Config.Env`），admin 通过 `container.attrs["Config"]["Env"]`（已缓存，不发网络请求）即可提取，复用给 `rebuild_container()`。**严格遵守"不持久化 apiKey"原则**：apiKey 从来不写进 cache，admin rebuild 用的是 env，env 跟着容器走，container 删了 env 也消失。约束：原容器不存在（用户从未登录 / 已被外部 rm）→ 400 让 admin 引导用户重新登录一次（登录后 env 就有 apiKey 了，下次能 rebuild）。resetHome/resetScratch 语义跟用户自己 rebuild 完全一致（resetHome=True 会把 `.portal_meta.json` 一起删 → displayName 丢失）。admin.js 每行 actions 加"重建 ▼"按钮（蓝色 btn-primary，跟删除按钮区分），展开 panel 跟删除 panel 互斥（toggle 时互关），提交后 listStatus 显示新端口+密码，admin 转交用户。 |
| 2026-07-22 | **rebuild 加 `syncTemplate` 选项（增量同步模板，§16）**：在原有 `resetHome` / `resetScratch` 之外新增第三个选项。语义：把 `volumes/node/` 的文件**增量同步**到 `volumes/users/<uid>/`（模板有的覆盖同名文件，模板没有的**不动**）—— 典型场景是管理员改了模板里某个 `.claude/hooks/foo.sh` 想推给所有用户，用 `syncTemplate` 而不是 `resetHome`（resetHome 会清空对话历史、扩展缓存、settings，不可接受）。后端新函数 `sync_template_to_user()`（`os.walk(USER_TEMPLATE)` + `shutil.copy2` + `os.makedirs(exist_ok=True)`，不删任何东西；完成后 `_chmod_user_dir` + `_chown_recursive` 跟 `ensure_user_dir` 同样收尾）；`rebuild_container()` 加 `sync_template` 参数，`reset_home=True` 时自动 no-op（copytree 已包含 sync 效果）；`/api/rebuild` 和 `/api/admin/rebuild` 都接受 `syncTemplate` 字段。前端三选一互斥（reset_home 超集 / sync_template 增量 / reset_scratch 子集），勾一个清掉另外两个，避免误以为三个能同时生效。文档 §16 重写为 4 行 markdown 表说明各选项 + 包含关系 + 典型用例。 |
| 2026-07-22 | **修 step2 URL/password 复制按钮无效**：旧实现 `navigator.clipboard.writeText()` 在非 secure context（HTTP 的 192.168.1.2:9900）直接抛 "Document is not focused" / 权限拒绝，被 catch 静默吞掉 —— 用户看到 "已复制 ✓" 但实际啥也没复制。改成两段式：1) secure context 优先走 `navigator.clipboard.writeText`；2) fallback 用隐藏 `<textarea>` + `document.execCommand("copy")`（HTTP 上仍能工作，所有浏览器都还支持）。失败时按钮文案改为"复制失败 — 手动选"而不是假装成功。 |
| 2026-07-22 | **显式设 `SESSION_COOKIE_SAMESITE = "Lax"` 修 Chrome admin 401 症状**：用户报 F12 → Application 看到 session cookie，但后续 /api/admin/containers 请求不带 → 401。根因：Flask 默认 SESSION_COOKIE_SAMESITE=None → Set-Cookie 不输出 SameSite 属性 → Chrome 80+ 对"没写 SameSite 的 cookie"按 Lax 处理，但**对纯 IP 地址（192.168.1.2 这种）**新版 Chrome site 算法古怪，会拒绝发送。显式设 Lax 后 Set-Cookie 带 `SameSite=Lax`，行为可预期。**和今天的代码改动无关**——curl 测一切正常，问题在浏览器对 IP 的处理策略收紧。修复：app.py 里加 `app.config["SESSION_COOKIE_SAMESITE"] = "Lax"`，portal 镜像重建并 `docker compose up -d portal` 重启。 |
| 2026-07-22 | **镜像装齐 anthropics/skills（docx/pptx/xlsx/pdf）依赖 + 常用系统工具 + Claude Code 2.1.217**：补 apt 包 `unzip/zip/xz-utils/bzip2/p7zip-full/jq/poppler-utils/qpdf/tesseract-ocr+tesseract-ocr-chi-sim+eng/pandoc/imagemagick/libreoffice/python3+python3-pip/coreutils/fonts-noto-cjk+fonts-liberation+fonts-dejavu`；补 pip 包 `pypdf/pdfplumber/pdf2image/pytesseract/reportlab/pypdfium2/Pillow/markitdown[pptx]/python-pptx/defusedxml/lxml/openpyxl/pandas`；补 npm 包 `docx/pptxgenjs/react/react-dom/react-icons/sharp`。vendor 替换 `Anthropic.claude-code-2.1.216@linux-x64.vsix` → `Anthropic.claude-code-2.1.217@linux-x64.vsix`，DEPLOY.md/ARCHITECTURE.md 同步版本号。镜像预计从 ~782MB → ~1.4GB（libreoffice ~280MB + pandoc ~80MB + tesseract ~50MB 是大头） |
| 2026-07-21 | **vendor 升级到 anthropic.claude-code@2.1.216**：跟市场推送同步；跟 2.1.215 相比改了 6 个文件（`extension.js` + `claude-code-settings.schema.json` + `package.json` 内容文本重排 + `resources/native-binary/claude` 二进制 + `webview/index.{js,css}`），功能上是普通 bug fix + UI 调整 + schema 更新，**未引入 `dist/browser/extension.js` web bundle**（web mode 激活限制保持不变）；同时清理 vendor：删掉旧 `anthropic.claude-code.vsix`(2.1.214) + `*.vsix.bak` 备份——避免 COPY 时多版同 ID 冲突。镜像 hash `ad0cc62a`（其他层全 cache 命中，仅 COPY .vsix 层重做） |
| 2026-07-20 | **精简 claude-code 镜像**：移除 `unified_db_mcp.ts`（MySQL+Oracle 只读 SQL MCP）+ `mysql2`/`oracledb`/`@modelcontextprotocol/sdk`/`zod`/`mcp-remote`/`tsx` npm 包 + Dockerfile 里的 `libaio1` apt 依赖 + portal/app.py 里的 Oracle Instant Client bind / `LD_LIBRARY_PATH` env；对应 .env.example / .env / docker-compose.yml / DEPLOY.md / 附录 B 全部同步清理。镜像从 793MB → 782MB（压缩）；运行时不再 bind `/opt/oracle/instantclient` ~200MB 内存/容器也省下。保留 claude-code npm vsix @ 2.1.215 + code-server 4.129.0 + Node 22.23.1 LTS（Jod）—— 都是当前最新。`git log` 这次大改：源码/配置/文档全部统一推进；存量用户 home 目录里的旧 mcp 残留不影响新容器启动（每容器重建后都用新 entrypoint）|
| 2026-07-31 | **门户访问密码 `PORTAL_PASSWORD`（§21）**：新增全局 `@app.before_request` 门禁 —— 未设该 env 时完全透明（旧部署零改动），设了则未登录访问 `/` 得到独立密码门页 `templates/gate.html`（401），未登录访问 `/api/*` 得到 401 JSON；豁免 `/static/`、`/api/portal/login|logout`、`/admin` + `/api/admin/*`（后者自带 ADMIN_PASSWORD 认证，不串两道密码）。session 键 `session["portal"]` 与 admin 分开，防爆破逻辑抽成 `login_cooldown_check()` / `login_record_failure()` 由两边共用但各用一个 IP 计数桶；`api_admin_login` 成功时顺带写 `session["portal"] = True`，避免其中的 `session.clear()` 把门户登录态一起清掉。前端 `app.js` 三处 fetch 加 `handleGate401()` → 401 时 reload 回密码门 |
| 2026-07-31 | **镜像三件套升级 + AI 一键禁用**：vendor 三件全部对齐 —— `Anthropic.claude-code-2.1.220@linux-x64.vsix` + `MS-CEINTL.vscode-language-pack-zh-hans-1.131.0.vsix` + `code-server_4.131.0_amd64.deb`（4.129.0 → 4.131.0，2.1.217 → 2.1.220，1.129.0 → 1.131.0；中文包 `engines.vscode ^1.131.0` 跟 code-server 自带 VS Code 1.131.0 严格匹配）。Dockerfile `ARG CODE_SERVER_VERSION` / DEPLOY.md / ARCHITECTURE.md 同步推进。**AI 一键禁用**：三层落点 —— entrypoint.sh 写 User/settings.json 时加 4 条 key；模板 `volumes/node/.local/share/code-server/User/settings.json` 写入相同 4 条（新建用户首次 start 自动落位）；portal `ensure_user_dir` + `sync_template_to_user` 末尾调 `_enforce_ai_disabled_settings(user_dir)` 合并写入（老用户 / 手动删 settings.json 也能兜住）。4 条 key：`chat.disableAIFeatures: true`（VS Code 内置 chat）+ `github.copilot.enable: {"*": false}`（Copilot 总开关）+ `github.copilot.chat.enabled: false`（Copilot Chat 子模块再挡一道）+ `inlineSuggest.enabled: false`（所有内联补全 ghost text）。用户后续要放开某条，只改 `_AI_DISABLED_SETTINGS` 字典即可 |
| 2026-08-01 | **修 admin 重建/删除面板 checkbox 5s 自动取消**：根因是 `renderRows()` 每 5s 重写整个 `innerHTML` —— 之前的 commit `3188b98` 只补了展开 class（`expanded` / `expanded-rebuild`），没补里面 checkbox 的 `checked` 属性，看起来就像"勾选 5 秒后自动取消"。修复：`renderRows()` 在 innerHTML 重写前用 `Map<uid, {opt: bool}>` 收集展开面板里所有 `input[type=checkbox][data-opt]` 的 checked 状态，重写后按 uid + data-opt 找回对应 checkbox 设回原值。只对展开行做记录（未展开的 checkbox 用户没碰过，重置无副作用） |
| 2026-08-01 | **注入 `customApiKeyResponses.approved` 跳过 Claude Code 风险确认窗**：portal `start_container()` 内 `ensure_user_dir()` 之后调 `_enforce_custom_api_key_responses(user_dir, api_key)`，把 apiKey 末段加进 `~/.claude.json` 的 `customApiKeyResponses.approved` 数组；Claude Code 启动时 `customApiKeyResponses?.approved?.includes(vZ(apiKey))` 命中就跳过"自定义 API key 风险确认"弹窗（vZ 函数来源：claude-code-linux-x64 binary 解 bundle JS）。实现对称于 `_enforce_ai_disabled_settings` —— 解析失败 WARN 不动、原子写回、幂等。rebuild_container 内部最终走 start_container，覆盖 reset_home / sync_template / 都不勾三种场景。**配套**：portal `.env` 新增 `DEFAULT_BASE_URL` / `DEFAULT_OPUS_MODEL` / `DEFAULT_SONNET_MODEL` / `DEFAULT_HAIKU_MODEL` 四个变量，login.html 4 个 input 用 `{{ default_* }}` 模板渲染做预填；用户 step-2 页面删掉"密码与 user_id 一致 / sha256(apiKey)[:16] 的前 16 位"段落 + PASSWORD 标签旁的"等于 user_id" hint（用户不该看到内部密码规则） |
| 2026-08-01 | **关闭 code-server telemetry 和 update check**：`entrypoint.sh` 的 `exec code-server "$@" $cert_args` 末尾硬编码 `--disable-telemetry --disable-update-check` —— CLI parser 后出现者覆盖前者，**保证即使 portal / CMD 传 `--disable-telemetry=false` 也覆盖不掉这两个 flag**。telemetry 默认上报 usage / 版本到 coder 总部，update check 默认每 6 小时 check GitHub release 并每周弹通知 —— 内网环境不该有任何外发；想解禁只能改 entrypoint.sh 重建 claude-code 镜像。两个 flag 在 code-server 4.131.0 `--help` 已确认存在并生效 |
| 2026-08-01 | **修 `customApiKeyResponses.approved` 尾段位数 16→20，对齐 Claude Code `vZ(-20)`**：上一条注入 approved 用了 `_APPROVED_TAIL_LEN = 16`，错了。Claude Code binary 里解出来 `function vZ(e){return e.trim().slice(-20)}` —— 取末 20 位；用 `customApiKeyResponses?.approved?.includes(vZ(apiKey))` 严格匹配。写 16 位 includes() 永远 false → 弹窗照弹。同时把 Python 端改成 `api_key.strip()[-20:]`（对齐 JS 的 `trim().slice(-20)`），不 strip 直接切的话 `vZ` 算出来 20 字符的子串虽包含在某些脏 entry 里但 includes 仍失败。来源：现有 `volumes/users/<uid>/.claude.json` 的 approved 数组里 Claude Code 自己写的 21 字符 `CvrRx9qXWCHbE295M-mY`（其末 20 位就是 vZ(-20) 输出）—— 印证 vZ(-20) 行为 |
