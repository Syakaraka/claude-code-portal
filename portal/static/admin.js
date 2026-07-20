/* Claude Code Portal — Admin 浏览器侧
 *
 * 鉴权：Flask session（签名 cookie，浏览器自动带）。
 * 后端不存任何 token —— 服务器每个 worker 用同一个 secret_key 解码 cookie → 跨 worker 无状态。
 * 401 → 自动回到登录页。
 */

const loginCard   = document.getElementById("login-card");
const loginForm   = document.getElementById("login-form");
const loginBtn    = document.getElementById("login-btn");
const loginStatus = document.getElementById("login-status");
const listCard    = document.getElementById("list-card");
const rowsEl      = document.getElementById("rows");
const listMeta    = document.getElementById("list-meta");
const listStatus  = document.getElementById("list-status");
const statusPill  = document.getElementById("status-pill");
const statusText  = document.getElementById("status-text");

function setPill(state, label) {
    statusPill.className = "status-pill" + (state ? " is-" + state : "");
    statusText.textContent = label;
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
}

function setStatus(el, msg, type = "") {
    el.className = "status" + (type ? " is-" + type : "");
    el.textContent = msg;
    if (msg) el.classList.add("is-visible");
}
function clearStatus(el) {
    el.className = "status";
    el.textContent = "";
}

// fetch 包装：401 自动回登录（cookie 自动带，不需手动加 header）
// _fromInterval=true 时（自动刷新触发的 401），显示"会话已过期"——
// 首次访问无 cookie 的 401 不显示（不是过期，是从来没登录过）
async function authFetch(path, opts = {}, _fromInterval = false) {
    const resp = await fetch(path, Object.assign({ credentials: "same-origin" }, opts));
    if (resp.status === 401) {
        showLogin(_fromInterval ? "会话已过期，请重新登录" : null);
        return { error: "unauthorized" };
    }
    return resp;
}

function timeAgo(epoch) {
    if (!epoch) return "—";
    const s = Math.max(0, Math.floor((Date.now() / 1000) - epoch));
    if (s < 60)    return `${s}s ago`;
    if (s < 3600)  return `${Math.floor(s/60)}m ago`;
    if (s < 86400) return `${Math.floor(s/3600)}h ago`;
    return `${Math.floor(s/86400)}d ago`;
}

function showLogin(errMsg) {
    loginCard.hidden = false;
    listCard.hidden  = true;
    document.getElementById("logout-link").hidden = true;
    setPill("", "SETUP");
    if (errMsg) setStatus(loginStatus, errMsg, "error");
    else        clearStatus(loginStatus);
}

function showList() {
    loginCard.hidden = true;
    listCard.hidden  = false;
    document.getElementById("logout-link").hidden = false;
    setPill("ready", "AUTHED");
}

// 退出登录：调 /api/admin/logout 清 session
document.getElementById("logout-link").addEventListener("click", async (e) => {
    e.preventDefault();
    await authFetch("/api/admin/logout", { method: "POST" });
    showLogin();
});

loginForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(loginForm);
    const password = (fd.get("password") || "").toString();
    loginBtn.disabled = true;
    setStatus(loginStatus, "验证中…");
    try {
        const resp = await fetch("/api/admin/login", {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ password }),
        });
        const data = await resp.json();
        if (!resp.ok || data.error) {
            setStatus(loginStatus, `❌ ${data.error || resp.statusText}`, "error");
            return;
        }
        clearStatus(loginStatus);
        showList();
        refresh();
    } catch (e) {
        setStatus(loginStatus, `❌ 网络错误：${e.message}`, "error");
    } finally {
        loginBtn.disabled = false;
        loginForm.password.value = "";
    }
});

async function refresh() {
    const resp = await authFetch("/api/admin/containers");
    if (!resp || resp.error) return;
    let data;
    try { data = await resp.json(); } catch (e) { return; }
    if (!resp.ok || data.error) {
        setStatus(listStatus, `加载失败：${data.error || resp.statusText}`, "error");
        return;
    }
    clearStatus(listStatus);
    renderRows(data.containers || [], {
        active: data.active,
        limit:  data.limit,
    });
}

function renderRows(items, meta) {
    const active = meta?.active ?? items.filter(c => (c.status||"").toLowerCase() === "running").length;
    const limit  = meta?.limit  ?? 0;
    const limitStr = limit > 0 ? `${active} / ${limit}` : `${active}`;
    listMeta.textContent = `活跃 ${limitStr} · 共 ${items.length}`;
    if (!items.length) {
        rowsEl.innerHTML = `
            <div class="empty">
                <div class="big">暂无容器</div>
                <div>等用户从用户门户登录后会出现条目。</div>
            </div>`;
        return;
    }
    rowsEl.innerHTML = items.map(c => rowHtml(c)).join("");

    rowsEl.querySelectorAll("[data-action]").forEach(btn => {
        btn.addEventListener("click", () => handleAction(btn));
    });
}

function rowHtml(c) {
    const status = (c.status || "unknown").toLowerCase();
    const tagCls = ["running","exited","stopped","other"].includes(status)
        ? status : "other";
    const name = (c.display_name || "").trim();
    // displayName 已改为必填——旧数据（升级前建的用户）可能没有，正常显示空槽
    const nameHtml = name
        ? escapeHtml(name)
        : `<span class="is-empty">（旧数据未填）</span>`;
    // port cell：实际绑定端口 + 固定端口状态标签
    //   - 端口空 → "—"（exited 容器没有 host port）
    //   - 没有 .port（升级前用户）→ 仅显示端口号（无标记）
    //   - pinned_port === port → "9905 ✓"（绿色，自上次启动以来稳定）
    //   - pinned_port !== port → "9905 ⚠"（rust 色，原本固定到 X 被占用，自动重摇）
    let portCell;
    if (c.port == null) {
        portCell = `<span class="muted">—</span>`;
    } else if (c.pinned_port == null) {
        portCell = `${c.port}`;
    } else if (c.pinned_port === c.port) {
        portCell = `${c.port}<span class="pin-tag pin-ok" title="固定端口 .port=${c.port}（每次启动都回归此端口）">✓</span>`;
    } else {
        portCell = `${c.port}<span class="pin-tag pin-warn" title=".port=${c.pinned_port} 已被其他容器占用，本次自动重摇到 ${c.port}">⚠</span>`;
    }
    return `
        <div class="trow" data-uid="${escapeHtml(c.user_id)}">
            <span class="uid" title="${escapeHtml(c.user_id)}">${escapeHtml(c.user_id)}</span>
            <span class="name ${name ? "" : "is-empty"}" title="${escapeHtml(name)}">${nameHtml}</span>
            <span class="port">${portCell}</span>
            <span><span class="tag ${tagCls}">${escapeHtml(c.status || "—")}</span></span>
            <span class="age">${escapeHtml(timeAgo(c.last_seen))}</span>
            <span class="actions">
                ${status === "running" ? `<button class="btn btn-ghost btn-sm" data-action="stop">停止</button>` : ""}
                <button class="btn btn-warn btn-sm" data-action="delete-toggle">删除 ▼</button>
            </span>
            <div class="row-panel">
                <div class="opts-title">⚠ 删除选项</div>
                <div class="opts">
                    <label>
                        <input type="checkbox" data-opt="wipeHome">
                        <span>
                            <div class="opt-title">同时清空用户目录</div>
                            <div class="opt-desc">删除 volumes/users/&lt;uid&gt;/ 全部数据（设置、扩展、对话缓存、显示名称 meta、固定端口 .port）。</div>
                        </span>
                    </label>
                    <label>
                        <input type="checkbox" data-opt="wipeScratch">
                        <span>
                            <div class="opt-title">同时清空临时目录</div>
                            <div class="opt-desc">删除 volumes/users/&lt;uid&gt;/scratch 全部数据（仅临时区，保留 settings/扩展、.port）。</div>
                        </span>
                    </label>
                </div>
                <div class="opts-actions">
                    <button class="btn btn-ghost btn-sm" data-action="cancel">取消</button>
                    <button class="btn btn-warn btn-sm" data-action="delete-confirm">确认删除</button>
                </div>
            </div>
        </div>`;
}

async function handleAction(btn) {
    const row = btn.closest(".trow");
    const uid = row.dataset.uid;
    const action = btn.dataset.action;
    if (action === "stop") {
        if (!confirm(`停止容器 claude-${uid}?`)) return;
        btn.disabled = true;
        setStatus(listStatus, `停止 ${uid.slice(0,8)}…`);
        const resp = await authFetch("/api/admin/stop", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ userId: uid }),
        });
        try {
            const data = await resp.json();
            if (!resp.ok || data.error) {
                setStatus(listStatus, `停止失败：${data.error || resp.statusText}`, "error");
            } else {
                setStatus(listStatus, `已停止 ${uid.slice(0,8)}`, "success");
            }
        } finally {
            btn.disabled = false;
            refresh();
        }
        return;
    }
    if (action === "delete-toggle") {
        // 展开 / 收起删除选项面板（同一行的二级确认）
        row.classList.toggle("expanded");
        return;
    }
    if (action === "cancel") {
        row.classList.remove("expanded");
        return;
    }
    if (action === "delete-confirm") {
        const wipeHome    = !!row.querySelector('[data-opt="wipeHome"]').checked;
        const wipeScratch = !!row.querySelector('[data-opt="wipeScratch"]').checked;
        const what = wipeHome    ? "容器 + 用户目录" :
                     wipeScratch ? "容器 + 临时目录" : "仅容器";
        if (!confirm(`删除 ${uid.slice(0,8)}（${what}）？用户可重新登录拉起新容器。`)) return;
        const confirmBtn = row.querySelector('[data-action="delete-confirm"]');
        if (confirmBtn) confirmBtn.disabled = true;
        setStatus(listStatus, `删除 ${uid.slice(0,8)}…`);
        const resp = await authFetch("/api/admin/delete", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ userId: uid, wipeHome, wipeScratch }),
        });
        try {
            const data = await resp.json();
            if (!resp.ok || data.error) {
                setStatus(listStatus, `删除失败：${data.error || resp.statusText}`, "error");
            } else {
                setStatus(listStatus, `已删除 ${uid.slice(0,8)}`, "success");
                row.classList.remove("expanded");
            }
        } finally {
            if (confirmBtn) confirmBtn.disabled = false;
            refresh();
        }
        return;
    }
}

// 启动：先尝试拉一次列表（带 cookie）—— 200 说明 session 有效，
// 401 说明没登录或过期，直接显示登录卡。这样跨刷新 / 跨标签页也保持登录。
// 首次访问没 cookie 的 401 不显示错误消息（不是过期，是从来没登录过）。
(function bootstrap() {
    refresh().then(() => {
        // refresh() 内部已经根据 resp.status 切换了 loginCard / listCard
        // —— 这里什么也不用做
    });
})();

// 自动刷新（5s）—— 仅在已登录状态刷新；带 _fromInterval=true 让 401 显示"会话已过期"
setInterval(() => {
    if (listCard.hidden) return;  // 登录卡显示中别刷
    authFetch("/api/admin/containers", {}, /*_fromInterval=*/true)
        .then(resp => {
            if (!resp || resp.error) return;
            return resp.json().then(data => {
                if (resp.ok && data && !data.error) {
                    clearStatus(listStatus);
                    renderRows(data.containers || [], {
                        active: data.active,
                        limit:  data.limit,
                    });
                }
            });
        });
}, 5000);