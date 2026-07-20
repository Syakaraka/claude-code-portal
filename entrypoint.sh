#!/bin/sh
# Claude 容器入口脚本
#
# 职责：
#   1. 装 /tmp/extensions/*.vsix（构建时 COPY 进来的本地扩展包）
#   2. 给 Claude Code 扩展里的 native binary 补可执行位（vendor vsix 内权限位
#      在 COPY / 解压时可能丢失，audio-capture.node 原本就是 0644，必须 +x 才能 load）
#   3. 写 User/settings.json：
#        - 禁用 workspace trust（避免默认 Restricted Mode 把扩展功能阉割）
#        - 预信任关键 publisher（Claude Code、MS-CEINTL 中文语言包），
#          否则 Restricted Mode 下它们不能激活，UI 永远英文
#   4. 写 User/argv.json（locale=zh-cn，code-server 的 --locale CLI 不会透传给 vscode 内核，
#      vscode 实际读 argv.json > cli flag > cookie > accept-language 这个顺序）
#      写 userDataDir/languagepacks.json（zh-cn → ms-ceintl 扩展的 main.i18n.json 绝对路径，
#      zp() 用 languageId 作 key 查这里；写错位置或写错 key → UI 永远英文）
#   5. 生成 multi-root workspace.code-workspace（/workspace 共享代码库 +
#      /home/node/scratch per-user 临时区）作为默认打开对象，让 Claude Code 的 CWD
#      保持在 /workspace 但 VS Code 同时能浏览/编辑 scratch 区
#   6. 把 CERT_FILE / KEY_FILE env 拼到 code-server 启动参数里
#      （portal 会动态生成带正确 SAN 的证书 bind 进来；env 没设就用镜像默认 cert）
#   7. exec code-server "$@"（用 exec 让自己被 code-server 取代，保证 PID 1 信号处理）
#
# 为什么不在 Dockerfile 里装：code-server v4.129.0 不再支持 config.yaml 里的
# extensions-gallery 字段，且默认 Open VSX 在国内构建环境拉不到；从本地 .vsix 装
# 可以在容器启动时离线完成，首启稍慢但稳。
#
# locale 通过 CMD --locale=zh-cn 传（code-server 4.x 不读 settings.json 的 locale 字段，
# VSCODE_LOCALE env 也不生效，详见 https://github.com/coder/code-server/issues/4333）。
# 但 CLI flag 不会透传给 vscode 内核，所以也写 argv.json 作为 file-based 兜底。
set -e

# 装扩展（已装则跳过；失败仅警告，不影响 code-server 启动）
if [ -d /tmp/extensions ] && ls /tmp/extensions/*.vsix >/dev/null 2>&1; then
    echo "[entrypoint] installing local VS Code extensions:"
    for vsix in /tmp/extensions/*.vsix; do
        echo "  - $(basename "$vsix")"
        code-server --install-extension "$vsix" \
            || echo "  WARN: failed to install $(basename "$vsix") (continuing)"
    done
    rm -rf /tmp/extensions

    # Claude Code 扩展内的 native binary 补可执行位（vendor .vsix 内权限位在
    # docker COPY / code-server 解压时都可能丢失；audio-capture.node 在 zip 内
    # 就是 0644，必须 +x 才能作为 node 原生模块加载）
    # 注：vsix 解压后扩展根布局是 anthropic.claude-code-X/resources/...（没有
    # 多余的 extension/ 层——VSIX manifest 把 extension/ 目录里的内容视为包体）
    claude_ext_root="$HOME/.local/share/code-server/extensions"
    if [ -d "$claude_ext_root" ]; then
        for f in \
            "$claude_ext_root"/anthropic.claude-code-*/resources/native-binary/claude \
            "$claude_ext_root"/anthropic.claude-code-*/resources/audio-capture/*/audio-capture.node; do
            if [ -f "$f" ]; then
                chmod +x "$f" && echo "[entrypoint] chmod +x: $(basename "$f")"
            fi
        done
    fi
else
    echo "[entrypoint] no /tmp/extensions/*.vsix found, skipping extension install"
fi

# 写 settings.json：禁用 workspace trust + 预信任 Claude Code / MS-CEINTL
# 关键原因：code-server 默认 Restricted Mode 下：
#   - anthropic.claude-code publisher untrusted → Claude Code 不能激活 → 打不开
#   - ms-ceintl.vscode-language-pack-zh-hans 同理 → --locale=zh-cn 也没用，UI 永远英文
# 禁掉 workspace trust 是最干净的解法（内部工具，没必要弹 trusted workspace 提示）
# 已存在则不动（保留用户手动改过的设置）
cs_user_dir="$HOME/.local/share/code-server/User"
cs_settings="$cs_user_dir/settings.json"
mkdir -p "$cs_user_dir"
if [ ! -f "$cs_settings" ]; then
    cat > "$cs_settings" <<'EOF'
{
    "security.workspace.trust.enabled": false,
    "extensions.supportUntrustedWorkspaces": {
        "anthropic.claude-code": true,
        "ms-ceintl.vscode-language-pack-zh-hans": true
    },
    "extensions.autoUpdate": false
}
EOF
    echo "[entrypoint] wrote User/settings.json (workspace trust disabled, publishers pre-trusted)"
fi

# 写 argv.json：vscode 启动时按这个文件的 locale 字段决定 UI 语言
# 关键：code-server 自己的 --locale CLI 不会透传给 vscode（见 cli.js 的 toCodeArgs，
# 只透传 help/version/port/log 四个字段）。vscode 内核是按 argv.json > cli flag >
# cookie > accept-language 这个顺序决定 locale 的（server-main.js _handleRoot）。
# CLI 第一个优先级没生效，但 argv.json 是 file-based，code-server 自己读 —— 写好就行。
# argvResource = appSettingsHome + "argv.json" = userDataDir + "User/argv.json"。
# 已存在则不动（用户可能手动改过语言）
cs_argv="$cs_user_dir/argv.json"
if [ ! -f "$cs_argv" ]; then
    cat > "$cs_argv" <<'EOF'
{
    "locale": "zh-cn"
}
EOF
    echo "[entrypoint] wrote User/argv.json (locale=zh-cn)"
fi

# 写 languagepacks.json：vscode 启动时 zp() 读 <userDataDir>/languagepacks.json
# （注意是 userDataDir 根，不是 User/ 子目录）按这里指向的翻译文件加载中文 UI。
# 关键点：key 必须是语言 ID（"zh-cn"），不是扩展 ID —— KW(i, locale) 拿 user locale
# 当 key 去查对象，没查到就回退英文。
# 路径里也得用绝对路径（zp() 检查 file exists，扩展内相对路径解析不到）。
# ms-ceintl.vscode-language-pack-zh-hans 扩展虽然装了，但 code-server 4.x 在某些场景
# 不会把它加载到 exthost（languagePacks service 不会触发 update() 写文件），
# → 这里手动写一份兜底，code-server 启动后会自己用 hash-tagged 版本覆盖（同样能用）。
# 已存在则不动（用户可能手动改过语言）
cs_data_dir="$HOME/.local/share/code-server"
cs_langpacks="$cs_data_dir/languagepacks.json"
if [ ! -f "$cs_langpacks" ]; then
    zh_ext=$(ls -d "$cs_data_dir/extensions"/ms-ceintl.vscode-language-pack-zh-hans-* 2>/dev/null | head -1)
    if [ -n "$zh_ext" ] && [ -f "$zh_ext/translations/main.i18n.json" ]; then
        cat > "$cs_langpacks" <<EOF
{
    "zh-cn": {
        "hash": "manual",
        "translations": {
            "vscode": "$zh_ext/translations/main.i18n.json"
        }
    }
}
EOF
        echo "[entrypoint] wrote $cs_langpacks (zh-cn manual override, ext=$zh_ext)"
    else
        echo "[entrypoint] WARN: zh-hans language pack not found at \$cs_data_dir/extensions/ms-ceintl.*"
    fi
fi

# 容器内路径都从环境变量读（portal 通过 docker.run env 注入），默认值匹配当前镜像
# 改了 CLAUDE_*_BIND 必须重建 claude-code 镜像（entrypoint.sh 是镜像内的）
CLAUDE_WORKSPACE_BIND="${CLAUDE_WORKSPACE_BIND:-/workspace}"
CLAUDE_HOME_BIND="${CLAUDE_HOME_BIND:-/home/node}"
CLAUDE_SCRATCH_BIND="${CLAUDE_SCRATCH_BIND:-/home/node/scratch}"
CLAUDE_SCRATCH_DIR_NAME="${CLAUDE_SCRATCH_DIR_NAME:-scratch}"
CLAUDE_WORKSPACE_FILE_NAME="${CLAUDE_WORKSPACE_FILE_NAME:-workspace.code-workspace}"

# Multi-root workspace：把 CLAUDE_WORKSPACE_BIND（RO 共享代码） +
# CLAUDE_SCRATCH_BIND（per-user 临时区）一起打开为一个 VS Code workspace。
# CLAUDE_WORKSPACE_BIND 放第一位 → 它是 primary root → VS Code integrated terminal
# CWD = CLAUDE_WORKSPACE_BIND → Claude Code 的工具调用以共享代码为 context。
# CLAUDE_SCRATCH_BIND 是 per-user bind mount 的（portal 启动前已 mkdir + chmod 0777）。
# **不在容器内 chmod 1777**：portal 创建的目录 owner=root，node 用户无权 chmod → 容器会 Exited(1)。
# sticky bit 在 per-user 容器里也没意义（每个容器只有一个 uid=1000 用户访问）。
# 想要 sticky bit 在 portal 端改 ensure_user_workspace，chown 给 node 用户后再 chmod。
# workspace_file 已存在则不动（用户可能手动加过第三个根或自定义了 settings）
# scratch 路径推导：CLAUDE_SCRATCH_BIND 默认是 CLAUDE_HOME_BIND/scratch_dir_name
# → 反推 CLAUDE_HOME_BIND 下哪个子目录；为简化，假定 CLAUDE_SCRATCH_BIND 是
# CLAUDE_HOME_BIND 下的子目录（与现状一致）
scratch_path="$CLAUDE_SCRATCH_BIND"
if [ "$CLAUDE_SCRATCH_BIND" = "$CLAUDE_HOME_BIND/$CLAUDE_SCRATCH_DIR_NAME" ]; then
    # 默认情况：scratch 在 home 下，用 $HOME/scratch 保持 portable
    scratch_path="$HOME/$CLAUDE_SCRATCH_DIR_NAME"
fi

workspace_file="$HOME/$CLAUDE_WORKSPACE_FILE_NAME"
if [ ! -f "$workspace_file" ]; then
    mkdir -p "$HOME/$CLAUDE_SCRATCH_DIR_NAME"
    cat > "$workspace_file" <<EOF
{
    "folders": [
        { "path": "$CLAUDE_WORKSPACE_BIND", "name": "Workspace" },
        { "path": "$scratch_path",           "name": "Scratch" }
    ],
    "settings": {
        "security.workspace.trust.enabled": false
    }
}
EOF
    echo "[entrypoint] wrote $workspace_file (multi-root: $CLAUDE_WORKSPACE_BIND + $scratch_path)"
fi

# HTTPS 证书路径：env 没设就用镜像默认的（CN=localhost，仅适合 localhost 访问）
# portal 会在每个用户容器启动前生成带正确 SAN 的证书，覆盖到 CLAUDE_CERT_DIR_BIND
# 然后通过 CERT_FILE / KEY_FILE env 传进来（也由 portal 拼好绝对路径）
CERT_FILE="${CERT_FILE:-$CLAUDE_HOME_BIND/cert.pem}"
KEY_FILE="${KEY_FILE:-$CLAUDE_HOME_BIND/key.pem}"
# 如果 CERT_FILE / KEY_FILE 没被 portal 注入，回退到 /etc/code-server 镜像默认
if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
    CERT_FILE="/etc/code-server/cert.pem"
    KEY_FILE="/etc/code-server/key.pem"
fi

# 把 cert 参数拼到 CMD 里（证书存在才加，避免 default cert 缺失导致启动失败时阻塞调试）
cert_args=""
if [ -f "$CERT_FILE" ] && [ -f "$KEY_FILE" ]; then
    cert_args="--cert $CERT_FILE --cert-key $KEY_FILE"
    echo "[entrypoint] using cert: $CERT_FILE"
else
    echo "[entrypoint] WARN: cert not found at $CERT_FILE / $KEY_FILE, starting without HTTPS"
fi

echo "[entrypoint] starting code-server: code-server $* $cert_args"
# shellcheck disable=SC2086
exec code-server "$@" $cert_args