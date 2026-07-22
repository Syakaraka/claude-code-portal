/* Claude Code Portal — 浏览器侧（向导状态机） */

const STORAGE_KEY       = "claude-code-creds";
const CREDS_HASH_KEY    = "claude-code-creds-hash";   // 上次启动用的凭据指纹，用于检测改动
const PASSWORD_MAP      = "claude-code-passwords";
const CA_INSTALLED_KEY  = "claude-code-ca-installed";

// ---- DOM ----
const page          = document.getElementById("page");
const form          = document.getElementById("login-form");
const submitBtn     = document.getElementById("submit-btn");
const submitLabel   = document.getElementById("submit-label");
const resetLink     = document.getElementById("reset-link");
const ticketIdEl    = document.getElementById("ticket-id");
const ticketNameEl  = document.getElementById("ticket-name");
const ticketNameSepEl = document.querySelector(".ticket-name-sep");
const statusPill    = document.getElementById("status-pill");
const statusText    = document.getElementById("status-text");
const statusEl      = document.getElementById("status");
const statusEl2     = document.getElementById("status-step2");
const stepEls       = document.querySelectorAll(".step-rail .step");
const cardEls       = document.querySelectorAll(".workorder .card[data-step]");
const resultUrlEl   = document.getElementById("result-url");
const resultPwdEl   = document.getElementById("result-password");
const openLineEl    = document.getElementById("open-line");
const openLinkEl    = document.getElementById("open-link");
const editLinkEl    = document.getElementById("edit-link");
const renameToggle  = document.getElementById("rename-toggle");
const renamePanel   = document.getElementById("rename-panel");
const renameInput   = document.getElementById("rename-input");
const renameSave    = document.getElementById("rename-save");
const renameCancel  = document.getElementById("rename-cancel");
const rebuildToggle = document.getElementById("rebuild-toggle");
const rebuildPanel  = document.getElementById("rebuild-panel");
const rebuildConfirm= document.getElementById("rebuild-confirm");
const rebuildCancel = document.getElementById("rebuild-cancel");
const resetHomeEl    = document.getElementById("reset-home");
const syncTemplateEl = document.getElementById("sync-template");
const resetScratchEl = document.getElementById("reset-scratch");
const caBanner      = document.getElementById("ca-banner");
const caCb          = document.getElementById("ca-installed");
const footHost      = document.getElementById("foot-host");

footHost.textContent = `host: ${window.location.hostname}`;

// ---- helpers ----
function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
}

function setStatus(targetEl, html, type = "") {
    targetEl.className = "status" + (type ? " is-" + type : "");
    targetEl.innerHTML = html;
    if (html) targetEl.classList.add("is-visible");
}

function clearStatus(targetEl) {
    targetEl.className = "status";
    targetEl.innerHTML = "";
}

function setPill(state, label) {
    statusPill.className = "status-pill" + (state ? " is-" + state : "");
    statusText.textContent = label;
}

function setStep(n) {
    stepEls.forEach(el => el.classList.toggle("is-active", el.dataset.step == n));
    cardEls.forEach(el => { el.hidden = el.dataset.step != n; });
}

function setTicketId(uid) {
    ticketIdEl.textContent = uid ? `TICKET #${uid.slice(0,8)}` : "TICKET #—";
}

function setTicketName(name) {
    const trimmed = (name || "").trim();
    if (trimmed) {
        ticketNameEl.textContent = trimmed;
        ticketNameEl.title = trimmed;
        ticketNameEl.hidden = false;
        ticketNameSepEl.hidden = false;
    } else {
        ticketNameEl.textContent = "";
        ticketNameEl.title = "";
        ticketNameEl.hidden = true;
        ticketNameSepEl.hidden = true;
    }
}

function loadCreds() {
    try {
        const c = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
        if (c && c.baseUrl && c.apiKey && c.opusModel && c.sonnetModel && c.haikuModel) return c;
    } catch (e) {}
    return null;
}

function saveCreds(c) {
    try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(c));
        localStorage.setItem(CREDS_HASH_KEY, hashCreds(c));
    } catch (e) {}
}

function clearCreds() {
    localStorage.removeItem(STORAGE_KEY);
    localStorage.removeItem(CREDS_HASH_KEY);
    // PASSWORD_MAP 不删 —— 用户重新输入 apiKey 后密码一样，留着方便查阅
}

function hashCreds(c) {
    // 用于检测"凭据是否改了"，稳定排序后 hash
    const stable = ["baseUrl", "apiKey", "opusModel", "sonnetModel", "haikuModel"]
        .map(k => `${k}=${c[k] || ""}`).join("|");
    let h = 0;
    for (let i = 0; i < stable.length; i++) {
        h = ((h << 5) - h) + stable.charCodeAt(i);
        h |= 0;
    }
    return String(h);
}

function savedCredsHash() {
    return localStorage.getItem(CREDS_HASH_KEY) || "";
}

function credsEqual(a, b) {
    return hashCreds(a) === hashCreds(b);
}

function savePassword(userId, pwd) {
    try {
        const m = JSON.parse(localStorage.getItem(PASSWORD_MAP) || "{}");
        m[userId] = pwd;
        localStorage.setItem(PASSWORD_MAP, JSON.stringify(m));
    } catch (e) {}
}

// ---- CA banner ----
function syncCaBanner() {
    if (!caBanner) return;
    const installed = localStorage.getItem(CA_INSTALLED_KEY) === "1";
    page.classList.toggle("is-ca-done", installed);
}
if (caCb) {
    caCb.addEventListener("change", () => {
        if (caCb.checked) {
            localStorage.setItem(CA_INSTALLED_KEY, "1");
            syncCaBanner();
        }
    });
}
syncCaBanner();

// ---- submit ----
async function handleSubmit(creds, isResubmit) {
    submitBtn.disabled = true;
    const originalLabel = submitLabel.textContent;
    submitLabel.textContent = "启动中…";

    // 决定走哪个端点：凭据变了 → rebuild，否则 start（start 会复用现有容器）
    const sameAsSaved = credsEqual(creds, loadCreds() || {});
    // 如果是 wizard 上 step1→step2 流程（页面刷新或空 savedCreds 但用户点击了启动），
    // savedCreds 总是等于当前输入 → 走 start；如果是 step1 "保存修改" 流程（用户从 step2
    // 点 "回到上一步" 后改了内容再提交），可能不等 → 走 rebuild。
    const endpoint = (isResubmit && !sameAsSaved) ? "/api/rebuild" : "/api/start";
    const resetHome    = rebuildPanel.classList.contains("is-open") && resetHomeEl.checked;
    const syncTemplate = rebuildPanel.classList.contains("is-open") && syncTemplateEl.checked;
    const resetScratch = rebuildPanel.classList.contains("is-open") && resetScratchEl.checked;

    setPill("working", "STARTING");
    setStatus(statusEl, '<span class="spinner"></span>正在启动容器（首次约 5–10 秒）…');

    try {
        const resp = await fetch(endpoint, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                baseUrl:     creds.baseUrl,
                apiKey:      creds.apiKey,
                opusModel:   creds.opusModel,
                sonnetModel: creds.sonnetModel,
                haikuModel:  creds.haikuModel,
                displayName: creds.displayName || "",
                resetHome, syncTemplate, resetScratch,
            }),
        });
        const data = await resp.json();

        if (!resp.ok || data.error) {
            setStatus(statusEl, `❌ 启动失败：${data.error || resp.statusText}`, "error");
            setPill("error", "ERROR");
            submitBtn.disabled = false;
            submitLabel.textContent = originalLabel;
            return;
        }

        const { port, password, user_id, display_name } = data;
        // 用后端返回的 display_name 校正本地（后端会做 sanitize）
        creds.displayName = display_name || "";
        savePassword(user_id, password);
        saveCreds(creds);   // 写 hash
        setTicketId(user_id);
        setTicketName(display_name);
        showStep2({ port, password, user_id, display_name, reused: !isResubmit && endpoint === "/api/start" && data.reused });
    } catch (e) {
        setStatus(statusEl, `❌ 网络错误：${e.message}`, "error");
        setPill("error", "ERROR");
        submitBtn.disabled = false;
        submitLabel.textContent = originalLabel;
    }
}

function showStep2({ port, password, user_id, display_name }) {
    const targetUrl = `https://${window.location.hostname}:${port}/`;
    resultUrlEl.textContent = targetUrl;
    resultPwdEl.textContent = password;
    openLineEl.textContent  = targetUrl;
    openLinkEl.href         = targetUrl;

    setStep(2);
    setPill("ready", "READY");
    setTicketId(user_id);
    setTicketName(display_name);
    if (renameInput) renameInput.value = display_name || "";
    clearStatus(statusEl);
    clearStatus(statusEl2);
    // 折叠两个面板
    rebuildPanel.classList.remove("is-open");
    if (renamePanel) renamePanel.hidden = true;
    resetHomeEl.checked = false;
    syncTemplateEl.checked = false;
    resetScratchEl.checked = false;
    submitBtn.disabled = false;
    submitLabel.textContent = "保存修改";  // 在 step2 状态下，submit 不再可见 —— 这里只是防御

    // 自动复制密码（用户最常用的下一步操作）
    setTimeout(async () => {
        try { await navigator.clipboard.writeText(password); } catch (e) {}
    }, 100);
}

function showStep1(prefill) {
    if (prefill) {
        form.baseUrl.value     = prefill.baseUrl     || "";
        form.apiKey.value      = prefill.apiKey      || "";
        form.opusModel.value   = prefill.opusModel   || "claude-opus-4";
        form.sonnetModel.value = prefill.sonnetModel || "claude-sonnet-4";
        form.haikuModel.value  = prefill.haikuModel  || "claude-haiku-4";
        if (form.displayName) form.displayName.value = prefill.displayName || "";
    } else {
        // 加载已保存
        const c = loadCreds();
        if (c) {
            form.baseUrl.value     = c.baseUrl     || "";
            form.apiKey.value      = c.apiKey      || "";
            form.opusModel.value   = c.opusModel   || "claude-opus-4";
            form.sonnetModel.value = c.sonnetModel || "claude-sonnet-4";
            form.haikuModel.value  = c.haikuModel  || "claude-haiku-4";
            if (form.displayName) form.displayName.value = c.displayName || "";
        }
    }
    setStep(1);
    setPill("warn", "EDIT");
    submitBtn.disabled = false;
    submitLabel.textContent = "启动 / 进入";
    rebuildPanel.classList.remove("is-open");
    clearStatus(statusEl);
    clearStatus(statusEl2);
}

// ---- copy buttons ----
// 跨环境复制（HTTP portal / HTTPS code-server 都行）：
//   - secure context（HTTPS / localhost）→ navigator.clipboard.writeText
//   - 非 secure context（HTTP IP）→ 隐藏 textarea + document.execCommand("copy")
// 旧实现只用 navigator.clipboard，在 HTTP 上静默失败 → 用户看到 "已复制 ✓"
// 但实际啥也没复制。execCommand 是 deprecated 但所有浏览器仍支持。
async function copyToClipboard(text) {
    // 1) 现代 API（secure context）
    try {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(text);
            return true;
        }
    } catch (e) { /* fall through */ }
    // 2) Fallback：textarea + execCommand
    try {
        const ta = document.createElement("textarea");
        ta.value = text;
        // 移出视口 + 不可聚焦：避免页面跳动 / 抢焦点
        ta.style.position = "fixed";
        ta.style.top = "0";
        ta.style.left = "-9999px";
        ta.setAttribute("readonly", "");
        document.body.appendChild(ta);
        ta.select();
        ta.setSelectionRange(0, text.length);
        const ok = document.execCommand("copy");
        document.body.removeChild(ta);
        return ok;
    } catch (e) {
        return false;
    }
}

document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-copy]");
    if (!btn) return;
    const targetId = btn.getAttribute("data-copy");
    const el = document.getElementById(targetId);
    if (!el) return;
    const val = el.textContent || el.value || "";
    const ok = await copyToClipboard(val);
    btn.classList.add("is-copied");
    const orig = btn.textContent;
    btn.textContent = ok ? "已复制 ✓" : "复制失败 — 手动选";
    setTimeout(() => {
        btn.classList.remove("is-copied");
        btn.textContent = orig;
    }, 1600);
});

// ---- 导航 / 重建按钮 ----
editLinkEl.addEventListener("click", () => {
    showStep1(/*prefill=*/loadCreds() || null);
});

rebuildToggle.addEventListener("click", () => {
    rebuildPanel.classList.toggle("is-open");
});

rebuildCancel.addEventListener("click", () => {
    rebuildPanel.classList.remove("is-open");
    resetHomeEl.checked = false;
    syncTemplateEl.checked = false;
    resetScratchEl.checked = false;
});

// 重建选项三选一互斥（reset_home ⊃ sync_template ⊃ reset_scratch 的包含关系）：
//   - 勾 reset_home → 取消 sync_template + reset_scratch（reset_home 是超集）
//   - 勾 sync_template → 取消 reset_scratch（不影响 reset_home，但同时勾了 reset_home 也无意义）
//   - 勾 reset_scratch → 不自动取消别的（reset_scratch 是独立的细粒度选项）
// 这里实现成"勾一个就把另外两个清掉"，避免用户以为三个能同时生效。
function _applyRebuildOptMutex(justChanged) {
    if (justChanged === "reset-home" && resetHomeEl.checked) {
        syncTemplateEl.checked = false;
        resetScratchEl.checked = false;
    } else if (justChanged === "sync-template" && syncTemplateEl.checked) {
        resetHomeEl.checked = false;
        // reset_scratch 跟 sync_template 语义不冲突（一个动 home，一个动 scratch），
        // 但 UI 上三选一更清晰：勾 sync_template 也清掉 reset_scratch
        resetScratchEl.checked = false;
    } else if (justChanged === "reset-scratch" && resetScratchEl.checked) {
        // reset_scratch 可以独立勾（保留 home，只清 scratch）
        // 但跟 reset_home 重复 → 用户勾了 reset_home 就别让 reset_scratch 留
        resetHomeEl.checked = false;
        syncTemplateEl.checked = false;
    }
}
resetHomeEl.addEventListener("change",    () => _applyRebuildOptMutex("reset-home"));
syncTemplateEl.addEventListener("change", () => _applyRebuildOptMutex("sync-template"));
resetScratchEl.addEventListener("change", () => _applyRebuildOptMutex("reset-scratch"));

// ---- rename panel ----
if (renameToggle) {
    renameToggle.addEventListener("click", () => {
        if (!renamePanel) return;
        const willOpen = renamePanel.hidden;
        renamePanel.hidden = !willOpen;
        if (willOpen) {
            const c = loadCreds();
            renameInput.value = (c && c.displayName) || "";
            setTimeout(() => renameInput.focus(), 0);
        }
    });
}
if (renameCancel) {
    renameCancel.addEventListener("click", () => {
        renamePanel.hidden = true;
    });
}
if (renameSave) {
    renameSave.addEventListener("click", async () => {
        const creds = loadCreds();
        if (!creds || !creds.apiKey) {
            setStatus(statusEl2, "❌ 未找到已保存的凭据，无法改名", "error");
            return;
        }
        const newName = renameInput.value.trim();
        if (!newName) {
            setStatus(statusEl2, "❌ 显示名称必填", "error");
            return;
        }
        renameSave.disabled = true;
        setStatus(statusEl2, '<span class="spinner"></span>正在保存…');
        try {
            const resp = await fetch("/api/profile", {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ apiKey: creds.apiKey, displayName: newName }),
            });
            const data = await resp.json();
            if (!resp.ok || data.error) {
                setStatus(statusEl2, `❌ 改名失败：${data.error || resp.statusText}`, "error");
                return;
            }
            const sanitized = data.display_name || "";
            creds.displayName = sanitized;
            saveCreds(creds);
            setTicketName(sanitized);
            renamePanel.hidden = true;
            renameInput.value = sanitized;
            setStatus(statusEl2, `✓ 名称已更新为「${escapeHtml(sanitized) || "（未命名）"}」`, "success");
        } catch (e) {
            setStatus(statusEl2, `❌ 网络错误：${e.message}`, "error");
        } finally {
            renameSave.disabled = false;
        }
    });
}

rebuildConfirm.addEventListener("click", async () => {
    rebuildConfirm.disabled = true;
    rebuildCancel.disabled = true;
    setStatus(statusEl2, '<span class="spinner"></span>正在重建容器…');
    setPill("working", "REBUILDING");
    try {
        const creds = loadCreds();
        if (!creds) throw new Error("未找到已保存的凭据");
        const resp = await fetch("/api/rebuild", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                ...creds,
                resetHome:    resetHomeEl.checked,
                syncTemplate: syncTemplateEl.checked,
                resetScratch: resetScratchEl.checked,
            }),
        });
        const data = await resp.json();
        if (!resp.ok || data.error) {
            setStatus(statusEl2, `❌ 重建失败：${data.error || resp.statusText}`, "error");
            setPill("error", "ERROR");
            return;
        }
        const { port, password, user_id, display_name } = data;
        // 用后端返回值校正本地 displayName
        const updated = { ...creds, displayName: display_name || "" };
        savePassword(user_id, password);
        saveCreds(updated);
        showStep2({ port, password, user_id, display_name });
    } catch (e) {
        setStatus(statusEl2, `❌ 网络错误：${e.message}`, "error");
        setPill("error", "ERROR");
    } finally {
        rebuildConfirm.disabled = false;
        rebuildCancel.disabled = false;
    }
});

resetLink.addEventListener("click", e => {
    e.preventDefault();
    if (!confirm("确定清除已保存的凭据？容器不会被删除，但下次访问需要重新填 API key。")) return;
    clearCreds();
    form.reset();
    form.baseUrl.value = "";
    form.apiKey.value = "";
    form.opusModel.value = "claude-opus-4";
    form.sonnetModel.value = "claude-sonnet-4";
    form.haikuModel.value = "claude-haiku-4";
    if (form.displayName) form.displayName.value = "";
    setStep(1);
    setTicketId(null);
    setTicketName(null);
    setPill("", "SETUP");
    clearStatus(statusEl);
    clearStatus(statusEl2);
});

// ---- form submit ----
form.addEventListener("submit", e => {
    e.preventDefault();
    const fd = new FormData(form);
    const creds = {
        baseUrl:     (fd.get("baseUrl")     || "").toString().trim(),
        apiKey:      (fd.get("apiKey")      || "").toString().trim(),
        opusModel:   (fd.get("opusModel")   || "").toString().trim(),
        sonnetModel: (fd.get("sonnetModel") || "").toString().trim(),
        haikuModel:  (fd.get("haikuModel")  || "").toString().trim(),
        displayName: (fd.get("displayName") || "").toString().trim(),
    };
    if (!creds.baseUrl || !creds.apiKey || !creds.opusModel || !creds.sonnetModel || !creds.haikuModel) return;
    if (!creds.displayName) {
        setStatus(statusEl, "❌ 请填写显示名称（管理员识别用）", "error");
        return;
    }

    // 判断"是不是 resubmit 流程"：用 savedHash 与当前 hash 对比 + 看 step2 是否显示过
    // 简化：savedCredsHash() 跟当前 creds 不同就当作 resubmit
    const isResubmit = (savedCredsHash() !== hashCreds(creds));
    handleSubmit(creds, isResubmit);
});

// ---- auto start: 有凭据就直接尝试进入（start 端点会自动复用现有容器） ----
(function bootstrap() {
    const c = loadCreds();
    if (!c) {
        setStep(1);
        setPill("", "SETUP");
        return;
    }
    setTicketId("· · ·");
    handleSubmit(c, /*isResubmit=*/false);
})();