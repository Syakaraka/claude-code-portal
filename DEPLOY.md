# Claude Code Portal — 部署 / 迁移指南

> 目标：把项目从一台机器完整搬到另一台机器（也可以是同台机器重装），
> 从零构建镜像、配置变量、起服务，所有用户凭据沿用。

---

## 0. 这个项目是什么（一句话）

一个多用户 LLM 编程沙箱门户。用户在浏览器填 API Key + 三个模型名，portal 给每个人
拉起一个隔离的 Code Server 容器，跑 Claude Code 扩展，配内部 CA 签的 HTTPS 证书。

架构细节看 `ARCHITECTURE.md`。这份文档只讲"怎么搬"。

---

## 1. 必须依赖当前机器的东西（环境耦合盘点）

搬之前先看下面这些点；每项都给出了"在目标机器上怎么对齐"的做法。

### 1.1 宿主用户 uid（root / 普通用户都行）

`portal/app.py` 把所有用户目录 chown 到**数值** uid/gid `1000:1000`
（对应 `claude-code:local` 镜像里 `/etc/passwd` 的 `node` 用户）。
Linux inode 上的 uid/gid 是数字，**不依赖用户名**：

- 容器内 `node` (uid 1000) 看到的 inode uid/gid 也是 1000/1000 → 读写正常
- host 上 `chown 1000:1000` 只设数字，不要求 host 真的有 `uid=1000` 的本地账号
- 所以 **host 用户是 root / 任意非 1000 的普通用户都能跑**，不需要对齐到 1000

#### 唯一会受 host 用户影响的事

| host 用户 | `ls -l volumes/users/<uid>/` 显示 | 其他 |
|-----------|------------------------------------|------|
| uid 1000 用户 | `thomas thomas ...` | 默认开发机场景，最直观 |
| 其他 uid | `1000 1000 ...` | 纯数字显示，无功能影响 |
| **root** (uid 0) | `1000 1000 ...` | 同上 |

#### Root 部署（云服务器常见场景）

如果是 root，**直接部署即可**，比文档 §2 的 Step 2 还简单：

```bash
# 不用 usermod（root 不能改 uid，也不需要）
# 不用 usermod -aG docker（root 默认能跑 docker）
# 不用 sudo（任何命令前都不需要）

apt update && apt install -y curl git
curl -fsSL https://get.docker.com | sh

git clone git@github.com:Syakaraka/claude-code-portal.git /root/claude-portal
cd /root/claude-portal
cp .env.example .env
# .env 里 HOST_PROJECT_DIR=/root/claude-portal
# 后续步骤按 §2 Step 4-7 走
```

#### 非 1000 的普通用户（root 之外的最常见情况）

```bash
sudo usermod -aG docker $USER    # 让自己能免 sudo 跑 docker
# 注销重登让 group 生效
# 不用改 uid；目录属主是数字 1000，不影响功能
```

### 1.2 Linux 宿主机（Debian/Ubuntu 系）

`vendor/code-server_4.129.0_amd64.deb` 是 amd64 Debian 包，`Dockerfile` 用 `apt-get
install` 装 `git` / `openssl` / `ca-certificates`。这些都假设 **Linux + apt**。

新机器必须是 **Debian/Ubuntu**（或同源发行版如 Linux Mint、Pop!_OS）。
**macOS / 原生 Windows 不行**——WSL2 算 Linux，可以。

WSL2 已知坑：

- Docker Desktop for Windows 通过 WSL2 integration 跑 daemon，路径必须是
  `/mnt/c/...` 或 `/home/...`，不要放在 `/mnt/c/Users/...`（权限很乱）。
- Docker socket 默认在 `\\.\pipe\docker_engine`，WSL2 内能通过 `/var/run/docker.sock`
  访问（Docker Desktop 会自动 symlink）。

### 1.3 Docker + Compose

新机器必须装好：

- Docker Engine ≥ 20.10（推荐 24.x）
- Docker Compose v2（`docker compose`，不是 `docker-compose`）

```bash
docker --version          # 验证 Docker
docker compose version    # 验证 Compose v2
```

### 1.4 当前机器路径（.env 控制）

唯一硬编码到代码里的"机器特定值"是 `HOST_PROJECT_DIR`，默认值
`/home/thomas/workspace/code/claude_web_images`（在 `app.py` 和 `.env.example` 里）。
**新机器一定要在 .env 里改成自己的路径**，否则 docker.run() 的 bind source
会找不到。

### 1.6 内部 CA 证书（迁移决策点）

Portal 每次启动如果 `volumes/certs/` 没有 CA 就生成一个全新的。
**用户浏览器信任的是那张 CA**——迁移时会决定：

| 决策 | 做法 | 后果 |
|------|------|------|
| **沿用旧 CA** | 把 `volumes/certs/ca.crt` + `ca.key` + `ca.srl` 整个目录带过来 | 现有用户浏览器**不用重新装 CA**，但 admin 面板里**显示不出历史 displayName**（meta.json 在 volumes/users/ 里，会单独带） |
| **换新 CA** | 不带 `volumes/certs/` | 所有用户首次访问要重新下载安装 CA |

> **推荐沿用旧 CA**——CA 本身不带敏感数据（只是信任锚点），换 CA 麻烦无收益。

### 1.7 用户数据（迁移决策点）

`volumes/users/<uid>/` 存了用户的：VS Code settings、扩展、对话缓存、`/home/node/scratch`、
`.portal_meta.json`（displayName）。

用户 ID = `sha256(apiKey)[:16]`——不依赖机器。要让某个用户**沿用他的 workspace
和显示名**，就把他对应的 `<uid>` 子目录整个带过来。

不带也没事：用户用同一个 apiKey 重新登录 → user_id 算出来还是同一个 → portal 自动
从模板重建一个新 home（设置/扩展都是默认），displayName 丢失（除非从 .portal_meta.json
单独抠出来塞回去）。

### 1.8 哪些文件是"这台机器生成的、不要入库"

| 路径 | 是否入库 | 说明 |
|------|----------|------|
| `.env` | ❌ `.gitignore` | 含管理员密码 / FLASK_SECRET 等敏感 |
| `vendor/*.vsix` | ❌ `.gitignore` | 扩展包太大，每个部署自己 vendor |
| `vendor/code-server_*.deb` | ❌ `.gitignore` | 同上 |
| `volumes/` | ❌ `.gitignore` + `.dockerignore` | 全是运行时数据 |
| `portal/__pycache__/` | ❌ | Python 编译产物 |
| `console.log` | ❌ | 调试日志 |
| `Dockerfile` / `entrypoint.sh` / `docker-compose.yml` / `portal/` / `.env.example` / `ARCHITECTURE.md` / `DEPLOY.md` | ✅ | 这就是项目本体 |

---

## 2. 迁移步骤（从一台机器搬到另一台）

### Step 1：在旧机器打包要带的文件

需要带（推荐打成 tar）：

```bash
# 项目源（git clone 也行，但 vendor/ 不入库所以必须单独带）
cd /home/thomas/workspace/code/claude_web_images

# 1) 项目源
tar czf claude-portal-src.tar.gz \
    --exclude=./.git \
    --exclude=./volumes \
    --exclude=./portal/__pycache__ \
    --exclude=./.env \
    --exclude=./console.log \
    --exclude='./vendor/*.bak' \
    .

# 2) vendor/（扩展 + code-server deb，必须单独带）
tar czf vendor.tar.gz vendor/

# 3) 可选：CA（沿用旧 CA 就带；不带则重生成）
tar czf ca-bundle.tar.gz volumes/certs/

# 4) 可选：用户数据
tar czf user-data.tar.gz volumes/users/
```

**vendor/ 文件的官方下载地址**（从 GitHub/Open VSX 拉）：

```bash
# 在新机器上 setup vendor/ 的标准流程（如果没带 tar 包）
mkdir -p vendor
cd vendor

# 1) Claude Code 扩展 (Linux x64, v2.1.217)
curl -L -o Anthropic.claude-code-2.1.217@linux-x64.vsix \
  https://openvsx.eclipsecontent.org/Anthropic/claude-code/linux-x64/2.1.217/Anthropic.claude-code-2.1.217@linux-x64.vsix

# 2) VS Code 中文语言包 (v1.129.0，跟 code-server 内核版本对齐)
curl -L -o MS-CEINTL.vscode-language-pack-zh-hans-1.129.0.vsix \
  https://openvsx.eclipsecontent.org/MS-CEINTL/vscode-language-pack-zh-hans/1.129.0/MS-CEINTL.vscode-language-pack-zh-hans-1.129.0.vsix

# 3) code-server v4.129.0 (amd64 Debian 包)
curl -L -o code-server_4.129.0_amd64.deb \
  https://github.com/coder/code-server/releases/download/v4.129.0/code-server_4.129.0_amd64.deb

cd ..
# 检查文件大小
ls -lh vendor/
# Anthropic...vsix      ~83M
# MS-CEINTL...vsix       ~620K
# code-server_...deb     ~195M
```

> **版本对齐是硬要求**：扩展 `engines.vscode` 字段必须 ≥ code-server 内的 VS Code 版本，
> 否则扩展装上但 UI 不生效；中文包 v1.128.x 配 code-server 4.129.0 会出现中文 UI 时有时无。
> Claude Code 扩展是 Linux x64 专用 —— Windows / macOS 客户端用 Open VSX 找对应平台的版本。

> **deb 必须用 amd64**：即使 host 是 arm64 也要走 qemu 模拟，没问题（构建期性能不敏感）。
> ARM64 host 长期跑 Claude Code 容器建议改成 `code-server_4.129.0_arm64.deb`。

### Step 2：在新机器准备环境

**如果是 root**（云服务器常见）：

```bash
apt update && apt install -y curl git
curl -fsSL https://get.docker.com | sh
# 不用加 docker group（root 默认能跑 docker）
# 不用改 uid

# 验证
docker --version
docker compose version
docker ps          # 不报权限错即可
```

**如果是普通用户**（假设用户名是 `deployer`）：

```bash
# 装 docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker deployer  # 让自己能免 sudo 跑 docker
# 注销重登让 group 生效

# 验证
id
docker --version
docker compose version
docker ps          # 不报权限错即可
# uid 可以是任意值，不必是 1000（详见 §1.1）
```

### Step 3：放置项目到目标路径

```bash
# 新机器上你想要的部署路径（root 就放 /root，普通用户就放 /home/$USER）
mkdir -p /root/claude-portal      # root
# 或 mkdir -p /home/deployer/claude-portal  # 普通用户
cd /root/claude-portal             # 或对应路径

# 解包
tar xzf /path/to/claude-portal-src.tar.gz
tar xzf /path/to/vendor.tar.gz
# 可选
tar xzf /path/to/ca-bundle.tar.gz
tar xzf /path/to/user-data.tar.gz

# 修正属主（仅当从其他机器迁移过来且 tar 里带了原始 uid 时需要）
# root 解包：通常文件已经是 root 拥有，可跳过
# 普通用户解包：sudo chown -R $USER:$USER /home/deployer/claude-portal
# （后续 portal/app.py 会把 users/<uid>/ 内部 chown 到数值 1000:1000，跟宿主机谁拥有项目目录无关）
```

### Step 4：写 .env

```bash
cp .env.example .env
nano .env    # 或 vim
```

**必须改的几项**（其他保持默认即可）：

```bash
# 改成新机器的实际路径（其它路径通过 ${HOST_PROJECT_DIR} 自动派生）
HOST_PROJECT_DIR=/home/deployer/claude-portal

# 默认 9900 / 9901-9999 一般不用改；如果被防火墙占了就改
PORTAL_HOST_PORT=9900
CLAUDE_PORT_MIN=9901
CLAUDE_PORT_MAX=9999

# 管理员密码（新机器一定要换，不要沿用旧的）
ADMIN_PASSWORD=改成强密码

# 门户访问密码（可选）：留空 = 免登录，谁能访问 9900 谁就能起容器；
# 填了 = 打开首页先要输这个密码，未登录时 /api/* 也一律 401。
# 内网可以不填；挂到公网 / 半开放网络上建议填。
PORTAL_PASSWORD=

# Flask session 密钥（强烈建议显式设；不设 portal 会从 HOST_PROJECT_DIR 派生，
# 迁移后旧 cookie 会失效——但一般没人保留旧 cookie，所以问题不大）
FLASK_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
```

### Step 5：构建两个镜像

```bash
cd /home/deployer/claude-portal

# 5.1 先构建 claude-code:local（包含 code-server + 扩展）
docker build . -t claude-code:local
#   ↑ 首次构建约 3–5 分钟（拉 node:22 基础镜像 + apt install + npm install）
#   ↑ 必须在项目根（不是 portal 子目录），Dockerfile 在根目录

# 5.2 再构建 portal
docker compose build
```

### Step 6：启动

```bash
docker compose up -d
docker logs --tail 20 claude-portal
# 应看到 "Listening at: http://0.0.0.0:9900"
```

### Step 7：验证（浏览器）

1. 浏览器访问 `http://新机器IP:9900/`（不是 `https://`，门户本身是 HTTP，仅 Code Server 用 HTTPS）
2. Step 1 填一个测试 API key + 三个模型名（可以用任意值测试）
3. Step 2 看到 URL + 密码
4. 点"打开" → 浏览器弹 SSL 警告 → 信任证书
   - 走 `http://新机器IP:9900/install-cert` 下载 CA 装到 Windows 信任库（步骤按 banner 提示）
5. 浏览器信任后，正常进 Code Server + Claude Code 扩展

---

## 3. 迁移特定用户数据（可选）

如果你沿用了旧的 user_data.tar.gz，portal 会**自动**根据 apiKey 算出 user_id，
并能读取到那个用户的 home（settings、扩展、scratch、displayName）。

具体路径：

```bash
# 旧机器上的某个用户
ls volumes/users/68ee7d6f3276c4eb/  # 用户的 home
# 含 .claude/  .config/  scratch/  .portal_meta.json  ...
```

```bash
# 解到新机器的同一个相对位置（卷路径由 HOST_PROJECT_DIR 决定）
cd /home/deployer/claude-portal
tar xzf user-data.tar.gz
# 解出 volumes/users/<uid>/... → 新机器的对应位置
# 权限修正
sudo chown -R 1000:1000 volumes/users/
```

然后该用户用同一个 apiKey 登录 → portal 算出同样的 user_id → 看到旧 home + 旧 displayName。

> ⚠️ 用户 cert（`volumes/certs/code-server-certs/<uid>/`）是首次 start 时重新签的，
> 不需要带过来。

---

## 4. 日常运维命令速查

```bash
# 看 portal 日志
docker logs -f claude-portal

# 重启 portal（.env 改了之后必须重启）
docker compose restart portal
# 或完全重建（配置 + 镜像都改了）
docker compose up -d --build portal

# 看 claude 容器
docker ps --filter "label=managed-by=claude-portal"

# 强制清理某个用户的容器（admin 页面也能做）
docker rm -f claude-<uid>

# 清理所有 claude 容器
docker ps -aq --filter "label=managed-by=claude-portal" | xargs -r docker rm -f

# 完全卸载（保留 volumes/ 在）
docker compose down

# 完全卸载（连 volumes 都删）
docker compose down -v
```

---

## 5. 故障排查

| 症状 | 排查 |
|------|------|
| `docker compose up -d` 报 `bind source path does not exist` | `.env` 里 `HOST_PROJECT_DIR` 写错了；或目录权限不是 1000:1000 |
| portal 起来后浏览器 502 | `docker logs claude-portal` 看 traceback；多半是 .env 缺关键变量或 FLASK_SECRET 不一致 |
| Claude 容器起不来 | `docker logs claude-<uid>` 看 entrypoint.sh 报错；常见是 vendor/*.vsix 不全（重新 vendor） |
| 用户浏览器一直 SSL 警告 | 旧 CA 没沿用 → 重新 `curl http://server:9900/install-cert` 下载新 CA 安装 |
| 用户能看到自己容器但管理员看不到 | admin `containers.list` 抛错；新版已经用 `c.attrs["Config"]["Image"]` 避免触发 image inspect，应已修；如还报错看 `docker logs claude-portal` |
| 重启 portal 后 admin session 立即过期 | `FLASK_SECRET` 没设；设上 `FLASK_SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')` |
| claude 容器里 node 用户写不进文件 | 宿主机用户 uid 不是 1000；见 §1.1 |
| `permission denied` on `/var/run/docker.sock` | 当前用户不在 `docker` group；`sudo usermod -aG docker $USER` 然后重登 |

---

## 6. 部署检查清单

新机器起来前对照打勾：

- [ ] Linux 发行版是 Debian/Ubuntu 系
- [ ] Docker Engine ≥ 20.10 + Compose v2 已装
- [ ] 当前用户 `id` 输出 uid=1000
- [ ] 当前用户在 `docker` group
- [ ] 项目解压到 `HOST_PROJECT_DIR` 指向的位置
- [ ] `vendor/` 已带过来（`.vsix` + `.deb`）
- [ ] `.env` 已 cp 自 `.env.example` 并改了 `HOST_PROJECT_DIR` + `ADMIN_PASSWORD`
- [ ] 可选：`volumes/certs/` 已沿用旧 CA（保留用户浏览器信任）
- [ ] 可选：`volumes/users/` 已沿用旧用户数据
- [ ] `sudo chown -R 1000:1000 $HOST_PROJECT_DIR` 跑过
- [ ] `docker build . -t claude-code:local` 成功
- [ ] `docker compose build` 成功
- [ ] `docker compose up -d` 成功，portal log 无 error
- [ ] 浏览器访问 `http://新机器IP:9900/` 能看到门户
- [ ] 浏览器访问 `http://新机器IP:9900/install-cert` 能下载 CA
- [ ] `/admin` 能用 `ADMIN_PASSWORD` 登录
- [ ] 若设了 `PORTAL_PASSWORD`：首页先出密码门，输对后才进门户

---

## 7. 后续扩展到多机

如果将来要部署到多台机器组成集群，几个注意点：

- portal 容器之间**不共享状态**——`.portal_users.json` 和 CA 各机器独立。
  这意味着同一用户在不同机器上会得到不同的 user_id（除非共用一个外部 store）。
  本部署定位是单机工具，不设计集群。
- 跨机器迁移用户 → 直接 `tar` 那一整个 `<uid>` 目录就行（同 §3）。
- 想做"漂移"用户体验（用户在哪台机器登都看到一样的 home）→ 把 `volumes/` 挂
  NFS / CephFS，所有 portal 容器共享同一份。这是另一个工程量级。
