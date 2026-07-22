"""
Claude Code Portal - 登录门户 + per-user 容器编排

详见 ARCHITECTURE.md
"""
import os
import re
import json
import time
import shutil
import secrets
import hashlib
import logging
import ipaddress
import subprocess
from pathlib import Path

from flask import Flask, request, jsonify, render_template, make_response, session
from datetime import timedelta
import docker
from docker.errors import NotFound, APIError

app = Flask(__name__)
# 默认 8 小时 session —— 改 .env 的 ADMIN_SESSION_MAX_AGE 调
app.permanent_session_lifetime = timedelta(
    seconds=int(os.environ.get("ADMIN_SESSION_MAX_AGE", 8 * 3600))
)

# ---------- 日志中过滤敏感信息（API Key） ----------

class SecretFilter(logging.Filter):
    def filter(self, record):
        s = str(record.msg)
        record.msg = re.sub(r"sk-ant-[A-Za-z0-9_\-]+", "[REDACTED]", s)
        return True

app.logger.addFilter(SecretFilter())
logging.getLogger().addFilter(SecretFilter())

# ---------- 常量（全部从环境变量读，默认值兼容旧部署） ----------
#
# 两种视角的路径并存：
#
#   - PORTAL 内视角（读取 volumes 内容：shutil.copytree / ls / chmod 等）：
#       通过 ./volumes:/volumes bind mount，portal 看到 host 的 volumes/ 在 /volumes
#
#   - HOST 视角（传给 docker.run() 作为 bind source）：
#       Docker daemon 解析时按 host 真实绝对路径找，portal 容器内的 /volumes/...
#       路径不能直接给 docker.run()，否则 daemon 会按 host 上不存在的位置处理。
#
# 所有路径 / 端口 / 镜像名都可由环境变量覆盖，详见 .env.example 与 ARCHITECTURE.md §15。

def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v else default


# === Portal HTTP 端口（compose ports 映射 + portal/Dockerfile gunicorn bind） ===
PORTAL_HOST_PORT      = _env_int("PORTAL_HOST_PORT", 9900)
PORTAL_CONTAINER_PORT = _env_int("PORTAL_CONTAINER_PORT", 9900)

# === Claude 容器端口范围（host 视角，从 PORT_BASE 改为 MIN/MAX） ===
CLAUDE_PORT_MIN = _env_int("CLAUDE_PORT_MIN", 9901)
CLAUDE_PORT_MAX = _env_int("CLAUDE_PORT_MAX", 9999)

# === Claude 镜像 ===
CLAUDE_IMAGE_NAME = _env("CLAUDE_IMAGE_NAME", "claude-code:local")

# === Portal / claude 容器标签 ===
PORTAL_LABEL = _env("PORTAL_LABEL", "managed-by")

# === Host 路径（传给 docker.run() 当 bind source） ===
HOST_PROJECT_DIR = _env(
    "HOST_PROJECT_DIR",
    "/home/thomas/workspace/code/claude_web_images",
)
HOST_VOLUMES_DIR = _env(
    "HOST_VOLUMES_DIR",
    os.path.join(HOST_PROJECT_DIR, "volumes"),
)
HOST_USERS_DIR     = _env("HOST_USERS_DIR",     os.path.join(HOST_VOLUMES_DIR, "users"))
HOST_CODE_DIR      = _env("HOST_CODE_DIR",      os.path.join(HOST_VOLUMES_DIR, "code"))
HOST_TEMPLATE_DIR  = _env("HOST_TEMPLATE_DIR",  os.path.join(HOST_VOLUMES_DIR, "node"))
HOST_CERTS_DIR     = _env("HOST_CERTS_DIR",     os.path.join(HOST_VOLUMES_DIR, "certs"))

# === Portal 容器内 mount point（./volumes → /volumes 的右侧） ===
PORTAL_VOLUMES_MOUNT = _env("PORTAL_VOLUMES_MOUNT", "/volumes")

# portal 容器内视角（通过 bind mount 访问 host 的 volumes/）
CONTAINER_VOLUMES = PORTAL_VOLUMES_MOUNT
USER_DATA_BASE = os.path.join(CONTAINER_VOLUMES, "users")
WORKSPACE_PATH  = os.path.join(CONTAINER_VOLUMES, "code")
USER_TEMPLATE   = os.path.join(CONTAINER_VOLUMES, "node")

# host 视角（给 docker.run() 当 bind source 用）
HOST_USER_DATA_BASE = HOST_USERS_DIR
HOST_WORKSPACE_PATH = HOST_CODE_DIR

# === Claude 容器内 bind target（必须与镜像内 WORKDIR / entrypoint 路径一致）
#     改这些值需要重建 claude-code 镜像；当前默认值匹配 v4.129.0 镜像 ===
CLAUDE_WORKSPACE_BIND = _env("CLAUDE_WORKSPACE_BIND", "/workspace")
CLAUDE_HOME_BIND      = _env("CLAUDE_HOME_BIND",      "/home/node")
CLAUDE_SCRATCH_BIND   = _env("CLAUDE_SCRATCH_BIND",   "/home/node/scratch")
CLAUDE_CERT_DIR_BIND  = _env("CLAUDE_CERT_DIR_BIND",  "/etc/code-server")
CLAUDE_INTERNAL_PORT  = _env_int("CLAUDE_INTERNAL_PORT", 8080)

# === 文件名 / 子目录名（一般不需要改；提供给极端定制场景） ===
USERS_CACHE_FILENAME = _env("USERS_CACHE_FILENAME", ".portal_users.json")
CERT_DIR_NAME        = _env("CERT_DIR_NAME",        "code-server-certs")
CA_CERT_FILENAME     = _env("CA_CERT_FILENAME",     "ca.crt")
CA_KEY_FILENAME      = _env("CA_KEY_FILENAME",      "ca.key")
CLAUDE_CERT_FILENAME = _env("CLAUDE_CERT_FILENAME", "cert.pem")
CLAUDE_KEY_FILENAME  = _env("CLAUDE_KEY_FILENAME",  "key.pem")
SCRATCH_DIR_NAME     = _env("SCRATCH_DIR_NAME",     "scratch")
WORKSPACE_FILE_NAME  = _env("WORKSPACE_FILE_NAME",  "workspace.code-workspace")
SAN_CONFIG_NAME      = _env("SAN_CONFIG_NAME",      "san.cnf")
CSR_FILENAME         = _env("CSR_FILENAME",         "cert.csr")

# === 活跃容器上限 ===
# 0 = 不限（默认）；非 0 = 拒绝超过上限的 start / rebuild 请求。
# "活跃"=docker 报的 status=="running"，不计入已 exited 的容器。
# 防止某用户狂拉 / 单机内存爆。
MAX_ACTIVE_CONTAINERS = _env_int("MAX_ACTIVE_CONTAINERS", 0)

# === Per-user 固定端口（.port 文件）===
# 用户首次启动容器时被分配一个 host port，写到 volumes/users/<uid>/.port；
# 后续重建 / 重启 portal / 重建容器都优先复用这个端口。
# 失败降级：端口被其他用户占用 / 落范围外 / 文件损坏 → 重新摇号并覆写。
# wipe_home 会连带删除 .port（语义：用户目录重置 = 端口也重置）；
# 如果需要保留，手动 cp volumes/users/<uid>/.port 出来再放回去。
PORT_FILE_NAME = ".port"

# === Flask session 签名密钥 ===
# secret_key 必须跨 worker 一致 —— 否则 session cookie 在某个 worker 签出来，
# 落到另一个 worker 就解不出来 → 401（用户症状：登录后随机 401，5 秒内必现）。
# gunicorn -w N 默认 preload_app=False，每个 worker 各自 import app 模块，
# 如果 secret_key 来自 secrets.token_hex(32) 之类的"启动时随机"源，
# 每个 worker 会得到不同 key。
# 修法：
#   - 优先用 FLASK_SECRET env（运维明确指定，最稳）
#   - 兜底用 HOST_PROJECT_DIR 派生（部署级稳定，跨 worker 一致）
_app_secret = os.environ.get("FLASK_SECRET")
if not _app_secret:
    _app_secret = hashlib.sha256(
        f"claude-code-portal|{HOST_PROJECT_DIR}".encode("utf-8")
    ).hexdigest()
    print(
        "[portal] WARNING: FLASK_SECRET not set, deriving from HOST_PROJECT_DIR. "
        "For production set FLASK_SECRET in .env explicitly.",
        flush=True,
    )
app.secret_key = _app_secret

# === Admin 认证（独立于 per-user 密码） ===
# 用独立的 ADMIN_PASSWORD env var 验证管理员（不要复用 user_id 派生方案）
# 不设或为空 → 禁用 admin 路由（/admin 返回 404）
ADMIN_PASSWORD = _env("ADMIN_PASSWORD", "")

# admin 登录失败计数（按客户端 IP，60 秒冷却）—— 简单防爆破
# 注意：gunicorn 多 worker 下每个 worker 各自一份计数器，不是全局严格限流；
# 5 次 / worker 足够拖慢手动尝试，但不是真分布式防爆破（这是内部工具可接受）
_admin_login_failures: dict[str, tuple[int, float]] = {}
_ADMIN_MAX_ATTEMPTS = 5
_ADMIN_LOCKOUT_SEC = 60.0

# === 派生路径（容器内视角 + host 视角的 cert / 用户数据 / CA） ===
USERS_CACHE = os.path.join(USER_DATA_BASE, USERS_CACHE_FILENAME)
CA_DIR       = os.path.join(CONTAINER_VOLUMES, "certs")  # 容器内视角
HOST_CA_DIR  = HOST_CERTS_DIR                              # host 视角

# ---------- Docker 客户端 ----------

try:
    client = docker.from_env()
except Exception as e:
    app.logger.error(f"Docker 连接失败: {e}")
    client = None


# ---------- 用户缓存（user_id -> {port, password, container_id}） ----------

def load_users_cache():
    try:
        with open(USERS_CACHE) as f:
            raw = json.load(f)
            return {k: v for k, v in raw.items() if is_valid_user_id(k)}
    except FileNotFoundError:
        return {}
    except Exception as e:
        app.logger.error(f"读取 user cache 失败: {e}")
        return {}


def save_users_cache(users):
    try:
        Path(USERS_CACHE).parent.mkdir(parents=True, exist_ok=True)
        with open(USERS_CACHE, "w") as f:
            json.dump(users, f)
    except Exception as e:
        app.logger.error(f"写入 user cache 失败: {e}")


# ---------- helpers ----------

def hash_api_key(api_key: str) -> str:
    """sha256 前 16 hex 字符，作为 user_id。"""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def is_valid_user_id(uid: str) -> bool:
    """严格校验，防止路径穿越。"""
    if not uid or not isinstance(uid, str):
        return False
    return bool(re.fullmatch(r"[a-f0-9]{16}", uid))


def _chmod_user_dir(path: str) -> None:
    """递归 chmod 用户目录：所有目录 0777，所有文件 0666。
    容器内 claude 进程以 node 用户（uid 1000）跑，需要能读/写所有内部文件。
    shutil.copytree 复制的文件保留模板原权限（很多是 0600/0700 root-owned），
    外层目录 chmod 不够，必须递归进所有子目录和文件。
    """
    for root, dirs, files in os.walk(path):
        try:
            os.chmod(root, 0o777)
        except Exception:
            pass
        for f in files:
            try:
                os.chmod(os.path.join(root, f), 0o666)
            except Exception:
                pass


# ---------- per-user metadata (displayName) ----------
# 纯辅助标识 —— 不参与鉴权、不影响隔离。仅供 admin 面板识别"哪个 user_id 是谁"。
# 存在 volumes/users/<uid>/.portal_meta.json，文件丢失 / 不存在视为未设置。

_USER_META_FILENAME = ".portal_meta.json"
_DISPLAY_NAME_MAX_LEN = 40
# 允许：中英文 / 数字 / 空格 / () / - / _ / . / 中文标点（，。：；！？、《》【】「」""''）
_DISPLAY_NAME_RE = re.compile(
    r"^[A-Za-z0-9一-鿿ぁ-ヿ々\-\._()（）\[\]【】「」\s，。：；！？、《》""'']+$"
)


def _sanitize_display_name(raw) -> str:
    """清洗 displayName：strip control chars，截断长度，剔除非法字符。
    空串合法（表示未命名）。返回值永远可通过 .strip() == "" 表示"未设置"。
    """
    if not isinstance(raw, str):
        return ""
    s = raw.strip()
    # 剥掉控制字符（保留换行/制表符之外的所有 \x00-\x1f 和 \x7f）
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", s)
    if len(s) > _DISPLAY_NAME_MAX_LEN:
        s = s[:_DISPLAY_NAME_MAX_LEN]
    # 过滤非法字符：把所有不在白名单里的字符替换为空
    s = "".join(c for c in s if _DISPLAY_NAME_RE.match(c) is not None or c.isspace())
    return s.strip()


def _user_meta_path(user_id: str) -> str:
    return os.path.join(USER_DATA_BASE, user_id, _USER_META_FILENAME)


def _read_user_meta(user_id: str) -> dict:
    """读取 .portal_meta.json，失败 / 不存在返回空 dict。"""
    try:
        with open(_user_meta_path(user_id)) as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    except Exception as e:
        app.logger.warning(f"read user meta failed for {user_id[:8]}: {e}")
    return {}


def _write_user_meta(user_id: str, meta: dict) -> None:
    """写 .portal_meta.json 到用户 home 目录。
    写完 _chmod_user_dir 在下一次重建 / 启动时再跑，但这里直接 chmod 0666 保险。
    """
    try:
        path = _user_meta_path(user_id)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        try:
            os.chmod(path, 0o666)
        except Exception:
            pass
    except Exception as e:
        app.logger.error(f"write user meta failed for {user_id[:8]}: {e}")


def _set_user_display_name(user_id: str, name: str) -> None:
    """合并写 displayName 到 meta（不覆盖其他未来字段）。"""
    meta = _read_user_meta(user_id)
    meta["display_name"] = _sanitize_display_name(name)
    meta["display_name_updated_at"] = int(time.time())
    _write_user_meta(user_id, meta)


def ensure_user_dir(user_id: str, wipe: bool = False) -> str:
    """确保用户 home 目录存在。首次访问时从 USER_TEMPLATE 复制初始内容。

    wipe=True → 先删整个目录再从模板重建（用于 rebuild 时的"重置用户目录"选项）。

    返回 portal 容器内的 user_dir 路径（用于绑定到后续代码判断）。
    实际传给 docker.run() 的 host 绝对路径用 host_user_dir_for(user_id) 拼出。
    """
    path = os.path.join(USER_DATA_BASE, user_id)  # 容器内视角
    if wipe and os.path.exists(path):
        # 用 onerror 回调暴露错误，而不是 swallow —— 之前 ignore_errors=True 会
        # 把"目录里有正在写的文件"等失败静默，导致用户以为 reset 没生效。
        def _on_rm_error(fn, p, exc_info):
            app.logger.error(f"ensure_user_dir wipe: rmtree failed at {p} ({fn}): {exc_info[1]}")
        try:
            shutil.rmtree(path, onerror=_on_rm_error)
        except Exception as e:
            app.logger.error(f"ensure_user_dir wipe: rmtree raised for {user_id}: {e}")
            raise
        # rmtree 后目录应不存在；如果还在（罕见，例如有 bind mount 残留），
        # 显式再删一次剩余内容，避免残留文件被误归为"已重置"
        if os.path.exists(path):
            remaining = os.listdir(path)
            app.logger.warning(
                f"ensure_user_dir: after rmtree, {path} still has {remaining}; "
                f"trying delete-then-recreate"
            )
            for entry in remaining:
                try:
                    fp = os.path.join(path, entry)
                    if os.path.isdir(fp) and not os.path.islink(fp):
                        shutil.rmtree(fp, onerror=_on_rm_error)
                    else:
                        os.unlink(fp)
                except Exception as e:
                    app.logger.error(f"ensure_user_dir: failed to remove leftover {entry}: {e}")
        app.logger.info(f"wiped user dir: {user_id}")
    if not os.path.exists(path):
        # 首次创建（首次启动 or 刚 wipe 完）：从模板复制
        if os.path.isdir(USER_TEMPLATE):
            try:
                shutil.copytree(USER_TEMPLATE, path)
                app.logger.info(f"initialized user dir from template: {user_id}")
            except Exception as e:
                app.logger.error(f"ensure_user_dir: copytree from template failed for {user_id}: {e}")
                # 降级：建空目录，不阻断
                os.makedirs(path, exist_ok=True)
        else:
            # 模板目录缺失则降级为空目录（不阻断启动）
            os.makedirs(path, exist_ok=True)
            app.logger.warning(f"USER_TEMPLATE missing ({USER_TEMPLATE}), created empty dir for {user_id}")
    # 递归 chmod：所有目录 0777，所有文件 0666
    # 不只改外层 —— shutil.copytree 保留模板原权限（.claude.json 是 0600 root-owned）
    _chmod_user_dir(path)
    # chown -R 1000:1000：让 claude 容器内的 node 用户完全 ownership
    # 之前不 chown 时，portal 以 root 写的 cert 文件（如 code-server-certs/*）保持 root 拥有，
    # node 用户没法 chmod/rm，容器内 /home/node/scratch 也建不了 sticky bit。
    # thomas 在 host 端仍有父目录 0777 写权限，可以 rm 任何子文件。
    _chown_recursive(path, uid=1000, gid=1000)
    return path


def _chown_recursive(path: str, uid: int, gid: int) -> None:
    """递归 chown 到指定 uid/gid。chown 失败仅记录，不阻断（rootful container 才有权限）。"""
    for root, dirs, files in os.walk(path):
        try:
            os.chown(root, uid, gid)
        except PermissionError:
            pass
        for f in files:
            try:
                os.chown(os.path.join(root, f), uid, gid)
            except PermissionError:
                pass


def host_user_dir_for(user_id: str) -> str:
    """user_id → host 视角绝对路径（给 docker.run() 当 bind source）。"""
    return os.path.join(HOST_USER_DATA_BASE, user_id)


def count_active_claude_containers() -> tuple:
    """统计 portal 编排的容器数。

    返回 (active, total)：
      active — status=="running" 的数量（受 MAX_ACTIVE_CONTAINERS 约束）
      total  — 含 exited/created/restarting 的全部 portal 容器（admin 展示用）
    """
    if not client:
        return 0, 0
    try:
        containers = client.containers.list(
            all=True, filters={"label": f"{PORTAL_LABEL}=claude-portal"}
        )
    except Exception as e:
        app.logger.error(f"count_active_claude_containers: list failed: {e}")
        return 0, 0
    active = sum(1 for c in containers if c.status == "running")
    return active, len(containers)


def get_used_ports() -> set:
    if not client:
        return set()
    used = set()
    try:
        for c in client.containers.list():
            ports = c.attrs.get("NetworkSettings", {}).get("Ports") or {}
            for host_cfg in ports.values():
                if host_cfg:
                    for hp in host_cfg:
                        try:
                            used.add(int(hp["HostPort"]))
                        except (KeyError, ValueError, TypeError):
                            pass
    except Exception as e:
        app.logger.error(f"列容器失败: {e}")
    return used


def next_free_port() -> int:
    """从 CLAUDE_PORT_MIN 扫描到 CLAUDE_PORT_MAX，找第一个未被占用的 host port。"""
    used = get_used_ports()
    p = CLAUDE_PORT_MIN
    while p <= CLAUDE_PORT_MAX and p in used:
        p += 1
    if p > CLAUDE_PORT_MAX:
        raise RuntimeError(
            f"port range [{CLAUDE_PORT_MIN}, {CLAUDE_PORT_MAX}] exhausted"
        )
    return p


def _port_file_path(user_id: str) -> str:
    """portal 容器内视角的 .port 文件路径（bind mount 到 host 的 volumes/users/<uid>/.port）"""
    return os.path.join(USER_DATA_BASE, user_id, PORT_FILE_NAME)


def _read_pinned_port(user_id: str) -> int | None:
    """读 .port 文件，校验是合法 int。失败返回 None（当成首次分配处理）。"""
    try:
        with open(_port_file_path(user_id)) as f:
            raw = f.read().strip()
        p = int(raw)
        if 1 <= p <= 65535:
            return p
    except (FileNotFoundError, ValueError):
        pass
    except Exception as e:
        app.logger.warning(f"read pinned port for {user_id[:8]}: {e}")
    return None


def _write_pinned_port(user_id: str, port: int) -> None:
    """把固定端口写到 .port。atomic write via tmp+replace，多 worker 同 uid 写同一个值是幂等的。"""
    try:
        path = _port_file_path(user_id)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(f"{port}\n")
        os.replace(tmp, path)
        # 防御：容器内 node 用户可能意外覆盖它；保险起见 chown 给 1000:1000
        try:
            os.chmod(path, 0o666)
            os.chown(path, 1000, 1000)
        except (PermissionError, OSError):
            pass  # 端口 host 上是 root-owned 也无所谓——不需要 node 进程碰它
    except Exception as e:
        app.logger.error(f"write pinned port for {user_id[:8]}: {e}")


def alloc_port_for_user(user_id: str) -> int:
    """返回该用户稳定的 host port。

    决策：
      1) 读 .port 文件里保存的端口
      2) 校验：port 在 [CLAUDE_PORT_MIN, CLAUDE_PORT_MAX] 内 + 当前未被其他容器占用
      3) 通过 → 用之；不通过 → 当成首次，next_free_port() 并覆写 .port

    注：调用前 find_user_container 已经返回 None（用户当前没有运行中容器），
        所以 saved_port 不会是被用户自己的容器占着的情况——get_used_ports() 里
        那条会触发"被其他用户占用"分支自动降级。
    """
    used = get_used_ports()
    saved = _read_pinned_port(user_id)
    if saved is not None and CLAUDE_PORT_MIN <= saved <= CLAUDE_PORT_MAX and saved not in used:
        return saved
    if saved is not None:
        if not (CLAUDE_PORT_MIN <= saved <= CLAUDE_PORT_MAX):
            app.logger.warning(
                f"alloc_port: saved port {saved} for {user_id[:8]} "
                f"out of range [{CLAUDE_PORT_MIN}, {CLAUDE_PORT_MAX}], re-allocating"
            )
        elif saved in used:
            app.logger.warning(
                f"alloc_port: saved port {saved} for {user_id[:8]} "
                f"taken by another container, re-allocating"
            )
        else:
            app.logger.warning(
                f"alloc_port: saved port {saved} for {user_id[:8]} invalid, re-allocating"
            )
    p = next_free_port()
    _write_pinned_port(user_id, p)
    return p


def find_user_container(user_id: str):
    """获取该用户的运行中容器，没有或状态不对返回 None。"""
    if not client or not is_valid_user_id(user_id):
        return None
    name = f"claude-{user_id}"
    try:
        c = client.containers.get(name)
        if c.status == "running":
            return c
        # 残留旧容器，清掉
        try:
            c.remove(force=True)
        except Exception:
            pass
    except NotFound:
        pass
    except Exception as e:
        app.logger.error(f"取容器失败 {name}: {e}")
    return None


def wait_running(container, timeout: int = 60) -> tuple[bool, str]:
    """轮询直到 running。返回 (ok, 日志尾巴)。失败时日志带回错误原因。"""
    start = time.time()
    last_status = None
    while time.time() - start < timeout:
        try:
            container.reload()
        except Exception:
            pass
        if container.status == "running":
            return True, ""
        last_status = container.status
        time.sleep(0.4)
    # 启动失败，把日志带回去
    try:
        logs = container.logs(tail=30).decode("utf-8", errors="replace")
    except Exception:
        logs = f"(no logs available, last_status={last_status})"
    return False, logs[:2000] + (logs[2000:] and " ...(truncated)")


def start_container(user_id: str, base_url: str, api_key: str,
                     opus_model: str, sonnet_model: str, haiku_model: str,
                     wipe_scratch: bool = False):
    """启动新 claude 容器。返回 (container, port, password)。
    password 直接等于 user_id（确定性），便于用户知道/记忆。

    模型环境变量约定：
      - ANTHROPIC_DEFAULT_HAIKU_MODEL  → 用户填的 haiku
      - ANTHROPIC_DEFAULT_SONNET_MODEL → 用户填的 sonnet
      - ANTHROPIC_DEFAULT_OPUS_MODEL   → 用户填的 opus
      - ANTHROPIC_MODEL                → 复制 opus（按用户要求"把 OPUS 复制到 MODEL"）
    Claude Code 优先级：--model flag > ANTHROPIC_MODEL > 默认；
    按 family 路由时用 *_DEFAULT_*_MODEL 系列。

    wipe_scratch=True → 先删 scratch 目录再重建（用于 rebuild 时的"重置临时文件"选项）。
    注意：home 目录的 wipe 不在这里做，由调用方在 ensure_user_dir(user_id, wipe=...) 时处理。
    """
    if not client:
        raise RuntimeError("docker unavailable")
    user_dir = ensure_user_dir(user_id)
    host_user_dir = host_user_dir_for(user_id)  # docker.run() bind source 用 host 视角
    # scratch 目录必须存在才能 bind mount（bind source 不存在 → 容器内是空目录但首次写会失败）
    # chown 给 1000:1000 + chmod 1777（sticky bit）：每个文件只能被创建者删除
    # makedirs 用 portal 当前 uid=root 创建，再 chmod/chown 修正
    scratch_dir = os.path.join(user_dir, SCRATCH_DIR_NAME)
    if wipe_scratch and os.path.exists(scratch_dir):
        shutil.rmtree(scratch_dir, ignore_errors=True)
        app.logger.info(f"wiped scratch dir: {user_id}")
    os.makedirs(scratch_dir, exist_ok=True)
    try:
        os.chmod(scratch_dir, 0o1777)
        os.chown(scratch_dir, 1000, 1000)
    except Exception as e:
        app.logger.warning(f"chmod/chown 1777 on scratch failed (non-fatal): {e}")
    # 给容器生成/复用带正确 SAN 的证书（SAN 包含浏览器访问时的 Host）
    cert_dir, cert_path, key_path = ensure_user_cert(user_id)
    # entrypoint.sh 在容器内跑，看不到 host 视角路径；用容器内 bind target 路径
    container_cert_path = os.path.join(CLAUDE_CERT_DIR_BIND, CLAUDE_CERT_FILENAME)
    container_key_path  = os.path.join(CLAUDE_CERT_DIR_BIND, CLAUDE_KEY_FILENAME)
    port = alloc_port_for_user(user_id)
    password = user_id   # 用户能用 apiKey 推出来；本团队内部用，可接受

    container = client.containers.run(
        image=CLAUDE_IMAGE_NAME,
        name=f"claude-{user_id}",
        detach=True,
        user="node:node",  # 以 node 用户跑：$HOME 默认就是 /home/node，正好对齐 bind mount
        environment={
            "ANTHROPIC_BASE_URL":            base_url,
            "ANTHROPIC_API_KEY":             api_key,
            "ANTHROPIC_MODEL":                opus_model,  # 默认模型 = opus（按用户要求）
            "ANTHROPIC_DEFAULT_HAIKU_MODEL":  haiku_model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": sonnet_model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL":   opus_model,
            "PASSWORD":                       password,
            # 把运行时证书路径告诉 entrypoint.sh（覆盖默认的 localhost cert）
            # 用容器内 bind target 路径，entrypoint 在容器内只能看见这个
            "CERT_FILE":                      container_cert_path,
            "KEY_FILE":                       container_key_path,
            # 给 entrypoint.sh / code-server 暴露容器内 bind target，
            # 这样镜像重建时如果改了 CLAUDE_*_BIND，entrypoint.sh 也能跟上
            "CLAUDE_WORKSPACE_BIND":          CLAUDE_WORKSPACE_BIND,
            "CLAUDE_HOME_BIND":               CLAUDE_HOME_BIND,
            "CLAUDE_SCRATCH_BIND":            CLAUDE_SCRATCH_BIND,
            "CLAUDE_SCRATCH_DIR_NAME":        SCRATCH_DIR_NAME,
            "CLAUDE_WORKSPACE_FILE_NAME":     WORKSPACE_FILE_NAME,
        },
        volumes={
            HOST_WORKSPACE_PATH: {"bind": CLAUDE_WORKSPACE_BIND, "mode": "ro"},
            host_user_dir:       {"bind": CLAUDE_HOME_BIND,      "mode": "rw"},
            # per-user scratch：multi-root workspace 的第二个根（CLAUDE_SCRATCH_BIND），
            # 由 entrypoint.sh 写入 .code-workspace 让 VS Code 把它和 CLAUDE_WORKSPACE_BIND 一起打开。
            # 独立 bind mount 不被 CLAUDE_HOME_BIND RW 覆盖，是因为它在 CLAUDE_HOME_BIND 下是子目录、
            # bind mount 是叶子挂载（leaf mount），优先级高于父目录 mount。
            os.path.join(host_user_dir, SCRATCH_DIR_NAME): {"bind": CLAUDE_SCRATCH_BIND, "mode": "rw"},
            # 证书目录 bind 进去：覆盖镜像里 CLAUDE_CERT_DIR_BIND（root:root 755）
            # 单文件 bind mount 在 docker 里会因 "not a directory" 失败（runc bug），
            # 所以 mount 整个目录，里面只放 CLAUDE_CERT_FILENAME / CLAUDE_KEY_FILENAME 两个文件
            cert_dir:             {"bind": CLAUDE_CERT_DIR_BIND, "mode": "ro"},
        },
        ports={f"{CLAUDE_INTERNAL_PORT}/tcp": port},
        labels={PORTAL_LABEL: "claude-portal"},
        remove=False,
    )

    ok, logs = wait_running(container)
    if not ok:
        # 清理
        try:
            container.remove(force=True)
        except Exception:
            pass
        raise RuntimeError(logs or "container did not start in time")

    return container, port, password


def get_container_port(container) -> int | None:
    ports = container.attrs.get("NetworkSettings", {}).get("Ports", {}).get(f"{CLAUDE_INTERNAL_PORT}/tcp")
    if ports:
        return int(ports[0]["HostPort"])
    return None


def rebuild_container(user_id: str, base_url: str, api_key: str,
                      opus_model: str, sonnet_model: str, haiku_model: str,
                      reset_home: bool = False, reset_scratch: bool = False):
    """停止并删除旧容器（如果存在），按选项重置 home/scratch 后启动新容器。
    返回 (container, port, password)。

    步骤：
      1) 找到旧容器（如有）→ 删掉
      2) 如果 reset_home → ensure_user_dir(user_id, wipe=True)（删整个 home 再从模板重建）
      3) start_container(..., wipe_scratch=reset_scratch)（在内部删 scratch 再建）
    注意顺序：先删容器，再删目录 —— 反过来容器还在跑就会写到被删的目录。
    """
    if not client:
        raise RuntimeError("docker unavailable")
    # 1) 旧容器 stop + remove
    try:
        old = client.containers.get(f"claude-{user_id}")
        try:
            old.stop(timeout=10)
        except Exception:
            pass
        old.remove(force=True)
        app.logger.info(f"rebuild: removed old container for {user_id}")
    except NotFound:
        pass
    except Exception as e:
        app.logger.warning(f"rebuild: removing old container failed (continuing): {e}")
    # 2) 重置 home（如果勾选）—— 必须在删容器之后
    if reset_home:
        ensure_user_dir(user_id, wipe=True)
    # 3) start_container 内部会处理 scratch 重置
    return start_container(
        user_id, base_url, api_key, opus_model, sonnet_model, haiku_model,
        wipe_scratch=reset_scratch,
    )


# ---------- Admin 认证 ----------
#
# 用 Flask session（有签名 cookie）做无状态认证：
#   - 登录：session["admin"] = True（Flask 用 app.secret_key 签名 cookie）
#   - 校验：session.get("admin") is True
#   - 退出：session.clear()
# 优点：gunicorn 多 worker 共享（cookie 客户端持有，worker 之间无状态）；
#       重启 portal 后旧 cookie 仍可解码但 secret_key 一变就全部失效。
# 注意：未设 ADMIN_PASSWORD → 整个 admin 路由直接 404（不留入口）。

def admin_auth_enabled() -> bool:
    """ADMIN_PASSWORD 没设或为空 → admin 路由全 404（关掉整个管理面板）。"""
    return bool(ADMIN_PASSWORD)


def require_admin_session():
    """Admin API 路由入口检查。失败 → (None, response)。成功 → (True, None)。"""
    if not admin_auth_enabled():
        return None, (jsonify({"error": "admin disabled (set ADMIN_PASSWORD)"}), 404)
    if not session.get("admin"):
        return None, (jsonify({"error": "未登录或会话已过期"}), 401)
    return True, None


# ---------- HTTPS 证书（运行时动态生成） ----------
#
# code-server 的 webview 依赖 crypto.subtle（secure context），
# 更严格的是 webview 还要注册 Service Worker——Service Worker 脚本下载时浏览器
# 会做证书 hostname 校验，必须跟浏览器访问的 Host 完全对得上。
# 镜像里默认证书 SAN=DNS:localhost, IP:127.0.0.1，只能在容器内/SSH 隧道访问时用；
# 用户直接通过 IP 访问（192.168.1.2 这种）就会失败。
#
# 解决：portal 在每次启动容器前，根据浏览器请求的 Host header 动态生成
# 带正确 SAN 的自签证书，写到 host 上 user_dir 里，bind mount 覆盖到
# 容器的 CLAUDE_CERT_DIR_BIND/cert.pem / key.pem。
# 同一用户复用容器时不会再生成（看 host/user 是否变）。

# Internal root CA：给所有用户容器签发 cert。用户首次使用时需要把 CA 装到
# chrome 的 trust store（/install-cert 下载入口），之后所有用这个 CA 签的 cert
# 在 chrome 里都被信任 → Service Worker 能注册 → webview 渲染。
# 为什么不用纯 self-signed cert：Chrome 的 Service Worker fetch 严格不接受
# self-signed certificate（即使主页面用户 accept 了 "继续访问"，SW context
# 仍然 reject），参考 microsoft/vscode#136553 + #152880。


def ensure_ca() -> tuple[str, str]:
    """确保 root CA 存在。首次启动时生成自签 CA（持久化到 HOST_CERTS_DIR）。

    返回 (container_ca_cert_path, container_ca_key_path) 在 portal 容器内的绝对路径。
    cert 是公开的（要下载给用户），key 是 CA private key（绝不能泄露）。
    """
    container_ca_cert = os.path.join(CA_DIR, CA_CERT_FILENAME)
    container_ca_key  = os.path.join(CA_DIR, CA_KEY_FILENAME)
    os.makedirs(CA_DIR, exist_ok=True)
    if os.path.exists(container_ca_cert) and os.path.exists(container_ca_key):
        return container_ca_cert, container_ca_key

    print(f"[ensure_ca] generating new root CA at {CA_DIR}", flush=True)
    try:
        # 4096-bit RSA，10 年有效
        subprocess.run([
            "openssl", "genrsa", "-out", container_ca_key, "4096",
        ], check=True, capture_output=True, text=True, timeout=30)
        subprocess.run([
            "openssl", "req", "-x509", "-new", "-nodes",
            "-key", container_ca_key, "-sha256", "-days", "3650",
            "-subj", "/CN=Claude Code Portal Internal CA",
            "-out", container_ca_cert,
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,digitalSignature,keyCertSign,cRLSign",
        ], check=True, capture_output=True, text=True, timeout=30)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"生成 root CA 失败: {e.stderr}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("生成 root CA 超时") from e

    # 锁 key 权限：只 root 可读写
    os.chmod(container_ca_key, 0o600)
    os.chmod(container_ca_cert, 0o644)
    print(f"[ensure_ca] root CA generated", flush=True)
    return container_ca_cert, container_ca_key


def ensure_user_cert(user_id: str) -> tuple[str, str, str]:
    """确保该用户有可用证书（用 internal root CA 签发），返回 (cert_dir, cert_path, key_path) 在 host 上的绝对路径。

    浏览器访问 portal 时 Host header 里的 hostname/IP 进证书 SAN。
    生成过一次就缓存（除非 Host 变了——但同一用户复用时 Host 一般不变）。

    返回 3 个值：
      - cert_dir（host 视角绝对路径，给 docker.run() 当 bind source）
      - cert_path / key_path（也用 host 视角，绑到容器 /etc/code-server/ 后
        code-server 用 CERT_FILE / KEY_FILE env 拿这两个路径）

    写文件时要用 portal 容器内的 /volumes 路径（USER_DATA_BASE），那才是 host 上
    volumes/users/<uid>/ 的实际 bind mount 入口。如果用 HOST_USER_DATA_BASE 写，
    会写到 portal 容器自己的 /home/... 路径下，host 上根本看不到。
    """
    host = (request.host.split(":")[0] if request.host else "localhost").strip() or "localhost"
    print(f"[ensure_user_cert] user={user_id} host={host}", flush=True)

    # 容器内视角（写入位置 = portal 通过 bind mount 看到的 host volumes/）
    container_cert_dir = os.path.join(USER_DATA_BASE, user_id, CERT_DIR_NAME)
    container_cert_path = os.path.join(container_cert_dir, CLAUDE_CERT_FILENAME)
    container_key_path  = os.path.join(container_cert_dir, CLAUDE_KEY_FILENAME)

    # host 视角（给 docker.run() 当 bind source —— docker daemon 在 host 上解析）
    cert_dir = os.path.join(HOST_USER_DATA_BASE, user_id, CERT_DIR_NAME)
    cert_path = os.path.join(cert_dir, CLAUDE_CERT_FILENAME)
    key_path = os.path.join(cert_dir, CLAUDE_KEY_FILENAME)
    print(f"[ensure_user_cert] container_cert_dir={container_cert_dir}", flush=True)
    print(f"[ensure_user_cert] host_cert_dir={cert_dir}", flush=True)

    # 决定 SAN：host 是 IP 加 IP:xx，是 hostname 加 DNS:xx；总带 localhost/127.0.0.1
    sans = ["DNS:localhost", "IP:127.0.0.1", "IP:0.0.0.0"]
    try:
        ipaddress.ip_address(host)
        sans.append(f"IP:{host}")
    except ValueError:
        sans.append(f"DNS:{host}")
    san_str = ",".join(sans)
    print(f"[ensure_user_cert] SAN={san_str}", flush=True)

    # 如果已生成过证书且 CN 与当前 host 一致，直接复用（同一用户复用容器时省时间）
    if os.path.exists(container_cert_path) and os.path.exists(container_key_path):
        try:
            existing_cn = subprocess.run(
                ["openssl", "x509", "-noout", "-subject", "-nameopt", "RFC2253",
                 "-in", container_cert_path],
                capture_output=True, text=True, timeout=5,
            ).stdout
            if f"CN={host}" in existing_cn:
                print(f"[ensure_user_cert] reusing existing cert", flush=True)
                return cert_dir, cert_path, key_path
            print(f"[ensure_user_cert] regenerating (host changed)", flush=True)
        except Exception as e:
            print(f"[ensure_user_cert] openssl x509 inspect failed: {e}", flush=True)

    os.makedirs(container_cert_dir, exist_ok=True)

    # 用 internal root CA 给这个 host 签 cert
    container_ca_cert, container_ca_key = ensure_ca()
    # 生成 key + CSR
    try:
        subprocess.run([
            "openssl", "genrsa", "-out", container_key_path, "2048",
        ], check=True, capture_output=True, text=True, timeout=15)
        # 用 SAN 写进 openssl config（-extfile 比 -addext 复杂，但更灵活）
        san_config = os.path.join(container_cert_dir, SAN_CONFIG_NAME)
        with open(san_config, "w") as f:
            f.write(f"subjectAltName={san_str}\n")
        subprocess.run([
            "openssl", "req", "-new",
            "-key", container_key_path,
            "-out", os.path.join(container_cert_dir, CSR_FILENAME),
            "-subj", f"/CN={host}",
        ], check=True, capture_output=True, text=True, timeout=15)
        # 用 CA 签发 cert
        subprocess.run([
            "openssl", "x509", "-req",
            "-in", os.path.join(container_cert_dir, CSR_FILENAME),
            "-CA", container_ca_cert,
            "-CAkey", container_ca_key,
            "-CAcreateserial",
            "-out", container_cert_path,
            "-days", "3650",
            "-sha256",
            "-extfile", san_config,
        ], check=True, capture_output=True, text=True, timeout=15)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"openssl 签发用户证书失败: rc={e.returncode} stderr={e.stderr} stdout={e.stdout}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("openssl 签发证书超时") from e

    # 清理 CSR + 临时 SAN 配置（CSR_FILENAME 不需要持久化，SAN_CONFIG_NAME 是一次性）
    try:
        os.remove(os.path.join(container_cert_dir, CSR_FILENAME))
        os.remove(san_config)
    except Exception:
        pass

    os.chmod(container_cert_path, 0o644)
    os.chmod(container_key_path, 0o644)
    # chown + chmod：让 node 用户（uid 1000）完全 ownership，父目录 mode 0777
    # 否则 thomas 在 host 端没法 rm（code-server-certs/ 默认 0755 owned by root）
    try:
        os.chown(container_cert_path, 1000, 1000)
        os.chown(container_key_path, 1000, 1000)
        os.chown(container_cert_dir, 1000, 1000)
        os.chmod(container_cert_dir, 0o777)
    except Exception as e:
        print(f"[ensure_user_cert] chown/chmod 1000:1000 warn: {e}", flush=True)
    print(f"[ensure_user_cert] cert signed by internal CA, SAN={san_str}", flush=True)
    return cert_dir, cert_path, key_path


# ---------- 路由 ----------

@app.route("/")
def index():
    return render_template("login.html")


@app.route("/api/start", methods=["POST"])
def api_start():
    if not client:
        return jsonify({"error": "docker unavailable"}), 503

    data = request.get_json(silent=True) or {}
    base_url     = (data.get("baseUrl")     or "").strip()
    api_key      = (data.get("apiKey")      or "").strip()
    opus_model   = (data.get("opusModel")   or "").strip()
    sonnet_model = (data.get("sonnetModel") or "").strip()
    haiku_model  = (data.get("haikuModel")  or "").strip()
    display_name = _sanitize_display_name(data.get("displayName"))

    if not (base_url and api_key and opus_model and sonnet_model and haiku_model):
        return jsonify({
            "error": "缺少必要字段 baseUrl/apiKey/opusModel/sonnetModel/haikuModel"
        }), 400
    if not display_name:
        return jsonify({
            "error": "缺少 displayName（显示名称必填）"
        }), 400

    user_id = hash_api_key(api_key)
    short = user_id[:8]
    app.logger.info(f"login attempt: user={short}... displayName={display_name!r}")

    users = load_users_cache()

    # 1) 复用现有
    existing = find_user_container(user_id)
    if existing:
        port = get_container_port(existing)
        if port:
            # password == user_id 是确定性的，无需从 cache 读
            password = user_id
            users[user_id] = {
                "container_id": existing.id,
                "port": port,
                "password": password,
                "last_seen": time.time(),
            }
            save_users_cache(users)
            # displayName：仅当客户端明确传了非空值时更新（保留旧名不会被空串清掉）
            if display_name:
                _set_user_display_name(user_id, display_name)
            app.logger.info(f"reused container for {short} on port {port}")
            return jsonify({
                "port": port,
                "password": password,
                "user_id": user_id,
                "display_name": _read_user_meta(user_id).get("display_name", ""),
                "reused": True,
            })

    # 2) 起新的
    if MAX_ACTIVE_CONTAINERS > 0:
        active, _ = count_active_claude_containers()
        if active >= MAX_ACTIVE_CONTAINERS:
            app.logger.warning(
                f"start rejected: active={active} >= MAX_ACTIVE_CONTAINERS={MAX_ACTIVE_CONTAINERS}"
            )
            return jsonify({
                "error": f"活跃容器已达上限 {MAX_ACTIVE_CONTAINERS}（当前 {active}）。请稍后再试，或联系管理员清理。",
                "code": "MAX_ACTIVE_REACHED",
                "active": active,
                "limit": MAX_ACTIVE_CONTAINERS,
            }), 429
    try:
        container, port, password = start_container(
            user_id, base_url, api_key, opus_model, sonnet_model, haiku_model
        )
    except Exception as e:
        app.logger.error(f"start failed for {short}: {e}")
        # 把容器日志也带回去，便于排查
        return jsonify({
            "error": f"启动容器失败: {str(e)}",
            "hint": "查看 ARCHITECTURE.md §10 常见问题"
        }), 500

    users[user_id] = {
        "container_id": container.id,
        "port": port,
        "password": password,
        "last_seen": time.time(),
    }
    save_users_cache(users)
    # displayName 仅在新建时写入（避免每次 start 都覆盖历史改名）
    if display_name:
        _set_user_display_name(user_id, display_name)
    app.logger.info(f"started container for {short} on port {port}")
    return jsonify({
        "port": port,
        "password": password,
        "user_id": user_id,
        "display_name": _read_user_meta(user_id).get("display_name", ""),
        "reused": False,
    })


@app.route("/api/status/<user_id>")
def api_status(user_id):
    if not is_valid_user_id(user_id):
        return jsonify({"error": "invalid user_id"}), 400
    container = find_user_container(user_id)
    users = load_users_cache()
    info = users.get(user_id)
    return jsonify({
        "user_id":      user_id,
        "port":         info.get("port") if info else None,
        "running":      container is not None,
        "last_seen":    info.get("last_seen") if info else None,
        "display_name": _read_user_meta(user_id).get("display_name", ""),
    })


@app.route("/install-cert")
def install_cert():
    """让用户下载 internal root CA，**首次使用**装到浏览器/系统 trust store。

    为什么需要：Code Server webview 依赖 Service Worker。Chrome 对 Service Worker
    fetch 严格不接受 self-signed certificate（即使主页 accept 了 "继续访问"），
    必须用 trusted CA 签的 cert。所以 portal 用 internal root CA 给所有用户
    容器签 cert；用户只需要把 root CA 装到本机一次。

    安装方法（Windows）：
      1. 下载 ca.crt 文件
      2. 双击 → "安装证书" → 选"受信任的根证书颁发机构" → 完成
      3. 重启 chrome
    """
    try:
        ca_cert_path, _ = ensure_ca()
        with open(ca_cert_path, "rb") as f:
            data = f.read()
    except Exception as e:
        return f"CA cert not available: {e}", 500

    from flask import Response
    return Response(
        data,
        mimetype="application/x-x509-ca-cert",
        headers={
            "Content-Disposition": 'attachment; filename="claude-code-portal-ca.crt"',
            "Content-Length": str(len(data)),
        },
    )


@app.route("/api/users")
def api_users():
    """列出所有由 portal 启动的容器（管理用，不暴露密钥）。"""
    if not client:
        return jsonify({"error": "docker unavailable"}), 503
    items = []
    try:
        for c in client.containers.list(filters={"label": f"{PORTAL_LABEL}=claude-portal"}):
            name = c.name
            uid  = name.replace("claude-", "", 1) if name.startswith("claude-") else name
            items.append({
                "user_id":        uid,
                "container_name": name,
                "status":         c.status,
                "port":           get_container_port(c),
                "created":        c.attrs.get("Created"),
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"users": items})


# ---------- 用户自助重建容器 ----------
#
# 用户改了 baseUrl / 模型名（任何字段变）→ 旧容器环境变量对不上 → 必须重建。
# 用户没改任何字段（hash 一致）→ 不该走这个接口，直接复用即可。
# reset_home / reset_scratch 默认 False，按需勾选。


# ---------- 用户自助改名（不动容器） ----------
#
# displayName 是纯辅助标识，存在 volumes/users/<uid>/.portal_meta.json。
# 不参与鉴权 / 不影响隔离。改名字不需要重启 code-server。
# 鉴权：因为 user_id = sha256(apiKey)[:16]，客户端想改自己的名字必须出示对应 apiKey。
# （同 uid 隔离边界已经在 hash_api_key 里由 apiKey 单向派生）
@app.route("/api/profile", methods=["PATCH"])
def api_profile():
    data = request.get_json(silent=True) or {}
    api_key = (data.get("apiKey") or "").strip()
    new_name = _sanitize_display_name(data.get("displayName"))
    if not api_key:
        return jsonify({"error": "缺少 apiKey"}), 400
    if not new_name:
        return jsonify({"error": "缺少 displayName（显示名称必填，不能清空）"}), 400

    user_id = hash_api_key(api_key)
    short = user_id[:8]
    app.logger.info(f"profile rename: user={short}... → {new_name!r}")
    # meta 目录可能还不存在（极端情况：start 之前先 PATCH）。确保 user_dir 在场。
    ensure_user_dir(user_id)
    _set_user_display_name(user_id, new_name)
    return jsonify({"ok": True, "display_name": new_name})

@app.route("/api/rebuild", methods=["POST"])
def api_rebuild():
    if not client:
        return jsonify({"error": "docker unavailable"}), 503
    data = request.get_json(silent=True) or {}
    base_url     = (data.get("baseUrl")     or "").strip()
    api_key      = (data.get("apiKey")      or "").strip()
    opus_model   = (data.get("opusModel")   or "").strip()
    sonnet_model = (data.get("sonnetModel") or "").strip()
    haiku_model  = (data.get("haikuModel")  or "").strip()
    display_name = _sanitize_display_name(data.get("displayName"))
    reset_home   = bool(data.get("resetHome",   False))
    reset_scratch = bool(data.get("resetScratch", False))
    if not (base_url and api_key and opus_model and sonnet_model and haiku_model):
        return jsonify({"error": "缺少必要字段"}), 400
    if not display_name:
        return jsonify({"error": "缺少 displayName（显示名称必填）"}), 400

    user_id = hash_api_key(api_key)
    short = user_id[:8]
    app.logger.info(f"rebuild: user={short}... reset_home={reset_home} reset_scratch={reset_scratch} displayName={display_name!r}")
    # 上限检查：rebuild 总是先 stop+remove 旧容器再起新的，活跃数变化跟 start 一样
    # 都要先验。但要排除自己的旧容器（rebuild 时它还没被删），否则永远自增 → 永远超限
    if MAX_ACTIVE_CONTAINERS > 0:
        active, _ = count_active_claude_containers()
        # 自己有一个要死的，先扣 1
        own_running = 1 if find_user_container(user_id) else 0
        if (active - own_running) >= MAX_ACTIVE_CONTAINERS:
            app.logger.warning(
                f"rebuild rejected: active={active} (own={own_running}) >= MAX_ACTIVE_CONTAINERS={MAX_ACTIVE_CONTAINERS}"
            )
            return jsonify({
                "error": f"活跃容器已达上限 {MAX_ACTIVE_CONTAINERS}（当前 {active}）。请稍后再试，或联系管理员清理。",
                "code": "MAX_ACTIVE_REACHED",
                "active": active,
                "limit": MAX_ACTIVE_CONTAINERS,
            }), 429
    try:
        container, port, password = rebuild_container(
            user_id, base_url, api_key, opus_model, sonnet_model, haiku_model,
            reset_home=reset_home, reset_scratch=reset_scratch,
        )
    except Exception as e:
        app.logger.error(f"rebuild failed for {short}: {e}")
        return jsonify({"error": f"重建失败: {e}", "hint": "查看 ARCHITECTURE.md §10"}), 500

    users = load_users_cache()
    users[user_id] = {
        "container_id": container.id,
        "port": port,
        "password": password,
        "last_seen": time.time(),
    }
    save_users_cache(users)
    # 注意：rebuild 时 reset_home=True 会删整个用户目录，所以 displayName 必须在
    # rebuild 写回 *之前* 重新落地一次，否则 .portal_meta.json 会被一起删掉。
    # 客户端传过来的 displayName 优先（用户改名场景）；若为空则保留旧 meta（如果还在的话）。
    if display_name:
        _set_user_display_name(user_id, display_name)
    elif reset_home:
        # 重置 home 后 meta.json 也被清空了，留空无所谓，admin 会显示 "—"
        pass
    app.logger.info(f"rebuild done: user={short}... port={port}")
    return jsonify({
        "port": port,
        "password": password,
        "user_id": user_id,
        "display_name": _read_user_meta(user_id).get("display_name", ""),
    })


# ---------- Admin 页面 + API ----------

@app.route("/admin")
def admin_page():
    """Admin 页面：未启用 ADMIN_PASSWORD 时直接 404（不留入口）。"""
    if not admin_auth_enabled():
        return "Not Found", 404
    return render_template("admin.html")


@app.route("/api/admin/login", methods=["POST"])
def api_admin_login():
    if not admin_auth_enabled():
        return jsonify({"error": "admin disabled"}), 404
    ip = request.remote_addr or "unknown"
    now = time.time()
    # 冷却检查
    fails = _admin_login_failures.get(ip)
    if fails:
        count, last = fails
        if count >= _ADMIN_MAX_ATTEMPTS and (now - last) < _ADMIN_LOCKOUT_SEC:
            return jsonify({
                "error": f"登录失败次数过多，请 {_ADMIN_LOCKOUT_SEC:.0f} 秒后重试"
            }), 429
        if (now - last) >= _ADMIN_LOCKOUT_SEC:
            _admin_login_failures.pop(ip, None)

    data = request.get_json(silent=True) or {}
    pw = (data.get("password") or "")
    if not pw or not secrets.compare_digest(pw, ADMIN_PASSWORD):
        # 累计失败次数
        prev = _admin_login_failures.get(ip, (0, now))
        _admin_login_failures[ip] = (prev[0] + 1, now)
        return jsonify({"error": "密码错误"}), 401

    # 成功：清失败计数 + 写 session（Flask 签名 cookie，跨 worker 无状态）
    _admin_login_failures.pop(ip, None)
    session.clear()
    session["admin"] = True
    session.permanent = True
    # permanent_session_lifetime 由 app.permanent_session_lifetime 控制（下面初始化时设）
    return jsonify({"ok": True})


@app.route("/api/admin/logout", methods=["POST"])
def api_admin_logout():
    _, err = require_admin_session()
    if err:
        return err
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/admin/containers")
def api_admin_containers():
    _, err = require_admin_session()
    if err:
        return err
    if not client:
        return jsonify({"error": "docker unavailable"}), 503
    users = load_users_cache()
    items = []
    # 一个失败容器不能拖垮整个列表 —— 单条 try/except 兜底
    # 特别坑：c.image.tags[0] 会触发 docker daemon 的 image inspect，
    # 若镜像已被 docker rmi 删掉会 404。改用 c.attrs["Config"]["Image"]（已在内存里，不发请求）。
    try:
        containers = client.containers.list(all=True, filters={"label": f"{PORTAL_LABEL}=claude-portal"})
    except Exception as e:
        return jsonify({"error": f"list containers: {e}"}), 500

    for c in containers:
        try:
            name = c.name
            uid  = name.replace("claude-", "", 1) if name.startswith("claude-") else name
            cached = users.get(uid, {})
            actual_port = get_container_port(c)
            pinned_port = _read_pinned_port(uid)
            items.append({
                "user_id":        uid,
                "display_name":   _read_user_meta(uid).get("display_name", ""),
                "container_name": name,
                "container_id":   c.id[:12],
                "status":         c.status,
                "port":           actual_port,
                # .port 文件里的固定端口；may be None（升级前用户没有 .port）。
                # admin UI 用它对比实际绑定端口：一致 → ✓ 正常；不一致 → ⚠️ 冲突。
                "pinned_port":    pinned_port,
                "last_seen":      cached.get("last_seen"),
                "created":        c.attrs.get("Created"),
                # attrs["Config"]["Image"] 在容器启动时就已记录在 metadata 里，
                # 即便镜像后来被 docker rmi 删了也不会失效；不触发 inspect API。
                "image":          c.attrs.get("Config", {}).get("Image") or "",
            })
        except Exception as e:
            # 单个容器信息拉不到（含镜像不存在、网络端口表异常等），跳过不阻断其他
            app.logger.warning(f"admin list: skip container (err={e})")
            continue

    items.sort(key=lambda x: x.get("last_seen") or 0, reverse=True)
    active = sum(1 for c in containers if c.status == "running")
    return jsonify({
        "containers": items,
        "active":     active,
        "total":      len(containers),
        "limit":      MAX_ACTIVE_CONTAINERS,
    })


@app.route("/api/admin/stop", methods=["POST"])
def api_admin_stop():
    _, err = require_admin_session()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    uid = (data.get("userId") or "").strip()
    if not is_valid_user_id(uid):
        return jsonify({"error": "invalid userId"}), 400
    try:
        c = client.containers.get(f"claude-{uid}")
        c.stop(timeout=10)
        return jsonify({"ok": True, "status": "stopped"})
    except NotFound:
        return jsonify({"error": "container not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/delete", methods=["POST"])
def api_admin_delete():
    """管理员强制删除某个用户容器（可选清 home/scratch 数据）。

    wipe_home / wipe_scratch：
      - wipe_home=True   → 删除整个 volumes/users/<uid>/（含 scratch、cert、meta 等一切）
      - wipe_home=False, wipe_scratch=True → 仅删 scratch/，保留 home/settings/扩展
      - 两个都 False     → 只删容器，用户数据保留供下次登录复用
    """
    _, err = require_admin_session()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    uid = (data.get("userId") or "").strip()
    wipe_home    = bool(data.get("wipeHome",    False))
    wipe_scratch = bool(data.get("wipeScratch", False))
    if not is_valid_user_id(uid):
        return jsonify({"error": "invalid userId"}), 400
    # 删容器
    try:
        c = client.containers.get(f"claude-{uid}")
        try:
            c.stop(timeout=10)
        except Exception:
            pass
        c.remove(force=True)
        app.logger.info(f"admin delete: removed container for {uid[:8]}...")
    except NotFound:
        app.logger.info(f"admin delete: container for {uid[:8]}... not found, continuing")
    except Exception as e:
        return jsonify({"error": f"删除容器失败: {e}"}), 500
    # 删数据（按需）。注意顺序：先删容器再删目录（容器还在跑时不能 rm bind source 内容）
    user_dir = os.path.join(USER_DATA_BASE, uid)
    wiped = []
    if wipe_home and os.path.isdir(user_dir):
        def _on_err(fn, p, exc_info):
            app.logger.error(f"admin delete wipe_home: rmtree failed at {p} ({fn}): {exc_info[1]}")
        try:
            shutil.rmtree(user_dir, onerror=_on_err)
            wiped.append("home")
            app.logger.info(f"admin delete: wiped home for {uid[:8]}...")
        except Exception as e:
            app.logger.error(f"admin delete: wipe_home raised for {uid[:8]}...: {e}")
    elif wipe_scratch:
        scratch = os.path.join(user_dir, SCRATCH_DIR_NAME)
        if os.path.isdir(scratch):
            def _on_err(fn, p, exc_info):
                app.logger.error(f"admin delete wipe_scratch: rmtree failed at {p} ({fn}): {exc_info[1]}")
            try:
                shutil.rmtree(scratch, onerror=_on_err)
                wiped.append("scratch")
                app.logger.info(f"admin delete: wiped scratch for {uid[:8]}...")
            except Exception as e:
                app.logger.error(f"admin delete: wipe_scratch raised for {uid[:8]}...: {e}")
    # 清 cache（无论是否 wipe，都把 user 从 cache 里去掉；用户下次登录会重新插）
    users = load_users_cache()
    users.pop(uid, None)
    save_users_cache(users)
    return jsonify({"ok": True, "wiped": wiped})


def _read_creds_from_container_env(container):
    """从已有容器的 attrs["Config"]["Env"] 提取凭据。

    跟用户自己 rebuild 的关键区别：admin 不知道 apiKey/baseUrl/models，
    但容器启动时这些值写进了 env（start_container() 在 environment= 里设的）。
    docker inspect 不发网络请求就拿得到 attrs —— 镜像被 rmi 也不影响。
    返回 (base_url, api_key, opus, sonnet, haiku)，缺任意一个就抛 RuntimeError。
    """
    env_list = (container.attrs.get("Config") or {}).get("Env") or []
    env = {}
    for kv in env_list:
        if "=" in kv:
            k, v = kv.split("=", 1)
            env[k] = v
    base_url = env.get("ANTHROPIC_BASE_URL", "").strip()
    api_key  = env.get("ANTHROPIC_API_KEY",  "").strip()
    opus     = env.get("ANTHROPIC_DEFAULT_OPUS_MODEL",   "").strip() \
            or env.get("ANTHROPIC_MODEL", "").strip()
    sonnet   = env.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "").strip()
    haiku    = env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL",  "").strip()
    missing = [n for n, v in (
        ("baseUrl", base_url), ("apiKey", api_key),
        ("opus", opus), ("sonnet", sonnet), ("haiku", haiku),
    ) if not v]
    if missing:
        raise RuntimeError(
            f"容器 env 缺关键字段: {', '.join(missing)}。"
            f"（可能是早期版本启动的容器，或容器已被外部修改 env）"
        )
    return base_url, api_key, opus, sonnet, haiku


@app.route("/api/admin/rebuild", methods=["POST"])
def api_admin_rebuild():
    """管理员强制重建某个用户的容器，复用用户已有的 apiKey/baseUrl/models。

    和用户自己 rebuild 的区别：admin 不需要 apiKey（也不应知道），
    直接从原容器的 env 读出来。代价：要求原容器存在（attrs["Config"]["Env"]
    在 stop/exit 后仍可读，无需容器在跑）。不存在 → 400，让 admin 引导用户
    重新登录。
    """
    _, err = require_admin_session()
    if err:
        return err
    if not client:
        return jsonify({"error": "docker unavailable"}), 503
    data = request.get_json(silent=True) or {}
    uid         = (data.get("userId")      or "").strip()
    reset_home  = bool(data.get("resetHome",    False))
    reset_scratch = bool(data.get("resetScratch", False))
    if not is_valid_user_id(uid):
        return jsonify({"error": "invalid userId"}), 400
    short = uid[:8]

    # 1) 必须先有容器才能从 env 拿凭据
    try:
        old = client.containers.get(f"claude-{uid}")
    except NotFound:
        return jsonify({
            "error": "用户容器不存在 —— 请先让用户从用户门户登录一次，再来重建。"
        }), 400
    except Exception as e:
        return jsonify({"error": f"查询容器失败: {e}"}), 500

    # 2) 从 env 读凭据
    try:
        base_url, api_key, opus, sonnet, haiku = _read_creds_from_container_env(old)
    except RuntimeError as e:
        app.logger.error(f"admin rebuild: read creds from env failed for {short}: {e}")
        return jsonify({"error": str(e)}), 400

    app.logger.info(
        f"admin rebuild: user={short}... reset_home={reset_home} "
        f"reset_scratch={reset_scratch} (creds from container env)"
    )

    # 3) 上限检查 —— 跟用户自己 rebuild 一样，排除自己的旧容器
    if MAX_ACTIVE_CONTAINERS > 0:
        active, _ = count_active_claude_containers()
        own_running = 1 if find_user_container(uid) else 0
        if (active - own_running) >= MAX_ACTIVE_CONTAINERS:
            app.logger.warning(
                f"admin rebuild rejected: active={active} (own={own_running}) "
                f">= MAX_ACTIVE_CONTAINERS={MAX_ACTIVE_CONTAINERS}"
            )
            return jsonify({
                "error": f"活跃容器已达上限 {MAX_ACTIVE_CONTAINERS}（当前 {active}）。"
                         f"请清理后再试。",
                "code": "MAX_ACTIVE_REACHED",
                "active": active,
                "limit":  MAX_ACTIVE_CONTAINERS,
            }), 429

    # 4) 重建容器（rebuild_container 内部已处理：stop+remove 旧 → ensure_user_dir(可选 wipe) → start）
    try:
        container, port, password = rebuild_container(
            uid, base_url, api_key, opus, sonnet, haiku,
            reset_home=reset_home, reset_scratch=reset_scratch,
        )
    except Exception as e:
        app.logger.error(f"admin rebuild: rebuild_container failed for {short}: {e}")
        return jsonify({"error": f"重建失败: {e}", "hint": "查看 ARCHITECTURE.md §10"}), 500

    # 5) 更新 cache（保留 displayName meta，admin rebuild 不改显示名）
    users = load_users_cache()
    users[uid] = {
        "container_id": container.id,
        "port":         port,
        "password":     password,
        "last_seen":    time.time(),
    }
    save_users_cache(users)
    # 注意：reset_home=True 会把 .portal_meta.json 一起删，displayName 丢失 —— 跟用户
    # 自己 rebuild 行为一致（admin rebuild 不主动重新落地 displayName，因为 admin 不知道
    # 也不应该改它）
    app.logger.info(f"admin rebuild done: user={short}... port={port}")
    return jsonify({
        "port":         port,
        "password":     password,
        "user_id":      uid,
        "display_name": _read_user_meta(uid).get("display_name", ""),
    })


@app.errorhandler(404)


@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "not found"}), 404
    return "Not Found", 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORTAL_HOST_PORT, debug=False)
