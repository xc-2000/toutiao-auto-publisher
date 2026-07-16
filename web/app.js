
function calcVideoSegmentCount(totalSeconds, clipMax = 15, maxSegments = 6) {
  const total = Math.max(2, Number(totalSeconds) || 15);
  const clip = Math.max(2, Math.min(15, Number(clipMax) || 15));
  const maxSeg = Math.max(1, Math.min(8, Number(maxSegments) || 6));
  if (total <= clip) return 1;
  return Math.min(maxSeg, Math.max(2, Math.ceil(total / clip)));
}

function updateVideoDurationHint(inputId, hintId) {
  const input = document.querySelector(inputId);
  const hint = document.querySelector(hintId);
  if (!input || !hint) return;
  const total = Math.max(2, Math.min(60, Number(input.value) || 15));
  const n = calcVideoSegmentCount(total, 15, 6);
  if (n <= 1) {
    hint.textContent = `发布总时长 ${total}s，生成 1 段（单段最长15s）`;
  } else {
    hint.textContent = `发布总时长 ${total}s，自动生成 ${n} 段并融合（每段≤15s）`;
  }
}

function bindVideoDurationHints() {
  const pairs = [
    ["#video-duration", "#video-duration-hint"],
    ["#automation-video-duration", "#automation-video-duration-hint"],
    ["#challenge-video-duration", "#challenge-video-duration-hint"],
  ];
  for (const [inputId, hintId] of pairs) {
    const input = document.querySelector(inputId);
    if (!input || input.dataset.segmentHintBound) continue;
    input.dataset.segmentHintBound = "1";
    const refresh = () => updateVideoDurationHint(inputId, hintId);
    input.addEventListener("input", refresh);
    input.addEventListener("change", refresh);
    refresh();
  }
}

const state = {
  currentUser: null,
  statusTimer: null,
  registrationOpen: true,
  adminUsers: [],
  editingUserId: null,
  userFilters: { query: "", role: "all", status: "all" },
  userPage: 1,
  userPageSize: 10,
  status: null,
  hotTopics: [],
  hotMeta: { sources: [], categories: [] },
  hotFilters: { category: "", source: "", query: "" },
  selectedTopic: null,
  hotRefreshTimer: null,
  automationAccountId: null,
  automationDirty: false,
  currentView: "dashboard",
  contentFilters: { query: "", status: "all", account: "all" },
  contentPage: 1,
  contentPageSize: 10,
  taskFilters: { query: "", status: "all", account: "all" },
  taskPage: 1,
  taskPageSize: 10,
  challenges: [],
  challengeTotal: 0,
  challengePage: 1,
  challengePageSize: 10,
  challengeAccountId: null,
  challengeBizId: 1,
  challengePartStatus: 0,
  challengeCategory: "全部",
  challengeQuery: "",
  challengeCategories: ["全部"],
  challengeLoading: false,
  challengeRefreshing: false,
  challengeLoadedPage: 1,
  challengeRequestId: 0,
  challengeRefreshTimer: null,
  challengeAcceptingIds: new Set(),
  challengeAcceptingAll: false,
  challengeBatchProgress: null,
  currentChallenge: null,
  editingJobId: null,
  editingModelId: null,
  qrLoginId: null,
  qrPollTimer: null,
  mediaPreviewJobId: null,
};

const statusLabels = {
  queued: "排队中",
  generating: "生成文章",
  "cover-generating": "生成封面",
  "video-requesting": "提交视频生成",
  "video-generating": "生成视频",
  "video-downloading": "下载视频",
  "video-ready": "视频已生成",
  ready: "待发布",
  "publish-queued": "等待上传",
  publishing: "上传中",
  "video-auth": "获取上传凭证",
  "video-uploading": "上传视频",
  "video-processing": "平台处理视频",
  "video-publishing": "提交视频",
  completed: "已完成",
  error: "异常",
};

const runningStatuses = new Set([
  "queued",
  "generating",
  "cover-generating",
  "video-requesting",
  "video-generating",
  "video-downloading",
  "video-ready",
  "publish-queued",
  "publishing",
  "video-auth",
  "video-uploading",
  "video-processing",
  "video-publishing",
]);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`;
    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch (_) {}
    if (response.status === 401 && !["/api/auth/login", "/api/auth/register"].includes(path)) {
      showAuth();
    }
    throw new Error(detail);
  }
  return response.json();
}

function toast(message, type = "success") {
  const item = document.createElement("div");
  item.className = `toast ${type === "error" ? "error" : ""}`;
  item.textContent = message;
  document.querySelector("#toast-stack").append(item);
  setTimeout(() => item.remove(), 4200);
}

function formatDate(value, includeTime = true) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    ...(includeTime ? { hour: "2-digit", minute: "2-digit" } : {}),
  }).format(date);
}

function formatHeat(value) {
  const number = Number(value || 0);
  if (!number) return "实时更新";
  if (number >= 10000) return `${(number / 10000).toFixed(number >= 100000 ? 0 : 1)} 万热度`;
  return `${number.toLocaleString("zh-CN")} 热度`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function icon(name) {
  return `<i data-lucide="${name}"></i>`;
}

function hydrateIcons() {
  if (window.lucide) window.lucide.createIcons();
}

function switchAuthMode(mode) {
  const target = mode === "register" && state.registrationOpen ? "register" : "login";
  document.querySelectorAll("[data-auth-mode]").forEach((button) => button.classList.toggle("active", button.dataset.authMode === target));
  document.querySelector("#login-form").hidden = target !== "login";
  document.querySelector("#register-form").hidden = target !== "register";
  document.querySelector("#auth-title").textContent = target === "login" ? "登录" : "注册";
  document.querySelector("#auth-error").textContent = "";
}

function resetUserScopedState() {
  state.status = null;
  state.selectedTopic = null;
  state.automationAccountId = null;
  state.automationDirty = false;
  state.contentFilters = { query: "", status: "all", account: "all" };
  state.contentPage = 1;
  state.taskFilters = { query: "", status: "all", account: "all" };
  state.taskPage = 1;
  state.challenges = [];
  state.challengeTotal = 0;
  state.challengePage = 1;
  state.challengeAccountId = null;
  state.challengeBizId = 1;
  state.challengePartStatus = 0;
  state.challengeCategory = "全部";
  state.challengeQuery = "";
  state.challengeCategories = ["全部"];
  state.challengeLoading = false;
  state.challengeRefreshing = false;
  state.challengeLoadedPage = 1;
  state.challengeRequestId = 0;
  state.challengeAcceptingIds = new Set();
  state.challengeAcceptingAll = false;
  state.challengeBatchProgress = null;
  state.currentChallenge = null;
  state.editingJobId = null;
  state.editingModelId = null;
  state.adminUsers = [];
  state.editingUserId = null;
  state.userPage = 1;
  state.userFilters = { query: "", role: "all", status: "all" };
  stopChallengeRefresh();
  stopQrPolling();
}

function showAuth(registrationOpen = state.registrationOpen) {
  state.registrationOpen = registrationOpen;
  state.currentUser = null;
  resetUserScopedState();
  if (state.statusTimer) {
    clearInterval(state.statusTimer);
    state.statusTimer = null;
  }
  document.body.classList.remove("is-admin");
  document.querySelector("#app-shell").hidden = true;
  document.querySelector("#auth-shell").hidden = false;
  document.querySelector('[data-auth-mode="register"]').hidden = !registrationOpen;
  switchAuthMode("login");
  hydrateIcons();
}

function renderUserIdentity() {
  const user = state.currentUser;
  if (!user) return;
  const admin = user.role === "admin";
  document.querySelector("#app-user-name").textContent = user.display_name || user.username;
  document.querySelector("#app-user-role").textContent = admin ? "管理员" : "普通用户";
  document.querySelectorAll(".admin-nav").forEach((item) => { item.hidden = !admin; });
  document.body.classList.toggle("is-admin", admin);
  if (!admin && state.currentView === "users") switchView("dashboard");
}

async function enterApp(user) {
  // switch user => wipe previous user's module state
  resetUserScopedState();
  state.currentUser = user;
  document.querySelector("#auth-shell").hidden = true;
  document.querySelector("#app-shell").hidden = false;
  renderUserIdentity();
  switchView("dashboard");
  await Promise.all([loadHot(false), loadStatus()]);
  if (!state.statusTimer) state.statusTimer = setInterval(() => loadStatus(false), 4000);
  hydrateIcons();
}

async function loadAuth() {
  try {
    const result = await api("/api/auth/me");
    state.registrationOpen = Boolean(result.registration_open);
    if (result.authenticated && result.user) await enterApp(result.user);
    else showAuth(state.registrationOpen);
  } catch (error) {
    showAuth(true);
    document.querySelector("#auth-error").textContent = error.message;
  }
}

async function submitLogin(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  document.querySelector("#auth-error").textContent = "";
  try {
    const result = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        username: document.querySelector("#login-username").value.trim(),
        password: document.querySelector("#login-password").value,
      }),
    });
    document.querySelector("#login-password").value = "";
    await enterApp(result.user);
  } catch (error) {
    document.querySelector("#auth-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function submitRegister(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const password = document.querySelector("#register-password").value;
  const confirmPassword = document.querySelector("#register-confirm-password").value;
  if (password !== confirmPassword) {
    document.querySelector("#auth-error").textContent = "两次输入的密码不一致";
    return;
  }
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  document.querySelector("#auth-error").textContent = "";
  try {
    const result = await api("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({
        display_name: document.querySelector("#register-display-name").value.trim(),
        username: document.querySelector("#register-username").value.trim(),
        password,
      }),
    });
    form?.reset();
    await enterApp(result.user);
  } catch (error) {
    document.querySelector("#auth-error").textContent = error.message;
  } finally {
    if (button) button.disabled = false;
  }
}

async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST", body: "{}" });
  } finally {
    showAuth(state.registrationOpen);
  }
}

async function loadAppUsers() {
  if (state.currentUser?.role !== "admin") return;
  try {
    const result = await api("/api/admin/users");
    state.adminUsers = result.users || [];
    renderAppUsers();
  } catch (error) {
    toast(error.message, "error");
  }
}

function renderAppUsers() {
  const body = document.querySelector("#user-body");
  const query = state.userFilters.query.toLowerCase();
  const users = state.adminUsers.filter((user) => (
    (!query || `${user.username} ${user.display_name}`.toLowerCase().includes(query))
    && (state.userFilters.role === "all" || user.role === state.userFilters.role)
    && (state.userFilters.status === "all" || (state.userFilters.status === "enabled") === Boolean(user.enabled))
  ));
  const pageState = paginate(users, state.userPage, state.userPageSize);
  state.userPage = pageState.page;
  updatePagination("user", users.length, pageState, state.userPageSize);
  if (!users.length) {
    body.innerHTML = `<tr><td colspan="6" class="empty-row">暂无符合条件的用户</td></tr>`;
    hydrateIcons();
    return;
  }
  body.innerHTML = pageState.rows.map((user) => `
    <tr>
      <td class="account-cell"><strong>${escapeHtml(user.display_name || user.username)}</strong><span>${escapeHtml(user.username)}${user.id === state.currentUser?.id ? " · 当前用户" : ""}</span></td>
      <td><span class="status-tag ${user.role === "admin" ? "ready" : ""}">${user.role === "admin" ? "管理员" : "普通用户"}</span></td>
      <td><span class="status-tag ${user.enabled ? "completed" : "error"}">${user.enabled ? "已启用" : "已停用"}</span></td>
      <td class="date-cell">${formatDate(user.created_at)}</td>
      <td class="date-cell">${formatDate(user.last_login_at)}</td>
      <td><div class="row-actions"><button class="icon-button" data-edit-app-user="${user.id}" title="编辑用户" aria-label="编辑用户">${icon("square-pen")}</button></div></td>
    </tr>
  `).join("");
  body.querySelectorAll("[data-edit-app-user]").forEach((button) => button.addEventListener("click", () => openUserDialog(button.dataset.editAppUser)));
  hydrateIcons();
}

function openUserDialog(userId = null) {
  const user = state.adminUsers.find((item) => item.id === userId) || null;
  state.editingUserId = user?.id || null;
  document.querySelector("#user-dialog-title").textContent = user ? "编辑用户" : "创建用户";
  document.querySelector("#user-display-name").value = user?.display_name || "";
  document.querySelector("#user-username").value = user?.username || "";
  document.querySelector("#user-username").disabled = Boolean(user);
  const password = document.querySelector("#user-password");
  password.value = "";
  password.required = !user;
  password.placeholder = user ? "留空保留当前密码" : "至少 8 个字符";
  document.querySelector("#user-role").value = user?.role || "user";
  document.querySelector("#user-enabled").value = String(user?.enabled ?? true);
  document.querySelector("#delete-app-user").hidden = !user || user.id === state.currentUser?.id;
  document.querySelector("#user-dialog").showModal();
}

function closeUserDialog() {
  document.querySelector("#user-dialog").close();
  state.editingUserId = null;
}

async function saveAppUser(event) {
  event.preventDefault();
  const editing = Boolean(state.editingUserId);
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  const payload = {
    display_name: document.querySelector("#user-display-name").value.trim(),
    role: document.querySelector("#user-role").value,
    enabled: document.querySelector("#user-enabled").value === "true",
  };
  const password = document.querySelector("#user-password").value;
  if (password) payload.password = password;
  try {
    let user;
    if (editing) {
      user = await api(`/api/admin/users/${state.editingUserId}`, { method: "PATCH", body: JSON.stringify(payload) });
    } else {
      user = await api("/api/admin/users", {
        method: "POST",
        body: JSON.stringify({ ...payload, username: document.querySelector("#user-username").value.trim(), password }),
      });
    }
    if (user.id === state.currentUser?.id) {
      state.currentUser = user;
      renderUserIdentity();
    }
    closeUserDialog();
    toast(editing ? "用户已更新" : "用户已创建");
    await loadAppUsers();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function deleteAppUser() {
  const user = state.adminUsers.find((item) => item.id === state.editingUserId);
  if (!user || !window.confirm(`确认删除用户“${user.display_name || user.username}”？`)) return;
  try {
    await api(`/api/admin/users/${user.id}`, { method: "DELETE" });
    closeUserDialog();
    toast("用户已删除");
    await loadAppUsers();
  } catch (error) {
    toast(error.message, "error");
  }
}

function statusClass(status) {
  if (status === "ready") return "ready";
  if (status === "completed") return "completed";
  if (status === "error") return "error";
  return "";
}

function jobStatusLabel(job) {
  if (job.status === "ready" && ["draft", "publish"].includes(job.publish_mode)) {
    return "等待自动上传";
  }
  if (job.status === "error" && /7050|保存失败/.test(job.error || "")) {
    return "平台保存失败";
  }
  return statusLabels[job.status] || job.status;
}

function renderHotTopics() {
  const root = document.querySelector("#hot-list");
  const query = state.hotFilters.query.toLowerCase();
  const topics = state.hotTopics.filter((topic) => (
    (!state.hotFilters.category || topic.category === state.hotFilters.category)
    && (!state.hotFilters.source || (topic.source_keys || []).includes(state.hotFilters.source))
    && (!query || `${topic.title} ${topic.source} ${topic.category}`.toLowerCase().includes(query))
  ));
  if (!topics.length) {
    root.innerHTML = `<div class="empty-row">暂无热点</div>`;
    return;
  }
  root.innerHTML = topics.map((topic) => `
    <article class="hot-item ${state.selectedTopic?.id === topic.id ? "selected" : ""}" data-topic-id="${topic.id}">
      <span class="rank ${topic.rank <= 3 ? "top" : ""}">${topic.rank}</span>
      <div class="hot-copy">
        <p class="hot-title">${escapeHtml(topic.title)}</p>
        <div class="hot-meta"><span class="category-mark">${escapeHtml(topic.category || "其他")}</span><span>${escapeHtml(topic.source)}</span><span class="heat">${formatHeat(topic.hot_value)}</span></div>
        ${topic.angle ? `<p class="hot-angle" title="${escapeHtml(topic.angle)}">角度：${escapeHtml(topic.angle)}</p>` : ""}
      </div>
      <button class="select-hot" title="选择热点" aria-label="选择热点">${icon("plus")}</button>
    </article>
  `).join("");
  root.querySelectorAll(".hot-item").forEach((item) => {
    item.addEventListener("click", () => selectTopic(item.dataset.topicId));
  });
  hydrateIcons();
}

function renderHotFilters() {
  const category = document.querySelector("#hot-category");
  const source = document.querySelector("#hot-source");
  category.innerHTML = `<option value="">全部分类</option>${(state.hotMeta.categories || []).map((item) => `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)} (${item.count})</option>`).join("")}`;
  source.innerHTML = `<option value="">全部平台</option>${(state.hotMeta.sources || []).map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} (${item.count || 0})</option>`).join("")}`;
  category.value = state.hotFilters.category;
  source.value = state.hotFilters.source;
}

function selectTopic(id) {
  state.selectedTopic = state.hotTopics.find((item) => item.id === id) || null;
  if (!state.selectedTopic) return;
  document.querySelector("#topic-input").value = state.selectedTopic.title;
  document.querySelector("#selected-rank").textContent = `热榜 #${state.selectedTopic.rank}`;
  const guidance = document.querySelector("#guidance-input");
  const autoAngle = state.selectedTopic.angle || (state.selectedTopic.angles || [])[0] || "";
  // 仅在空着或仍是上一次自动角度时覆盖，避免打断用户手写角度
  if (autoAngle && (!guidance.value.trim() || guidance.dataset.autoFilled === "1")) {
    guidance.value = autoAngle;
    guidance.dataset.autoFilled = "1";
  }
  renderHotTopics();
}

function syncChallengeAccounts() {
  if (!state.status) return false;
  const previousAccountId = state.challengeAccountId;
  const accountState = state.status.accounts || {};
  const accounts = accountState.accounts || [];
  if (!accounts.some((account) => account.id === state.challengeAccountId)) {
    state.challengeAccountId = accounts.some((account) => account.id === accountState.active_id)
      ? accountState.active_id
      : accounts[0]?.id || null;
  }
  const select = document.querySelector("#challenge-account");
  select.innerHTML = accounts.length
    ? accounts.map((account) => `<option value="${escapeHtml(account.id)}">${escapeHtml(account.name)}</option>`).join("")
    : `<option value="">暂无账号</option>`;
  select.value = state.challengeAccountId || "";
  select.disabled = !accounts.length;
  return previousAccountId !== state.challengeAccountId;
}

function challengeAcceptedLabel(repeatMode) {
  if (repeatMode === "daily") return "今日已领";
  if (repeatMode === "weekly") return "本周已领";
  return "已领取";
}

function challengeAvailableLabel(repeatMode) {
  if (repeatMode === "daily") return "每日可领";
  if (repeatMode === "weekly") return "每周可领";
  return "可领取";
}

function renderChallenges() {
  const body = document.querySelector("#challenge-body");
  const totalPages = Math.max(1, Math.ceil(state.challengeTotal / state.challengePageSize));
  state.challengePage = Math.min(state.challengePage, totalPages);
  const acceptAllButton = document.querySelector("#accept-all-challenges");
  const acceptAllLabel = acceptAllButton.querySelector("span");
  acceptAllButton.disabled = state.challengeLoading
    || state.challengeAcceptingAll
    || !state.challengeAccountId
    || !state.challengeTotal;
  acceptAllLabel.textContent = state.challengeBatchProgress
    ? `领取 ${state.challengeBatchProgress.processed.toLocaleString("zh-CN")} / ${state.challengeBatchProgress.total.toLocaleString("zh-CN")}`
    : "一键领取全部";
  document.querySelector("#challenge-result-count").textContent = state.challengeLoading
    ? "协议读取中"
    : `${state.challengeTotal.toLocaleString("zh-CN")} 个活动`;
  document.querySelector("#challenge-page-summary").textContent = state.challengeTotal
    ? `共 ${state.challengeTotal.toLocaleString("zh-CN")} 个活动`
    : "暂无活动";
  document.querySelector("#challenge-page-index").textContent = `第 ${state.challengePage} / ${totalPages} 页`;
  document.querySelector("#challenge-prev").disabled = state.challengeLoading || state.challengePage <= 1;
  document.querySelector("#challenge-next").disabled = state.challengeLoading || state.challengePage >= totalPages;

  if (state.challengeLoading) {
    body.innerHTML = `<tr><td colspan="6" class="empty-row">正在通过头条协议读取创作活动...</td></tr>`;
    return;
  }
  if (!state.challenges.length) {
    body.innerHTML = `<tr><td colspan="6" class="empty-row">暂无符合条件的创作活动</td></tr>`;
    return;
  }
  const jobs = state.status?.jobs || [];
  body.innerHTML = state.challenges.map((activity) => {
    const relatedJob = jobs.find((job) => String(job.activity_id || "") === String(activity.id));
    const active = activity.status === "active";
    const participation = !active
      ? { label: "已结束", className: "error" }
      : activity.accepted
      ? { label: challengeAcceptedLabel(activity.repeat_mode), className: "accepted" }
      : ["daily", "weekly"].includes(activity.repeat_mode)
        ? { label: challengeAvailableLabel(activity.repeat_mode), className: "ready" }
      : activity.participated
        ? { label: "平台已参与", className: "active" }
      : relatedJob
        ? { label: "已有内容", className: "completed" }
        : { label: "可领取", className: "" };
    const accepting = state.challengeAcceptingIds.has(`${state.challengeAccountId}:${activity.id}`);
    const typeLabel = activity.content_type === "video" ? "视频" : "图文";
    const typeIcon = activity.content_type === "video" ? "video" : "file-text";
    const reward = activity.reward_label || (activity.max_award ? `${Number(activity.max_award).toLocaleString("zh-CN")} 元` : "按活动规则");
    const participants = activity.participants_label || `${Number(activity.participants || 0).toLocaleString("zh-CN")} 人参与`;
    return `<tr>
      <td><div class="challenge-record"><strong>${escapeHtml(activity.title)}</strong><span>${escapeHtml(activity.introduction || "查看活动要求与投稿方向")}</span><small>${activity.fresh ? `<b class="challenge-fresh">新活动</b>` : ""}${activity.repeat_mode === "daily" ? `<b class="challenge-daily">每日任务</b>` : activity.repeat_mode === "weekly" ? `<b class="challenge-weekly">每周任务</b>` : ""}<span>${escapeHtml(activity.creator || "头条官方")}</span></small></div></td>
      <td><span class="challenge-type-cell">${icon(typeIcon)}${typeLabel}</span></td>
      <td><div class="challenge-reward"><strong>${escapeHtml(reward)}</strong><span>${escapeHtml(participants)}</span></div></td>
      <td class="date-cell">${escapeHtml(activity.activity_time || `${formatDate(activity.starts_at, false)} - ${formatDate(activity.ends_at, false)}`)}</td>
      <td><span class="status-tag ${participation.className}">${participation.label}</span></td>
      <td><div class="row-actions"><button class="button secondary" data-open-challenge="${escapeHtml(activity.id)}">${active ? "详情" : "规则"}</button><button class="button secondary challenge-accept-button" data-accept-challenge="${escapeHtml(activity.id)}" ${!active || activity.accepted || accepting ? "disabled" : ""}>${accepting ? "领取中" : activity.accepted ? challengeAcceptedLabel(activity.repeat_mode) : "在线领取"}</button></div></td>
    </tr>`;
  }).join("");
  body.querySelectorAll("[data-open-challenge]").forEach((button) => button.addEventListener("click", () => openChallenge(button.dataset.openChallenge)));
  body.querySelectorAll("[data-accept-challenge]").forEach((button) => button.addEventListener("click", () => acceptChallenge(button.dataset.acceptChallenge)));
  hydrateIcons();
}

async function loadChallenges(resetPage = false, { background = false } = {}) {
  if (background && (state.challengeLoading || state.challengeRefreshing || state.challengeAcceptingAll)) return;
  const requestId = ++state.challengeRequestId;
  const previous = {
    challenges: state.challenges,
    total: state.challengeTotal,
    categories: state.challengeCategories,
    page: state.challengeLoadedPage,
  };
  const preserveOnError = !resetPage && previous.challenges.length > 0;
  if (resetPage) state.challengePage = 1;
  if (!state.challengeAccountId) {
    state.challenges = [];
    state.challengeTotal = 0;
    state.challengeLoadedPage = 1;
    renderChallenges();
    return;
  }
  if (background) {
    state.challengeRefreshing = true;
  } else {
    state.challengeLoading = true;
    renderChallenges();
  }
  const params = new URLSearchParams({
    account_id: state.challengeAccountId,
    biz_id: String(state.challengeBizId),
    part_status: String(state.challengePartStatus),
    category: state.challengeCategory,
    query: state.challengeQuery,
    page: String(state.challengePage),
    page_size: String(state.challengePageSize),
  });
  try {
    const result = await api(`/api/challenges?${params}`);
    if (requestId !== state.challengeRequestId) return;
    state.challenges = result.activities || [];
    state.challengeTotal = Number(result.total || 0);
    state.challengeLoadedPage = Number(result.page || state.challengePage);
    state.challengeCategories = result.categories || ["全部"];
    const category = document.querySelector("#challenge-category");
    category.innerHTML = state.challengeCategories.map((item) => `<option value="${escapeHtml(item)}">${escapeHtml(item === "全部" ? "全部领域" : item)}</option>`).join("");
    if (!state.challengeCategories.includes(state.challengeCategory)) state.challengeCategory = "全部";
    category.value = state.challengeCategory;
  } catch (error) {
    if (requestId !== state.challengeRequestId) return;
    state.challenges = preserveOnError ? previous.challenges : [];
    state.challengeTotal = preserveOnError ? previous.total : 0;
    state.challengeCategories = preserveOnError ? previous.categories : ["全部"];
    state.challengePage = preserveOnError ? previous.page : 1;
    if (!background) toast(error.message, "error");
  } finally {
    if (background) state.challengeRefreshing = false;
    if (requestId === state.challengeRequestId) {
      if (!background) state.challengeLoading = false;
      renderChallenges();
    }
  }
}

function stopChallengeRefresh() {
  if (!state.challengeRefreshTimer) return;
  clearInterval(state.challengeRefreshTimer);
  state.challengeRefreshTimer = null;
}

function syncChallengeRefresh() {
  if (!state.currentUser || state.currentView !== "challenges") {
    stopChallengeRefresh();
    return;
  }
  if (state.challengeRefreshTimer) return;
  state.challengeRefreshTimer = setInterval(() => {
    if (document.visibilityState === "visible") {
      loadChallenges(false, { background: true });
    }
  }, 30000);
}

function updateChallengeDetailActions() {
  const button = document.querySelector("#challenge-accept-detail");
  const label = button.querySelector("span");
  const challenge = state.currentChallenge;
  const accepting = challenge?.id
    ? state.challengeAcceptingIds.has(`${state.challengeAccountId}:${challenge.id}`)
    : false;
  const active = challenge?.status === "active";
  button.disabled = !challenge?.id || !active || Boolean(challenge.accepted) || accepting;
  label.textContent = accepting
    ? "领取中"
    : challenge?.accepted
      ? challengeAcceptedLabel(challenge.repeat_mode)
      : "在线领取";
  document.querySelector("#challenge-generate-label").textContent = challenge?.accepted ? "再次生成" : "领取并生成";
}

async function acceptChallenge(activityId) {
  const accountId = state.challengeAccountId;
  const bizId = state.challengeBizId;
  const activity = String(state.currentChallenge?.id || "") === String(activityId)
    ? state.currentChallenge
    : state.challenges.find((item) => String(item.id) === String(activityId));
  if (!accountId || !activityId) return;
  const key = `${accountId}:${activityId}`;
  if (state.challengeAcceptingIds.has(key)) return;
  state.challengeAcceptingIds.add(key);
  renderChallenges();
  updateChallengeDetailActions();
  try {
    const result = await api(`/api/challenges/${activityId}/accept`, {
      method: "POST",
      body: JSON.stringify({
        account_id: accountId,
        biz_id: bizId,
        repeat_mode: activity?.repeat_mode || "unknown",
        repeat_reason: activity?.repeat_reason || "",
        activity_url: activity?.detail_url || "",
      }),
    });
    if (state.challengeAccountId === accountId) {
      state.challenges = state.challenges.map((activity) => (
        String(activity.id) === String(activityId) ? { ...activity, accepted: true } : activity
      ));
      if (String(state.currentChallenge?.id || "") === String(activityId)) {
        state.currentChallenge = { ...state.currentChallenge, accepted: true };
      }
    }
    const repeatMode = result.acceptance?.repeat_mode;
    toast(result.new_count
      ? (repeatMode === "daily" ? "每日任务领取成功，明日可再次领取" : repeatMode === "weekly" ? "每周任务领取成功，下周可再次领取" : "活动领取成功，可直接生成相关内容")
      : (repeatMode === "daily" ? "该任务今日已经领取" : repeatMode === "weekly" ? "该任务本周已经领取" : "该活动已经领取"));
  } catch (error) {
    toast(error.message, "error");
  } finally {
    state.challengeAcceptingIds.delete(key);
    renderChallenges();
    updateChallengeDetailActions();
  }
}

async function acceptChallengeBatchPage(payload, retries = 2) {
  let attempt = 0;
  while (true) {
    try {
      return await api("/api/challenges/accept-batch", {
        method: "POST",
        body: JSON.stringify(payload),
      });
    } catch (error) {
      if (attempt >= retries) throw error;
      attempt += 1;
      await new Promise((resolve) => setTimeout(resolve, attempt * 700));
    }
  }
}

async function acceptAllChallenges() {
  if (state.challengeAcceptingAll || !state.challengeAccountId || !state.challengeTotal) return;
  const scope = {
    account_id: state.challengeAccountId,
    biz_id: state.challengeBizId,
    part_status: state.challengePartStatus,
    category: state.challengeCategory,
    query: state.challengeQuery,
    page_size: 100,
  };
  let total = state.challengeTotal;
  let page = 1;
  let newCount = 0;
  let existingCount = 0;
  state.challengeAcceptingAll = true;
  state.challengeBatchProgress = { processed: 0, total };
  renderChallenges();
  try {
    while ((page - 1) * scope.page_size < total) {
      const result = await acceptChallengeBatchPage({ ...scope, page });
      total = Number(result.total || total);
      newCount += Number(result.new_count || 0);
      existingCount += Number(result.existing_count || 0);
      const acceptedIds = new Set((result.accepted_ids || []).map(String));
      if (state.challengeAccountId === scope.account_id && acceptedIds.size) {
        state.challenges = state.challenges.map((activity) => (
          acceptedIds.has(String(activity.id)) ? { ...activity, accepted: true } : activity
        ));
      }
      state.challengeBatchProgress = {
        processed: Math.min(page * scope.page_size, total),
        total,
      };
      renderChallenges();
      page += 1;
    }
    toast(newCount
      ? `新增领取 ${newCount.toLocaleString("zh-CN")} 个任务，跳过 ${existingCount.toLocaleString("zh-CN")} 个已领任务`
      : `当前范围 ${existingCount.toLocaleString("zh-CN")} 个活动均已领取`);
  } catch (error) {
    toast(`领取进度中断：${error.message}`, "error");
  } finally {
    state.challengeAcceptingAll = false;
    state.challengeBatchProgress = null;
    renderChallenges();
    if (state.challengeAccountId === scope.account_id) {
      await loadChallenges(false, { background: true });
    }
  }
}

function updateChallengeGenerateFields() {
  const type = document.querySelector("#challenge-generate-type").value || "article";
  document.querySelector("#challenge-word-count-field").hidden = type === "video";
  document.querySelectorAll(".challenge-video-field").forEach((field) => { field.hidden = type !== "video"; });
}

async function openChallenge(activityId) {
  const activity = state.challenges.find((item) => String(item.id) === String(activityId));
  state.currentChallenge = activity || { id: activityId };
  const dialog = document.querySelector("#challenge-dialog");
  document.querySelector("#challenge-dialog-title").textContent = activity?.title || "活动详情";
  document.querySelector("#challenge-detail-meta").innerHTML = `<span>${icon("loader-circle")}正在读取平台活动要求</span>`;
  document.querySelector("#challenge-detail-blocks").innerHTML = `<div class="challenge-detail-empty">加载中...</div>`;
  document.querySelector("#challenge-generate-form").hidden = true;
  document.querySelector("#challenge-banner").hidden = true;
  updateChallengeDetailActions();
  dialog.showModal();
  hydrateIcons();
  try {
    const params = new URLSearchParams({
      account_id: state.challengeAccountId || "",
      repeat_mode: activity?.repeat_mode || "unknown",
      activity_url: activity?.detail_url || "",
    });
    const detail = await api(`/api/challenges/${activityId}?${params}`);
    const repeatMode = detail.repeat_mode === "unknown"
      ? activity?.repeat_mode || "unknown"
      : detail.repeat_mode;
    state.currentChallenge = {
      ...activity,
      ...detail,
      repeat_mode: repeatMode,
      repeat_reason: detail.repeat_mode === "unknown"
        ? activity?.repeat_reason || detail.repeat_reason
        : detail.repeat_reason,
      daily_repeatable: repeatMode === "daily",
      weekly_repeatable: repeatMode === "weekly",
    };
    state.challenges = state.challenges.map((item) => (
      String(item.id) === String(activityId)
        ? {
            ...item,
            repeat_mode: repeatMode,
            repeat_reason: state.currentChallenge.repeat_reason,
            daily_repeatable: repeatMode === "daily",
            weekly_repeatable: repeatMode === "weekly",
            accepted: detail.accepted,
          }
        : item
    ));
    renderChallenges();
    document.querySelector("#challenge-dialog-title").textContent = detail.title || activity?.title || "活动详情";
    const typeLabels = (detail.publish_types || []).map((item) => item.type === "video" ? "视频" : "图文");
    document.querySelector("#challenge-detail-meta").innerHTML = `
      <span>${icon("radio-tower")}HTTP 协议</span>
      <span>${icon("database")}${detail.rule_source === "toutiao-magic-page" ? "活动页规则" : "平台详情"}</span>
      <span>${icon("file-stack")}${escapeHtml(typeLabels.join(" / ") || (activity?.content_type === "video" ? "视频" : "图文"))}</span>
      <span>${icon(detail.participated ? "circle-check" : "circle-dashed")}${detail.participated ? "平台已参与" : "尚未投稿"}</span>
      <span>${icon(["daily", "weekly"].includes(repeatMode) ? "calendar-sync" : "calendar-check")}${repeatMode === "daily" ? "每日可重复" : repeatMode === "weekly" ? "每周可重复" : "单次领取"}</span>
      <span>${icon(detail.accepted ? "circle-check-big" : "circle-plus")}${detail.accepted ? challengeAcceptedLabel(repeatMode) : "可领取"}</span>
      ${activity?.activity_time ? `<span>${icon("calendar-days")}${escapeHtml(activity.activity_time)}</span>` : ""}`;
    const banner = document.querySelector("#challenge-banner");
    if (detail.banner) {
      banner.src = String(detail.banner).startsWith("//") ? `https:${detail.banner}` : detail.banner;
      banner.hidden = false;
    }
    const blocks = detail.blocks || [];
    const ruleImages = detail.rule_images || [];
    const blockHtml = blocks.map((block) => `<section><h3>${escapeHtml(block.title)}</h3><p>${escapeHtml(block.text)}</p></section>`).join("");
    const imageHtml = ruleImages.length
      ? `<section class="challenge-rule-images"><h3>活动规则原图</h3><div>${ruleImages.map((url, index) => `<img src="${escapeHtml(url)}" alt="活动规则图 ${index + 1}" loading="lazy" />`).join("")}</div></section>`
      : "";
    document.querySelector("#challenge-detail-blocks").innerHTML = blocks.length || ruleImages.length
      ? `${blockHtml}${imageHtml}`
      : `<div class="challenge-detail-empty">平台详情接口与活动页均未返回文本规则，将根据活动标题与简介生成相关内容</div>`;
    const publishTypes = detail.publish_types?.length
      ? detail.publish_types
      : [{ type: activity?.content_type || "article", label: activity?.content_type === "video" ? "发表视频" : "发表文章" }];
    const typeSelect = document.querySelector("#challenge-generate-type");
    typeSelect.innerHTML = publishTypes.map((item) => `<option value="${escapeHtml(item.type)}">${escapeHtml(item.label)}</option>`).join("");
    const preferredType = activity?.content_type || (state.challengeBizId === 2 ? "video" : "article");
    if (publishTypes.some((item) => item.type === preferredType)) typeSelect.value = preferredType;
    document.querySelector("#challenge-generate-form").hidden = detail.status !== "active";
    updateChallengeGenerateFields();
    updateChallengeDetailActions();
    hydrateIcons();
  } catch (error) {
    document.querySelector("#challenge-detail-meta").innerHTML = `<span>${icon("circle-alert")}读取失败</span>`;
    document.querySelector("#challenge-detail-blocks").innerHTML = `<div class="challenge-detail-empty">${escapeHtml(error.message)}</div>`;
    toast(error.message, "error");
    updateChallengeDetailActions();
    hydrateIcons();
  }
}

function closeChallengeDialog() {
  document.querySelector("#challenge-dialog").close();
  state.currentChallenge = null;
}

async function createChallengeJob(event) {
  event.preventDefault();
  if (!state.currentChallenge?.id || !state.challengeAccountId) return;
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  const contentType = document.querySelector("#challenge-generate-type").value;
  try {
    await api(`/api/challenges/${state.currentChallenge.id}/generate`, {
      method: "POST",
      body: JSON.stringify({
        account_id: state.challengeAccountId,
        biz_id: state.challengeBizId,
        content_type: contentType,
        word_count: Number(document.querySelector("#challenge-word-count").value),
        auto_action: document.querySelector("#challenge-auto-action").value || null,
        video_duration: Number(document.querySelector("#challenge-video-duration").value),
        video_aspect_ratio: document.querySelector("#challenge-video-ratio").value,
        introduction: state.currentChallenge.introduction || "",
        repeat_mode: state.currentChallenge.repeat_mode || "unknown",
        repeat_reason: state.currentChallenge.repeat_reason || "",
        activity_url: state.currentChallenge.detail_url || "",
      }),
    });
    closeChallengeDialog();
    toast(`已接取活动，${contentType === "video" ? "视频" : "图文"}内容进入生成队列`);
    await loadStatus();
    switchView("tasks");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function renderStatus() {
  if (!state.status) return;
  if (state.status.app_user) {
    state.currentUser = state.status.app_user;
    renderUserIdentity();
  }
  const jobs = state.status.jobs || [];
  const running = jobs.filter((job) => runningStatuses.has(job.status)).length;
  const ready = jobs.filter((job) => job.status === "ready").length;
  const completed = jobs.filter((job) => job.status === "completed").length;
  document.querySelector("#metric-hot").textContent = state.hotTopics.length;
  document.querySelector("#metric-running").textContent = running;
  document.querySelector("#metric-ready").textContent = ready;
  document.querySelector("#metric-completed").textContent = completed;
  const badge = document.querySelector("#api-key-badge");
  badge.className = `connection-badge ${state.status.api_key_configured ? "ready" : "missing"}`;
  badge.innerHTML = `${icon("key-round")}<span>${state.status.api_key_configured ? "生成 API 已连接" : "缺少 API Key"}</span>`;

  document.querySelector("#article-model").textContent = state.status.ai_model || "--";
  document.querySelector("#cover-model").textContent = state.status.cover_model || "--";
  document.querySelector("#video-model").textContent = state.status.video_model || "--";
  document.querySelector("#api-key-name").textContent = state.status.api_key_env || "OPENAI_API_KEY";
  document.querySelector("#api-key-state").textContent = state.status.api_key_configured ? "已配置" : "待配置";
  const session = state.status.protocol_session || {};
  const sessionState = state.status.session || {};
  const accountState = state.status.accounts || {};
  const activeAccount = (accountState.accounts || []).find((item) => item.id === accountState.active_id);
  document.querySelector("#session-message").textContent = activeAccount ? `${activeAccount.name} · ${activeAccount.media_id || activeAccount.user_id || "已登录"}` : sessionState.message || (session.configured ? "协议 Cookie 已配置" : "尚未登录");
  document.querySelector("#account-state-badge").textContent = activeAccount || session.configured ? "已登录" : "未登录";
  const challengeAccountChanged = syncChallengeAccounts();
  if (challengeAccountChanged && state.currentView === "challenges" && !state.challengeLoading) {
    loadChallenges(true);
  }
  renderAutomation();
  renderContentTable();
  renderTaskTable();
  renderAccounts();
  renderModels();
  hydrateIcons();
}

function renderAccounts() {
  const root = document.querySelector("#account-list");
  const accountState = state.status?.accounts || {};
  const accounts = accountState.accounts || [];
  if (!accounts.length) {
    root.innerHTML = `<div class="resource-empty">暂无账号</div>`;
    return;
  }
  root.innerHTML = accounts.map((account) => {
    const active = account.id === accountState.active_id;
    const initial = escapeHtml((account.name || "头").slice(0, 1));
    return `<div class="resource-row" data-account-row="${escapeHtml(account.id)}">
      <div class="resource-identity">
        <span class="account-avatar">${account.avatar ? `<img src="${escapeHtml(account.avatar)}" alt="" />` : initial}</span>
        <span class="account-meta">
          <strong class="account-name" data-account-name="${escapeHtml(account.id)}" title="点击编辑名称">${escapeHtml(account.name)}</strong>
          <input class="account-name-input" type="text" maxlength="40" value="${escapeHtml(account.name)}" data-account-name-input="${escapeHtml(account.id)}" hidden />
          <small>${escapeHtml(account.media_id || account.user_id || account.external_id || "头条号")}</small>
        </span>
      </div>
      <span class="status-tag ${active ? "ready" : ""}">${active ? "当前账号" : "已保存"}</span>
      <div class="resource-actions">
        ${active ? "" : `<button class="button secondary" data-activate-account="${account.id}">切换</button>`}
        <button class="icon-button" data-rename-account="${account.id}" title="编辑名称" aria-label="编辑名称">${icon("pencil")}</button>
        <button class="icon-button" data-delete-account="${account.id}" title="删除账号" aria-label="删除账号">${icon("trash-2")}</button>
      </div>
    </div>`;
  }).join("");
  root.querySelectorAll("[data-activate-account]").forEach((button) => button.addEventListener("click", () => activateAccount(button.dataset.activateAccount)));
  root.querySelectorAll("[data-rename-account]").forEach((button) => button.addEventListener("click", () => beginRenameAccount(button.dataset.renameAccount)));
  root.querySelectorAll("[data-account-name]").forEach((el) => el.addEventListener("click", () => beginRenameAccount(el.dataset.accountName)));
  root.querySelectorAll("[data-account-name-input]").forEach((input) => {
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        commitRenameAccount(input.dataset.accountNameInput, input.value);
      } else if (event.key === "Escape") {
        event.preventDefault();
        cancelRenameAccount(input.dataset.accountNameInput);
      }
    });
    input.addEventListener("blur", () => {
      if (input.hidden) return;
      commitRenameAccount(input.dataset.accountNameInput, input.value);
    });
  });
  root.querySelectorAll("[data-delete-account]").forEach((button) => button.addEventListener("click", () => deleteAccount(button.dataset.deleteAccount)));
  hydrateIcons();
}

function renderModels() {
  const root = document.querySelector("#model-list");
  const modelState = state.status?.models || {};
  const profiles = (modelState.profiles || []).filter((profile) => (
    !profile.builtin || modelState.active?.[profile.kind] === profile.id
  ));
  root.innerHTML = profiles.map((profile) => {
    const active = modelState.active?.[profile.kind] === profile.id;
    const kindMeta = {
      article: { icon: "file-text", label: "文章" },
      cover: { icon: "image", label: "封面" },
      video: { icon: "video", label: "视频" },
    }[profile.kind] || { icon: "box", label: profile.kind };
    return `<div class="resource-row model-row" data-model-kind="${escapeHtml(profile.kind)}">
      <div class="resource-identity model-identity">
        <span class="model-kind-icon">${icon(kindMeta.icon)}</span>
        <span class="model-copy"><strong>${escapeHtml(profile.name)}</strong><small class="model-meta"><span>${escapeHtml(profile.model)}</span><span>${escapeHtml(profile.base_url || "")}</span></small></span>
      </div>
      <span class="status-tag ${active ? "ready" : ""}">${kindMeta.label}${active ? " · 已启用" : ""}</span>
      <div class="resource-actions">
        ${active ? "" : `<button class="button secondary" data-activate-model="${profile.id}">启用</button>`}
        ${profile.builtin ? "" : `<button class="icon-button" data-edit-model="${profile.id}" title="编辑模型" aria-label="编辑模型">${icon("square-pen")}</button><button class="icon-button" data-delete-model="${profile.id}" title="删除模型" aria-label="删除模型">${icon("trash-2")}</button>`}
      </div>
    </div>`;
  }).join("");
  root.querySelectorAll("[data-activate-model]").forEach((button) => button.addEventListener("click", () => activateModel(button.dataset.activateModel)));
  root.querySelectorAll("[data-edit-model]").forEach((button) => button.addEventListener("click", () => openModelDialog(button.dataset.editModel)));
  root.querySelectorAll("[data-delete-model]").forEach((button) => button.addEventListener("click", () => deleteModel(button.dataset.deleteModel)));
  hydrateIcons();
}

function jobMatchesQuery(job, query) {
  if (!query) return true;
  return [
    job.title,
    job.topic,
    job.summary,
    job.topic_category,
    job.topic_source,
    job.account_name,
    job.error,
    ...(job.tags || []),
  ].filter(Boolean).join(" ").toLowerCase().includes(query.toLowerCase());
}

function renderAccountFilter(selector, jobs, selected) {
  const select = document.querySelector(selector);
  const accounts = new Map();
  (state.status?.accounts?.accounts || []).forEach((account) => accounts.set(account.id, account.name));
  jobs.forEach((job) => {
    if (job.account_id) accounts.set(job.account_id, job.account_name || accounts.get(job.account_id) || "未命名账号");
  });
  select.innerHTML = `<option value="all">全部账号</option>${Array.from(accounts, ([id, name]) => `<option value="${escapeHtml(id)}">${escapeHtml(name)}</option>`).join("")}`;
  const normalized = accounts.has(selected) ? selected : "all";
  select.value = normalized;
  return normalized;
}

function paginate(items, requestedPage, pageSize) {
  const totalPages = Math.max(1, Math.ceil(items.length / pageSize));
  const page = Math.min(Math.max(1, requestedPage), totalPages);
  const start = (page - 1) * pageSize;
  return { page, totalPages, start, rows: items.slice(start, start + pageSize) };
}

function updatePagination(prefix, total, pageState, pageSize) {
  const { page, totalPages, start } = pageState;
  const end = Math.min(start + pageSize, total);
  document.querySelector(`#${prefix}-page-summary`).textContent = total ? `显示 ${start + 1}-${end}，共 ${total} 条` : "暂无记录";
  document.querySelector(`#${prefix}-page-index`).textContent = `第 ${page} / ${totalPages} 页`;
  document.querySelector(`#${prefix}-prev`).disabled = page <= 1;
  document.querySelector(`#${prefix}-next`).disabled = page >= totalPages;
}

function jobActionButtons(job) {
  const contentLabel = job.content_type === "video" ? "视频" : "文章";
  const canRegen = Boolean(job.article_path || job.topic) && !runningStatuses.has(job.status);
  const mediaTarget = job.content_type === "video" ? "video" : "cover";
  const mediaLabel = job.content_type === "video" ? "重做视频" : "重做封面";
  return `
    ${job.article_path ? `<button class="icon-button" data-edit="${job.id}" title="编辑${contentLabel}" aria-label="编辑${contentLabel}">${icon("square-pen")}</button>` : ""}
    ${canRegen ? `<button class="icon-button" data-regen="${job.id}" data-regen-target="article" title="重写文案" aria-label="重写文案">${icon("file-pen-line")}</button>` : ""}
    ${canRegen && job.article_path ? `<button class="icon-button" data-regen="${job.id}" data-regen-target="${mediaTarget}" title="${mediaLabel}" aria-label="${mediaLabel}">${icon(job.content_type === "video" ? "clapperboard" : "image")}</button>` : ""}
    ${canRegen ? `<button class="icon-button" data-regen="${job.id}" data-regen-target="all" title="全部重生成" aria-label="全部重生成">${icon("refresh-cw")}</button>` : ""}
    ${job.status === "ready" ? `<button class="icon-button" data-draft="${job.id}" title="上传${contentLabel}草稿" aria-label="上传${contentLabel}草稿">${icon("file-up")}</button>` : ""}
    ${job.status === "ready" ? `<button class="icon-button" data-publish="${job.id}" title="立即发布${contentLabel}" aria-label="立即发布${contentLabel}">${icon("send")}</button>` : ""}
  `;
}


function jobMediaUrls(job) {
  if (!job) return { cover: "", video: "" };
  const stamp = encodeURIComponent(job.updated_at || Date.now());
  const cover = job.has_cover || job.cover_path || job.cover_url
    ? (job.cover_url || `/api/jobs/${job.id}/cover?v=${stamp}`)
    : "";
  const video = job.has_video || job.video_path || job.video_url
    ? (job.video_url || `/api/jobs/${job.id}/video?v=${stamp}`)
    : "";
  return { cover, video };
}

function contentThumbHtml(job) {
  const media = jobMediaUrls(job);
  const isVideo = job.content_type === "video";
  if (isVideo && media.video) {
    return `<button type="button" class="content-thumb is-media is-video" data-preview-job="${job.id}" data-preview-kind="video" title="预览视频">
      <video src="${media.video}#t=0.1" muted playsinline preload="metadata"></video>
      <span class="content-thumb-badge">${icon("play")}</span>
    </button>`;
  }
  if (media.cover) {
    return `<button type="button" class="content-thumb is-media is-image" data-preview-job="${job.id}" data-preview-kind="image" title="预览封面">
      <img src="${media.cover}" alt="" loading="lazy" onerror="this.closest('.content-thumb')?.classList.add('is-broken')" />
    </button>`;
  }
  if (isVideo && media.cover) {
    return `<button type="button" class="content-thumb is-media is-image" data-preview-job="${job.id}" data-preview-kind="image" title="预览封面">
      <img src="${media.cover}" alt="" loading="lazy" />
    </button>`;
  }
  return `<span class="content-thumb">${icon(isVideo ? "video" : "file-text")}</span>`;
}

function openMediaPreview(jobId, preferredKind = "") {
  const job = (state.status?.jobs || []).find((item) => item.id === jobId);
  if (!job) return toast("内容不存在", "error");
  const media = jobMediaUrls(job);
  const image = document.querySelector("#media-preview-image");
  const video = document.querySelector("#media-preview-video");
  const empty = document.querySelector("#media-preview-empty");
  const title = document.querySelector("#media-preview-title");
  title.textContent = job.title || job.topic || "媒体预览";
  image.hidden = true;
  video.hidden = true;
  empty.hidden = true;
  image.removeAttribute("src");
  video.pause();
  video.removeAttribute("src");
  const wantVideo = preferredKind === "video" || (job.content_type === "video" && media.video);
  if (wantVideo && media.video) {
    video.src = media.video;
    video.hidden = false;
    video.load();
  } else if (media.cover) {
    image.src = media.cover;
    image.hidden = false;
  } else if (media.video) {
    video.src = media.video;
    video.hidden = false;
    video.load();
  } else {
    empty.hidden = false;
  }
  state.mediaPreviewJobId = jobId;
  bindMediaPreviewControls();
  const dialog = document.querySelector("#media-preview-dialog");
  if (!dialog) return toast("预览组件未加载", "error");
  if (!dialog.open) dialog.showModal();
  hydrateIcons();
}

function cleanupMediaPreview() {
  const video = document.querySelector("#media-preview-video");
  const image = document.querySelector("#media-preview-image");
  const empty = document.querySelector("#media-preview-empty");
  if (video) {
    try { video.pause(); } catch (_) {}
    video.removeAttribute("src");
    try { video.load(); } catch (_) {}
    video.hidden = true;
  }
  if (image) {
    image.removeAttribute("src");
    image.hidden = true;
  }
  if (empty) empty.hidden = true;
  state.mediaPreviewJobId = null;
}

function closeMediaPreview(event) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }
  const dialog = document.querySelector("#media-preview-dialog");
  cleanupMediaPreview();
  if (dialog?.open) {
    dialog.close();
  }
}

function bindMediaPreviewControls() {
  const dialog = document.querySelector("#media-preview-dialog");
  const closeBtn = document.querySelector("#close-media-preview");
  if (!dialog || dialog.dataset.bound === "1") return;
  dialog.dataset.bound = "1";
  closeBtn?.addEventListener("click", closeMediaPreview);
  dialog.addEventListener("cancel", (event) => {
    // allow ESC to close
    cleanupMediaPreview();
  });
  dialog.addEventListener("close", () => {
    cleanupMediaPreview();
  });
  // click backdrop (dialog itself) to close
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) closeMediaPreview(event);
  });
}

function renderContentTable() {
  const body = document.querySelector("#content-body");
  const allContent = (state.status?.jobs || []).filter((job) => (
    job.article_path || job.has_cover || job.has_video || job.cover_path || job.video_path
  ));
  state.contentFilters.account = renderAccountFilter("#content-account", allContent, state.contentFilters.account);
  const jobs = allContent.filter((job) => (
    (state.contentFilters.status === "all" || job.status === state.contentFilters.status)
    && (state.contentFilters.account === "all" || job.account_id === state.contentFilters.account)
    && jobMatchesQuery(job, state.contentFilters.query)
  ));
  const pageState = paginate(jobs, state.contentPage, state.contentPageSize);
  state.contentPage = pageState.page;
  document.querySelector("#content-result-count").textContent = `${jobs.length} 条内容`;
  if (!pageState.rows.length) {
    body.innerHTML = `<tr><td colspan="5" class="empty-row">暂无符合条件的内容</td></tr>`;
  } else {
    body.innerHTML = pageState.rows.map((job) => {
      const media = jobMediaUrls(job);
      const mediaHint = job.content_type === "video"
        ? (media.video ? "视频已生成" : media.cover ? "封面已生成" : "媒体生成中")
        : (media.cover ? "封面已生成" : "封面生成中");
      return `
      <tr>
        <td>
          <div class="content-record">
            ${contentThumbHtml(job)}
            <span class="content-cell">
              <strong>${escapeHtml(job.title || job.topic)}</strong>
              <span>${escapeHtml(job.summary || job.topic)}</span>
              <small class="content-media-hint">${escapeHtml(mediaHint)}</small>
              ${job.activity_title ? `<small class="job-activity-hint">关联任务：${escapeHtml(job.activity_title)}${job.activity_reward ? ` · ${escapeHtml(job.activity_reward)}` : ""}</small>` : ""}
            </span>
          </div>
        </td>
        <td class="account-cell"><strong>${escapeHtml(job.account_name || "未绑定账号")}</strong><span>${escapeHtml(job.topic_category || "未分类")}</span></td>
        <td><span class="status-tag ${statusClass(job.status)}">${jobStatusLabel(job)}</span></td>
        <td class="date-cell">${formatDate(job.updated_at)}</td>
        <td><div class="row-actions">
          ${(media.cover || media.video) ? `<button class="icon-button" data-preview-job="${job.id}" data-preview-kind="${job.content_type === "video" && media.video ? "video" : "image"}" title="预览" aria-label="预览">${icon("eye")}</button>` : ""}
          ${jobActionButtons(job)}
        </div></td>
      </tr>`;
    }).join("");
  }
  updatePagination("content", jobs.length, pageState, state.contentPageSize);
  bindJobActions(body);
  body.querySelectorAll("[data-preview-job]").forEach((el) => {
    el.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      openMediaPreview(el.dataset.previewJob, el.dataset.previewKind || "");
    });
  });
  // kick video thumbs to load first frame
  body.querySelectorAll(".content-thumb video").forEach((video) => {
    video.addEventListener("loadeddata", () => video.classList.add("is-ready"), { once: true });
    video.addEventListener("error", () => video.closest(".content-thumb")?.classList.add("is-broken"));
    try { video.currentTime = 0.1; } catch (_) {}
  });
}

function jobProgress(job) {
  const fallback = {
    queued: 8,
    generating: 36,
    "cover-generating": 64,
    "video-requesting": 68,
    "video-generating": 74,
    "video-downloading": 79,
    "video-ready": 82,
    ready: 80,
    "publish-queued": 86,
    publishing: 94,
    "video-auth": 86,
    "video-uploading": 91,
    "video-processing": 95,
    "video-publishing": 98,
    completed: 100,
    error: 100,
  }[job.status] ?? 0;
  const stored = Number(job.progress);
  const progress = Number.isFinite(stored) ? Math.min(100, Math.max(0, stored)) : fallback;
  return { progress, text: job.status === "error" ? "已停止" : `${progress}%` };
}

function renderTaskTable() {
  const body = document.querySelector("#task-body");
  const allJobs = state.status?.jobs || [];
  state.taskFilters.account = renderAccountFilter("#task-account", allJobs, state.taskFilters.account);
  const jobs = allJobs.filter((job) => {
    const statusMatches = state.taskFilters.status === "all"
      || (state.taskFilters.status === "running" ? runningStatuses.has(job.status) : job.status === state.taskFilters.status);
    return statusMatches
      && (state.taskFilters.account === "all" || job.account_id === state.taskFilters.account)
      && jobMatchesQuery(job, state.taskFilters.query);
  });
  const pageState = paginate(jobs, state.taskPage, state.taskPageSize);
  state.taskPage = pageState.page;
  document.querySelector("#task-result-count").textContent = `${jobs.length} 个任务`;
  if (!pageState.rows.length) {
    body.innerHTML = `<tr><td colspan="6" class="empty-row">暂无符合条件的任务</td></tr>`;
  } else {
    body.innerHTML = pageState.rows.map((job) => {
      const progress = jobProgress(job);
      return `
        <tr>
          <td class="content-cell"><strong>${escapeHtml(job.title || job.topic)}</strong><span>${escapeHtml([job.topic_category, job.topic_source, job.topic].filter(Boolean).join(" · "))}</span>${job.activity_title ? `<small class="job-activity-hint">变现任务：${escapeHtml(job.activity_title)}${job.activity_reward ? ` · ${escapeHtml(job.activity_reward)}` : ""}</small>` : ""}</td>
          <td class="account-cell"><strong>${escapeHtml(job.account_name || "未绑定账号")}</strong><span>${escapeHtml(job.publish_mode === "publish" ? "发布模式" : "草稿模式")}</span></td>
          <td>
            <div class="task-progress ${job.status === "error" ? "error" : job.status === "completed" ? "completed" : ""}">
              <span><strong>${jobStatusLabel(job)}</strong><small>${progress.text}</small></span>
              <i><b style="width: ${progress.progress}%"></b></i>
              ${job.error ? `<em title="${escapeHtml(job.error)}">${escapeHtml(job.error)}</em>` : ""}
            </div>
          </td>
          <td class="date-cell">${formatDate(job.created_at)}</td>
          <td class="date-cell">${formatDate(job.updated_at)}</td>
          <td><div class="row-actions">${jobActionButtons(job)}</div></td>
        </tr>
      `;
    }).join("");
  }
  updatePagination("task", jobs.length, pageState, state.taskPageSize);
  bindJobActions(body);
}

function bindJobActions(root) {
  root.querySelectorAll("[data-edit]").forEach((button) => button.addEventListener("click", () => openEditor(button.dataset.edit)));
  root.querySelectorAll("[data-regen]").forEach((button) => button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    regenerateJob(button.dataset.regen, button.dataset.regenTarget || "all");
  }));
  root.querySelectorAll("[data-draft]").forEach((button) => button.addEventListener("click", () => submitJob(button.dataset.draft, "draft")));
  root.querySelectorAll("[data-publish]").forEach((button) => button.addEventListener("click", () => submitJob(button.dataset.publish, "publish")));
  hydrateIcons();
}

function renderScopeOptions(root, items, selected, allLabel) {
  const selectedSet = new Set(selected || []);
  root.innerHTML = `
    <label><input type="checkbox" data-scope-all checked /><span>${escapeHtml(allLabel)}</span></label>
    ${items.map((item) => `<label><input type="checkbox" value="${escapeHtml(item.id || item.name)}" ${selectedSet.has(item.id || item.name) ? "checked" : ""} /><span>${escapeHtml(item.name)}</span></label>`).join("")}
  `;
  const all = root.querySelector("[data-scope-all]");
  const choices = Array.from(root.querySelectorAll("input:not([data-scope-all])"));
  all.checked = selectedSet.size === 0;
  all.addEventListener("change", () => {
    if (all.checked) choices.forEach((input) => { input.checked = false; });
  });
  choices.forEach((input) => input.addEventListener("change", () => {
    if (input.checked) all.checked = false;
    if (!choices.some((choice) => choice.checked)) all.checked = true;
  }));
}


function automationPayloadFromProfile(profile, overrides = {}) {
  return {
    account_id: profile.account_id,
    enabled: Boolean(overrides.enabled ?? profile.enabled),
    interval_minutes: Number(overrides.interval_minutes ?? profile.interval_minutes ?? 60),
    mode: overrides.mode ?? profile.mode ?? "draft",
    content_type: overrides.content_type ?? profile.content_type ?? "article",
    video_duration: Number(overrides.video_duration ?? profile.video_duration ?? 15),
    auto_claim_challenges: Boolean(overrides.auto_claim_challenges ?? profile.auto_claim_challenges ?? true),
    pick_count: Number(overrides.pick_count ?? profile.pick_count ?? 1),
    categories: overrides.categories ?? profile.categories ?? [],
    sources: overrides.sources ?? profile.sources ?? [],
  };
}

function automationPayloadFromForm(accountId, enabledOverride) {
  const contentType = document.querySelector('input[name="automation-content-type"]:checked')?.value || "article";
  return {
    account_id: accountId,
    enabled: enabledOverride ?? document.querySelector("#automation-enabled").checked,
    interval_minutes: Number(document.querySelector("#automation-interval").value),
    mode: document.querySelector('input[name="automation-mode"]:checked')?.value || "draft",
    content_type: contentType,
    video_duration: Number(document.querySelector("#automation-video-duration")?.value || 15),
    auto_claim_challenges: document.querySelector("#automation-auto-claim")?.checked !== false,
    pick_count: Number(document.querySelector("#automation-count").value),
    categories: readScope("#automation-categories"),
    sources: readScope("#automation-sources"),
  };
}

function syncAutomationContentTypeUI(contentType) {
  const isVideo = contentType === "video";
  const options = document.querySelector("#automation-video-options");
  if (options) options.hidden = !isVideo;
  const value = document.querySelector("#automation-content-type-value");
  if (value) value.textContent = isVideo ? "短视频" : "图文";
}

function readScope(rootSelector) {
  return Array.from(document.querySelectorAll(`${rootSelector} input:not([data-scope-all]):checked`)).map((input) => input.value);
}

function renderAutomationAccountList(profiles) {
  const root = document.querySelector("#automation-account-list");
  const hint = document.querySelector("#automation-account-hint");
  if (!root) return;
  if (!profiles.length) {
    root.innerHTML = `<div class="auto-account-empty">
      <i data-lucide="users-round"></i>
      <strong>暂无头条账号</strong>
      <span>请先在设置中添加账号，再配置自动化</span>
    </div>`;
    if (hint) hint.hidden = true;
    hydrateIcons();
    return;
  }
  const enabledCount = profiles.filter((item) => item.enabled).length;
  if (hint) {
    hint.hidden = false;
    hint.innerHTML = `<span class="auto-stat"><em>${profiles.length}</em> 个账号</span><span class="auto-stat-dot"></span><span class="auto-stat"><em>${enabledCount}</em> 个运行中</span><span class="auto-stat-dot"></span><span>点击卡片切换配置</span>`;
  }
  root.innerHTML = profiles.map((profile) => {
    const selected = profile.account_id === state.automationAccountId;
    const initial = escapeHtml((profile.account_name || "头").slice(0, 1));
    const jobs = (state.status?.jobs || []).filter((job) => job.account_id === profile.account_id).length;
    const modeLabel = profile.mode === "publish" ? "发布" : "草稿";
    const typeLabel = profile.content_type === "video" ? "视频" : "图文";
    const statusLabel = profile.enabled ? "运行中" : "已停止";
    const interval = Number(profile.interval_minutes || 60);
    return `<article class="auto-account-card ${selected ? "is-selected" : ""} ${profile.enabled ? "is-enabled" : ""}" data-auto-account="${escapeHtml(profile.account_id)}">
      <button type="button" class="auto-account-main" data-select-auto-account="${escapeHtml(profile.account_id)}">
        <span class="auto-account-avatar">${profile.account_avatar ? `<img src="${escapeHtml(profile.account_avatar)}" alt="" />` : initial}</span>
        <span class="auto-account-body">
          <span class="auto-account-top">
            <strong>${escapeHtml(profile.account_name || "头条账号")}</strong>
            <span class="auto-pill ${profile.enabled ? "is-on" : ""}">${statusLabel}</span>
          </span>
          <span class="auto-account-metrics">
            <span>${interval} 分钟</span>
            <span class="auto-metric-sep">·</span>
            <span>${jobs} 任务</span>
            <span class="auto-metric-sep">·</span>
            <span>${typeLabel}</span>
            <span class="auto-metric-sep">·</span>
            <span>${modeLabel}</span>
            <span class="auto-metric-sep">·</span>
            <span>${Number(profile.challenge_opportunity_count || 0)} 个活动</span>
          </span>
        </span>
      </button>
      <label class="auto-account-switch" title="${profile.enabled ? "停止该账号" : "开启该账号"}">
        <input type="checkbox" data-toggle-auto-account="${escapeHtml(profile.account_id)}" ${profile.enabled ? "checked" : ""} />
        <span class="switch" aria-hidden="true"></span>
      </label>
    </article>`;
  }).join("");

  root.querySelectorAll("[data-select-auto-account]").forEach((button) => {
    button.addEventListener("click", () => selectAutomationAccount(button.dataset.selectAutoAccount));
  });
  root.querySelectorAll("[data-toggle-auto-account]").forEach((input) => {
    input.addEventListener("click", (event) => event.stopPropagation());
    input.addEventListener("change", () => {
      toggleAutomationAccount(input.dataset.toggleAutoAccount, input.checked);
    });
  });
  hydrateIcons();
}


function selectAutomationAccount(accountId) {
  if (!accountId || accountId === state.automationAccountId) return;
  if (state.automationDirty && !window.confirm("当前账号设置尚未保存，切换后将丢弃未保存修改，是否继续？")) {
    renderAutomation(true);
    return;
  }
  state.automationAccountId = accountId;
  state.automationDirty = false;
  renderAutomation(true);
}

async function toggleAutomationAccount(accountId, enabled) {
  const profiles = state.status?.automation?.accounts || [];
  const profile = profiles.find((item) => item.account_id === accountId);
  if (!profile) return;
  try {
    const result = await api("/api/automation", {
      method: "POST",
      body: JSON.stringify(automationPayloadFromProfile(profile, { enabled: Boolean(enabled) })),
    });
    state.status.automation = result;
    if (accountId === state.automationAccountId) state.automationDirty = false;
    renderAutomation(true);
    toast(enabled ? `已开启：${profile.account_name || "账号"}` : `已停止：${profile.account_name || "账号"}`);
  } catch (error) {
    toast(error.message, "error");
    renderAutomation(true);
  }
}

async function setAllAutomationEnabled(enabled) {
  const profiles = state.status?.automation?.accounts || [];
  if (!profiles.length) return toast("请先添加账号", "error");
  if (!window.confirm(enabled ? `确认开启全部 ${profiles.length} 个账号的自动化？` : `确认停止全部账号的自动化？`)) return;
  try {
    for (const profile of profiles) {
      const result = await api("/api/automation", {
        method: "POST",
        body: JSON.stringify(automationPayloadFromProfile(profile, { enabled: Boolean(enabled) })),
      });
      state.status.automation = result;
    }
    state.automationDirty = false;
    renderAutomation(true);
    toast(enabled ? "已全部开启" : "已全部停止");
  } catch (error) {
    toast(error.message, "error");
    await loadStatus();
  }
}

function renderAutomation(force = false) {
  if (!state.status) return;
  const automation = state.status.automation || {};
  const profiles = automation.accounts || [];
  const activeAccountId = state.status.accounts?.active_id;
  if (!profiles.some((profile) => profile.account_id === state.automationAccountId)) {
    state.automationAccountId = profiles.some((profile) => profile.account_id === activeAccountId)
      ? activeAccountId
      : profiles[0]?.account_id || null;
  }
  const accountSelect = document.querySelector("#automation-account");
  accountSelect.innerHTML = profiles.length
    ? profiles.map((profile) => {
        const mark = profile.enabled ? " · 运行中" : "";
        return `<option value="${escapeHtml(profile.account_id)}">${escapeHtml(profile.account_name || "头条账号")}${mark}</option>`;
      }).join("")
    : `<option value="">暂无账号</option>`;
  accountSelect.value = state.automationAccountId || "";
  const profile = profiles.find((item) => item.account_id === state.automationAccountId);
  const stateLabel = document.querySelector("#automation-state");
  stateLabel.textContent = automation.enabled_count
    ? `${automation.enabled_count}/${profiles.length || automation.total_accounts || 0} 账号运行中`
    : "已停止";
  stateLabel.className = `automation-state ${automation.enabled_count ? "active" : ""}`;

  // account list always refreshes so multi-account status stays visible
  if (!(state.automationDirty && !force)) {
    renderAutomationAccountList(profiles);
  } else {
    // still update selection highlight lightly
    renderAutomationAccountList(profiles);
  }

  if (state.automationDirty && !force) return;
  // only lock detail strategy fields; multi-account list stays operable
  document.querySelectorAll(".auto-block-control input, .auto-block-control select, .auto-block-scope input, .auto-block-scope select, .save-automation, .auto-run-card").forEach((control) => {
    if (control.id === "automation-account") return;
    control.disabled = !profile;
  });
  accountSelect.disabled = !profiles.length;
  const enableAll = document.querySelector("#automation-enable-all");
  const disableAll = document.querySelector("#automation-disable-all");
  if (enableAll) enableAll.disabled = !profiles.length;
  if (disableAll) disableAll.disabled = !profiles.length;
  if (!profile) {
    document.querySelector("#account-run-state").textContent = "请先添加账号";
    return;
  }
  document.querySelector("#automation-enabled").checked = Boolean(profile.enabled);
  const autoClaim = document.querySelector("#automation-auto-claim");
  if (autoClaim) autoClaim.checked = profile.auto_claim_challenges !== false;
  document.querySelector("#automation-interval").value = profile.interval_minutes || 60;
  document.querySelector("#automation-count").value = profile.pick_count || 1;
  const contentType = profile.content_type === "video" ? "video" : "article";
  const typeInput = document.querySelector(`input[name="automation-content-type"][value="${contentType}"]`);
  if (typeInput) typeInput.checked = true;
  const durationInput = document.querySelector("#automation-video-duration");
  if (durationInput) durationInput.value = profile.video_duration || 15;
  syncAutomationContentTypeUI(contentType);
  const mode = document.querySelector(`input[name="automation-mode"][value="${profile.mode || "draft"}"]`);
  if (mode) mode.checked = true;
  document.querySelector("#account-run-state").textContent = profile.enabled ? "运行中" : "已停止";
  document.querySelector("#next-run-label").textContent = profile.enabled ? `下次 ${formatDate(profile.next_run)}` : "暂无计划";
  document.querySelector("#last-run-value").textContent = formatDate(profile.last_run);
  document.querySelector("#next-run-value").textContent = formatDate(profile.next_run);
  document.querySelector("#job-total-value").textContent = (state.status.jobs || []).filter((job) => job.account_id === profile.account_id).length;
  const opportunityValue = document.querySelector("#automation-opportunity-value");
  if (opportunityValue) opportunityValue.textContent = `${Number(profile.challenge_opportunity_count || 0)} 个`;
  const claimedValue = document.querySelector("#automation-claimed-value");
  if (claimedValue) claimedValue.textContent = `${Number(profile.last_challenge_claimed || 0)} 个`;
  const rewardValue = document.querySelector("#automation-reward-value");
  if (rewardValue) rewardValue.textContent = profile.challenge_top_reward || "--";
  document.querySelector("#automation-mode-value").textContent = profile.mode === "publish" ? "直接发布" : "草稿";
  document.querySelector("#automation-category-value").textContent = profile.categories?.length ? profile.categories.join("、") : "全部";
  const sourceNames = new Map((state.hotMeta.sources || []).map((item) => [item.id, item.name]));
  document.querySelector("#automation-source-value").textContent = profile.sources?.length ? profile.sources.map((id) => sourceNames.get(id) || id).join("、") : "全部";
  renderScopeOptions(document.querySelector("#automation-categories"), state.hotMeta.categories || [], profile.categories, "全部分类");
  renderScopeOptions(document.querySelector("#automation-sources"), state.hotMeta.sources || [], profile.sources, "全部平台");
  hydrateIcons();
}


function stopHotAutoRefresh() {
  if (state.hotRefreshTimer) {
    clearTimeout(state.hotRefreshTimer);
    state.hotRefreshTimer = null;
  }
}

function scheduleHotAutoRefresh() {
  stopHotAutoRefresh();
  state.hotRefreshTimer = setTimeout(async () => {
    try {
      // force=false 走服务端短缓存；后台线程也会主动刷新
      if (document.querySelector("#view-create")?.classList.contains("active") || document.querySelector("#view-automation")?.classList.contains("active")) {
        await loadHot(false);
      }
    } catch (_) {
      // ignore background failures
    } finally {
      scheduleHotAutoRefresh();
    }
  }, 30000);
}

async function loadHot(force = false) {
  document.querySelector("#source-status").textContent = "更新中";
  try {
    const result = await api(`/api/hot-topics?force=${force}`);
    state.hotTopics = result.topics || [];
    state.hotMeta = result;
    const fallback = state.hotTopics.some((topic) => topic.is_fallback);
    const totalSources = (result.sources || []).length;
    const healthySources = Number(result.healthy_sources || 0);
    document.querySelector("#source-dot").className = `status-dot ${fallback || !healthySources ? "error" : "online"}`;
    document.querySelector("#source-status").textContent = fallback
      ? "备用选题"
      : `${healthySources}/${totalSources} 平台实时 · ${formatDate(result.refreshed_at)}`;
    renderHotFilters();
    renderHotTopics();
    renderStatus();
  } catch (error) {
    document.querySelector("#source-dot").className = "status-dot error";
    document.querySelector("#source-status").textContent = "连接异常";
    toast(error.message, "error");
  }
}

async function loadStatus(showError = true) {
  try {
    state.status = await api("/api/status");
    renderStatus();
  } catch (error) {
    if (showError) toast(error.message, "error");
  }
}

async function createJob(event) {
  event.preventDefault();
  const topic = document.querySelector("#topic-input").value.trim();
  if (!topic) return toast("请输入或选择热点选题", "error");
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const contentType = document.querySelector('input[name="content-type"]:checked').value;
    const autoAction = document.querySelector('input[name="auto-action"]:checked').value || null;
    await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({
        topic,
        guidance: document.querySelector("#guidance-input").value.trim(),
        word_count: Number(document.querySelector("#word-count").value),
        auto_action: autoAction,
        account_id: state.status?.accounts?.active_id || null,
        topic_id: state.selectedTopic?.id || "",
        topic_category: state.selectedTopic?.category || "",
        topic_source: state.selectedTopic?.source || "",
        topic_source_keys: state.selectedTopic?.source_keys || [],
        topic_url: state.selectedTopic?.url || "",
        content_type: contentType,
        video_duration: Number(document.querySelector("#video-duration").value),
        video_aspect_ratio: document.querySelector("#video-aspect-ratio").value,
      }),
    });
    toast(`${contentType === "video" ? "视频" : "图文"}任务已进入生成队列`);
    document.querySelector("#topic-input").value = "";
    state.selectedTopic = null;
    document.querySelector("#selected-rank").textContent = "未选择";
    const guidance = document.querySelector("#guidance-input");
    if (guidance?.dataset.autoFilled === "1") {
      guidance.value = "";
      guidance.dataset.autoFilled = "0";
    }
    await loadStatus();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function updateContentType() {
  const contentType = document.querySelector('input[name="content-type"]:checked')?.value || "article";
  const videoMode = contentType === "video";
  document.querySelector("#video-options").hidden = !videoMode;
  document.querySelector(".compose-panel").classList.toggle("video-mode", videoMode);
  document.querySelector("#word-count").closest("label").hidden = videoMode;
  const button = document.querySelector(".generate-button");
  const title = button.querySelector(".compose-submit-title") || button.querySelector("span");
  const desc = button.querySelector(".compose-submit-desc");
  const icon = button.querySelector("i");
  if (title) title.textContent = videoMode ? "生成并上传视频" : "生成文章与封面";
  if (desc) desc.textContent = videoMode ? "创建视频生产任务并按策略处理" : "创建图文生产任务并按策略处理";
  if (icon) icon.dataset.lucide = videoMode ? "clapperboard" : "sparkles";
  hydrateIcons();
}


const regenLabels = {
  article: "重写文案",
  cover: "重做封面",
  video: "重做视频",
  all: "全部重生成",
};

async function regenerateJob(jobId, target = "all") {
  const job = (state.status?.jobs || []).find((item) => item.id === jobId);
  if (!job) return toast("任务不存在", "error");
  if (runningStatuses.has(job.status)) return toast("任务正在处理中", "error");
  const label = regenLabels[target] || "重新生成";
  if (!window.confirm(`确认${label}？原内容将被覆盖。`)) return;
  try {
    await api(`/api/jobs/${jobId}/regenerate`, {
      method: "POST",
      body: JSON.stringify({ target }),
    });
    toast(`${label}已开始`);
    if (state.editingJobId === jobId) {
      const dialog = document.querySelector("#editor-dialog");
      if (dialog?.open) dialog.close();
      state.editingJobId = null;
    }
    await loadStatus();
    if (state.currentView !== "tasks") switchView("tasks");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function openEditor(jobId) {
  try {
    const article = await api(`/api/jobs/${jobId}/article`);
    const job = state.status.jobs.find((item) => item.id === jobId);
    const videoMode = job?.content_type === "video";
    state.editingJobId = jobId;
    document.querySelector("#dialog-heading").textContent = job?.status === "completed"
      ? `已发布${videoMode ? "视频" : "内容"}`
      : `待发布${videoMode ? "视频" : "草稿"}`;
    document.querySelector("#edit-title").value = article.title || "";
    document.querySelector("#edit-summary").value = article.summary || "";
    document.querySelector("#edit-body").value = article.body_markdown || "";
    document.querySelector("#edit-tags").value = (article.tags || []).join(", ");
    const image = document.querySelector("#dialog-cover");
    const video = document.querySelector("#dialog-video");
    const media = jobMediaUrls(job);
    video.pause();
    video.removeAttribute("src");
    video.classList.remove("ready");
    image.removeAttribute("src");
    image.classList.remove("ready");
    image.onerror = () => {
      image.classList.remove("ready");
      if (!(videoMode && media.video)) document.querySelector("#cover-placeholder").hidden = false;
    };
    video.onerror = () => {
      video.classList.remove("ready");
      if (media.cover) {
        image.src = media.cover.includes("?") ? `${media.cover}&_=${Date.now()}` : `${media.cover}?_=${Date.now()}`;
        image.classList.add("ready");
        document.querySelector("#cover-placeholder").hidden = true;
      } else {
        document.querySelector("#cover-placeholder").hidden = false;
      }
    };
    if (videoMode && media.video) {
      video.src = media.video.includes("?") ? `${media.video}&_=${Date.now()}` : `${media.video}?_=${Date.now()}`;
      video.classList.add("ready");
      document.querySelector("#cover-placeholder").hidden = true;
      video.load();
    } else if (media.cover) {
      image.src = media.cover.includes("?") ? `${media.cover}&_=${Date.now()}` : `${media.cover}?_=${Date.now()}`;
      image.classList.add("ready");
      document.querySelector("#cover-placeholder").hidden = true;
    } else {
      document.querySelector("#cover-placeholder").hidden = false;
    }
    document.querySelector("#upload-draft span").textContent = videoMode ? "上传视频草稿" : "上传草稿";
    document.querySelector("#publish-now span").textContent = videoMode ? "发布视频" : "立即发布";
    const regenMediaLabel = document.querySelector("#regen-media-label");
    if (regenMediaLabel) regenMediaLabel.textContent = videoMode ? "重做视频" : "重做封面";
    const regenMediaBtn = document.querySelector("#regen-media");
    if (regenMediaBtn) {
      regenMediaBtn.dataset.target = videoMode ? "video" : "cover";
      const iconHost = regenMediaBtn.querySelector("[data-lucide]") || regenMediaBtn.querySelector("i");
      if (iconHost) {
        if (iconHost.tagName && iconHost.tagName.toLowerCase() === "svg") {
          iconHost.outerHTML = `<i data-lucide="${videoMode ? "clapperboard" : "image"}"></i>`;
        } else {
          iconHost.setAttribute("data-lucide", videoMode ? "clapperboard" : "image");
        }
      }
    }
    const busy = runningStatuses.has(job?.status);
    ["#regen-article", "#regen-media", "#regen-all", "#save-draft", "#upload-draft", "#publish-now"].forEach((sel) => {
      const el = document.querySelector(sel);
      if (el) el.disabled = Boolean(busy);
    });
    document.querySelector("#editor-dialog").showModal();
    hydrateIcons();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function saveEditor(closeAfter = false) {
  if (!state.editingJobId) return;
  try {
    await api(`/api/jobs/${state.editingJobId}/article`, {
      method: "PUT",
      body: JSON.stringify({
        title: document.querySelector("#edit-title").value,
        summary: document.querySelector("#edit-summary").value,
        body_markdown: document.querySelector("#edit-body").value,
        tags: document.querySelector("#edit-tags").value.split(/[,，]/).map((tag) => tag.trim()).filter(Boolean),
      }),
    });
    toast("草稿已保存");
    await loadStatus();
    if (closeAfter) closeEditor();
  } catch (error) {
    toast(error.message, "error");
    throw error;
  }
}

async function submitJob(jobId, mode) {
  const job = state.status?.jobs?.find((item) => item.id === jobId);
  const contentLabel = job?.content_type === "video" ? "视频" : "文章";
  if (mode === "publish" && !window.confirm(`确认将该${contentLabel}提交到头条号发布？`)) return;
  try {
    await api(`/api/jobs/${jobId}/publish`, { method: "POST", body: JSON.stringify({ mode }) });
    toast(mode === "publish" ? `${contentLabel}已进入发布队列` : `${contentLabel}已进入草稿上传队列`);
    closeEditor();
    await loadStatus();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function submitEditor(mode) {
  try {
    await saveEditor(false);
    await submitJob(state.editingJobId, mode);
  } catch (_) {}
}

async function deleteEditingJob() {
  if (!state.editingJobId || !window.confirm("确认删除该任务记录？")) return;
  try {
    await api(`/api/jobs/${state.editingJobId}`, { method: "DELETE" });
    closeEditor();
    toast("任务已删除");
    await loadStatus();
  } catch (error) {
    toast(error.message, "error");
  }
}

function closeEditor() {
  const dialog = document.querySelector("#editor-dialog");
  if (dialog.open) dialog.close();
  const video = document.querySelector("#dialog-video");
  video.pause();
  video.removeAttribute("src");
  video.load();
  video.classList.remove("ready");
  state.editingJobId = null;
}

async function saveAutomation(event) {
  event.preventDefault();
  try {
    const result = await api("/api/automation", {
      method: "POST",
      body: JSON.stringify(automationPayloadFromForm(state.automationAccountId)),
    });
    state.status.automation = result;
    state.automationDirty = false;
    renderAutomation(true);
    const name = (result.accounts || []).find((item) => item.account_id === state.automationAccountId)?.account_name || "当前账号";
    toast(`已保存「${name}」的自动化策略（${result.enabled_count || 0} 个账号运行中）`);
  } catch (error) {
    toast(error.message, "error");
  }
}

function openAccountDialog() {
  document.querySelector("#session-cookie").value = "";
  document.querySelector("#session-headers").value = "";
  switchLoginMode("qr");
  document.querySelector("#account-dialog").showModal();
  startQrLogin();
}

function closeAccountDialog() {
  stopQrPolling();
  const dialog = document.querySelector("#account-dialog");
  if (dialog.open) dialog.close();
}

function switchLoginMode(mode) {
  document.querySelectorAll(".login-tab").forEach((button) => button.classList.toggle("active", button.dataset.loginMode === mode));
  document.querySelector("#qr-login-panel").hidden = mode !== "qr";
  document.querySelector("#session-form").hidden = mode !== "cookie";
  if (mode !== "qr") stopQrPolling();
  else if (document.querySelector("#account-dialog").open && !state.qrLoginId) startQrLogin();
}

function stopQrPolling() {
  if (state.qrPollTimer) clearTimeout(state.qrPollTimer);
  state.qrPollTimer = null;
  state.qrLoginId = null;
}

async function startQrLogin() {
  stopQrPolling();
  const image = document.querySelector("#login-qrcode");
  image.classList.remove("ready");
  image.removeAttribute("src");
  document.querySelector("#qr-placeholder").hidden = false;
  document.querySelector("#qr-status-title").textContent = "正在获取二维码";
  document.querySelector("#qr-status-detail").textContent = "等待扫码";
  try {
    const result = await api("/api/auth/qr", { method: "POST", body: "{}" });
    state.qrLoginId = result.login_id;
    image.src = result.qrcode;
    image.classList.add("ready");
    document.querySelector("#qr-placeholder").hidden = true;
    document.querySelector("#qr-status-title").textContent = "扫码登录";
    document.querySelector("#qr-status-detail").textContent = "等待手机确认";
    state.qrPollTimer = setTimeout(pollQrLogin, 1000);
  } catch (error) {
    document.querySelector("#qr-status-title").textContent = "二维码获取失败";
    document.querySelector("#qr-status-detail").textContent = error.message;
  }
}

async function pollQrLogin() {
  const loginId = state.qrLoginId;
  if (!loginId) return;
  try {
    const result = await api(`/api/auth/qr/${loginId}`);
    if (result.qrcode) {
      document.querySelector("#login-qrcode").src = result.qrcode;
    }
    if (result.status === "scanned") {
      document.querySelector("#qr-status-title").textContent = "已扫码";
      document.querySelector("#qr-status-detail").textContent = "请在手机端确认登录";
    } else if (result.status === "confirmed") {
      stopQrPolling();
      document.querySelector("#qr-status-title").textContent = "登录成功";
      document.querySelector("#qr-status-detail").textContent = result.account?.name || "账号已保存";
      toast(`账号已保存：${result.account?.name || "头条号"}`);
      await loadStatus();
      setTimeout(closeAccountDialog, 900);
      return;
    } else if (result.status === "expired" || result.status === "error") {
      stopQrPolling();
      document.querySelector("#qr-status-title").textContent = "二维码已失效";
      document.querySelector("#qr-status-detail").textContent = "点击刷新重新获取";
      return;
    }
    state.qrPollTimer = setTimeout(pollQrLogin, 1100);
  } catch (error) {
    stopQrPolling();
    document.querySelector("#qr-status-title").textContent = "登录状态异常";
    document.querySelector("#qr-status-detail").textContent = error.message;
  }
}

async function saveSession(event) {
  event.preventDefault();
  const cookie = document.querySelector("#session-cookie").value.trim();
  if (!cookie) return toast("请输入 Cookie", "error");
  let headers = {};
  const rawHeaders = document.querySelector("#session-headers").value.trim();
  try {
    headers = rawHeaders ? JSON.parse(rawHeaders) : {};
    if (!headers || Array.isArray(headers) || typeof headers !== "object") throw new Error();
  } catch (_) {
    return toast("附加请求头需要是 JSON 对象", "error");
  }
  try {
    await api("/api/session", { method: "POST", body: JSON.stringify({ cookie, headers }) });
    closeAccountDialog();
    toast("账号已导入并通过检测");
    await loadStatus();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function activateAccount(accountId) {
  try {
    const account = await api(`/api/accounts/${accountId}/activate`, { method: "POST", body: "{}" });
    toast(`已切换到 ${account.name}`);
    await loadStatus();
  } catch (error) {
    toast(error.message, "error");
  }
}

function beginRenameAccount(accountId) {
  const row = document.querySelector(`[data-account-row="${CSS.escape(accountId)}"]`);
  if (!row) return;
  const label = row.querySelector(`[data-account-name="${CSS.escape(accountId)}"]`);
  const input = row.querySelector(`[data-account-name-input="${CSS.escape(accountId)}"]`);
  if (!label || !input) return;
  input.value = (label.textContent || "").trim();
  label.hidden = true;
  input.hidden = false;
  input.focus();
  input.select();
}

function cancelRenameAccount(accountId) {
  const row = document.querySelector(`[data-account-row="${CSS.escape(accountId)}"]`);
  if (!row) return;
  const label = row.querySelector(`[data-account-name="${CSS.escape(accountId)}"]`);
  const input = row.querySelector(`[data-account-name-input="${CSS.escape(accountId)}"]`);
  if (!label || !input) return;
  input.hidden = true;
  label.hidden = false;
}

async function commitRenameAccount(accountId, rawName) {
  if (state._renamingAccountId === accountId) return;
  const row = document.querySelector(`[data-account-row="${CSS.escape(accountId)}"]`);
  const input = row?.querySelector(`[data-account-name-input="${CSS.escape(accountId)}"]`);
  if (input) input.hidden = true;
  const account = (state.status?.accounts?.accounts || []).find((item) => item.id === accountId);
  const name = String(rawName || "").trim().replace(/\s+/g, " ");
  if (!account) {
    cancelRenameAccount(accountId);
    return;
  }
  if (!name) {
    toast("账号名称不能为空", "error");
    beginRenameAccount(accountId);
    return;
  }
  if (name === account.name) {
    cancelRenameAccount(accountId);
    return;
  }
  state._renamingAccountId = accountId;
  try {
    const updated = await api(`/api/accounts/${accountId}`, {
      method: "PATCH",
      body: JSON.stringify({ name }),
    });
    toast(`账号名称已更新：${updated.name}`);
    await loadStatus();
  } catch (error) {
    toast(error.message, "error");
    beginRenameAccount(accountId);
  } finally {
    if (state._renamingAccountId === accountId) state._renamingAccountId = null;
  }
}

async function deleteAccount(accountId) {
  if (!window.confirm("确认删除该账号的本地会话？")) return;
  try {
    await api(`/api/accounts/${accountId}`, { method: "DELETE" });
    toast("账号已删除");
    await loadStatus();
  } catch (error) {
    toast(error.message, "error");
  }
}

function openModelDialog(profileId = null) {
  const profiles = state.status?.models?.profiles || [];
  const profile = profiles.find((item) => item.id === profileId);
  state.editingModelId = profile?.id || null;
  document.querySelector("#model-dialog-title").textContent = profile ? "编辑模型" : "添加模型";
  document.querySelector("#model-name").value = profile?.name || "";
  document.querySelector("#model-kind").value = profile?.kind || "article";
  document.querySelector("#model-kind").disabled = Boolean(profile);
  document.querySelector("#model-base-url").value = profile?.base_url || "https://api.openai.com/v1";
  document.querySelector("#model-id").value = profile?.model || "";
  document.querySelector("#model-api-key").value = "";
  document.querySelector("#model-api-key").placeholder = profile?.api_key_configured ? "留空保留现有 Key" : "sk-...";
  document.querySelector("#model-temperature").value = profile?.temperature ?? 0.7;
  document.querySelector("#model-json-mode").checked = profile?.json_mode ?? true;
  document.querySelector("#model-size").value = profile?.size || "1536x1024";
  document.querySelector("#model-quality").value = profile?.quality || "medium";
  document.querySelector("#model-create-path").value = profile?.create_path || "/videos";
  document.querySelector("#model-duration").value = profile?.duration ?? 15;
  document.querySelector("#model-aspect-ratio").value = profile?.aspect_ratio || "16:9";
  document.querySelector("#model-video-size").value = profile?.size || "1280x720";
  document.querySelector("#model-poll-interval").value = profile?.poll_interval ?? 5;
  document.querySelector("#model-timeout").value = profile?.timeout ?? 900;
  updateModelFields();
  document.querySelector("#model-dialog").showModal();
}

function closeModelDialog() {
  const dialog = document.querySelector("#model-dialog");
  if (dialog.open) dialog.close();
  state.editingModelId = null;
}

function updateModelFields() {
  const kind = document.querySelector("#model-kind").value;
  document.querySelectorAll(".article-model-field").forEach((field) => { field.hidden = kind !== "article"; });
  document.querySelectorAll(".cover-model-field").forEach((field) => { field.hidden = kind !== "cover"; });
  document.querySelectorAll(".video-model-field").forEach((field) => { field.hidden = kind !== "video"; });
}

async function saveModel(event) {
  event.preventDefault();
  try {
    const kind = document.querySelector("#model-kind").value;
    await api("/api/models", {
      method: "POST",
      body: JSON.stringify({
        id: state.editingModelId,
        kind,
        name: document.querySelector("#model-name").value.trim(),
        base_url: document.querySelector("#model-base-url").value.trim(),
        model: document.querySelector("#model-id").value.trim(),
        api_key: document.querySelector("#model-api-key").value.trim(),
        temperature: Number(document.querySelector("#model-temperature").value),
        json_mode: document.querySelector("#model-json-mode").checked,
        size: document.querySelector(kind === "video" ? "#model-video-size" : "#model-size").value.trim(),
        quality: document.querySelector("#model-quality").value,
        create_path: document.querySelector("#model-create-path").value.trim(),
        duration: Number(document.querySelector("#model-duration").value),
        aspect_ratio: document.querySelector("#model-aspect-ratio").value,
        poll_interval: Number(document.querySelector("#model-poll-interval").value),
        timeout: Number(document.querySelector("#model-timeout").value),
      }),
    });
    closeModelDialog();
    toast("模型配置已保存并启用");
    await loadStatus();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function activateModel(profileId) {
  try {
    await api(`/api/models/${profileId}/activate`, { method: "POST", body: "{}" });
    toast("模型已启用");
    await loadStatus();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function deleteModel(profileId) {
  if (!window.confirm("确认删除该模型配置？")) return;
  try {
    await api(`/api/models/${profileId}`, { method: "DELETE" });
    toast("模型配置已删除");
    await loadStatus();
  } catch (error) {
    toast(error.message, "error");
  }
}

function switchView(view) {
  if (view === "users" && state.currentUser?.role !== "admin") return;
  state.currentView = view;
  document.querySelectorAll(".view").forEach((item) => item.classList.toggle("active", item.id === `view-${view}`));
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  const titles = { dashboard: "内容工作台", challenges: "创作任务", drafts: "内容库", tasks: "任务进度", automation: "自动化", users: "用户管理", settings: "设置" };
  document.querySelector("#view-title").textContent = titles[view] || "内容工作台";
  if (view === "users") loadAppUsers();
  if (view === "challenges") {
    document.querySelector("#view-challenges .table-wrap").scrollLeft = 0;
    renderChallenges();
    if (!state.challengeLoading && !state.challenges.length) loadChallenges();
  }
  syncChallengeRefresh();
}

function bindEvents() {
  document.querySelectorAll("[data-auth-mode]").forEach((button) => button.addEventListener("click", () => switchAuthMode(button.dataset.authMode)));
  document.querySelector("#login-form").addEventListener("submit", submitLogin);
  document.querySelector("#register-form").addEventListener("submit", submitRegister);
  document.querySelector("#logout-button").addEventListener("click", logout);
  document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
  document.querySelectorAll("[data-view-target]").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.viewTarget)));
  document.querySelector("#refresh-hot").addEventListener("click", () => loadHot(true));
  document.querySelector("#guidance-input")?.addEventListener("input", (event) => {
    if (event.target.dataset.autoFilled === "1") event.target.dataset.autoFilled = "0";
  });
  document.querySelector("#hot-category").addEventListener("change", (event) => { state.hotFilters.category = event.target.value; renderHotTopics(); });
  document.querySelector("#hot-source").addEventListener("change", (event) => { state.hotFilters.source = event.target.value; renderHotTopics(); });
  document.querySelector("#hot-query").addEventListener("input", (event) => { state.hotFilters.query = event.target.value.trim(); renderHotTopics(); });
  document.querySelector("#content-query").addEventListener("input", (event) => { state.contentFilters.query = event.target.value.trim(); state.contentPage = 1; renderContentTable(); });
  document.querySelector("#content-status").addEventListener("change", (event) => { state.contentFilters.status = event.target.value; state.contentPage = 1; renderContentTable(); });
  document.querySelector("#content-account").addEventListener("change", (event) => { state.contentFilters.account = event.target.value; state.contentPage = 1; renderContentTable(); });
  document.querySelector("#content-page-size").addEventListener("change", (event) => { state.contentPageSize = Number(event.target.value); state.contentPage = 1; renderContentTable(); });
  document.querySelector("#content-prev").addEventListener("click", () => { state.contentPage -= 1; renderContentTable(); });
  document.querySelector("#content-next").addEventListener("click", () => { state.contentPage += 1; renderContentTable(); });
  document.querySelector("#task-query").addEventListener("input", (event) => { state.taskFilters.query = event.target.value.trim(); state.taskPage = 1; renderTaskTable(); });
  document.querySelector("#task-status").addEventListener("change", (event) => { state.taskFilters.status = event.target.value; state.taskPage = 1; renderTaskTable(); });
  document.querySelector("#task-account").addEventListener("change", (event) => { state.taskFilters.account = event.target.value; state.taskPage = 1; renderTaskTable(); });
  document.querySelector("#task-page-size").addEventListener("change", (event) => { state.taskPageSize = Number(event.target.value); state.taskPage = 1; renderTaskTable(); });
  document.querySelector("#task-prev").addEventListener("click", () => { state.taskPage -= 1; renderTaskTable(); });
  document.querySelector("#task-next").addEventListener("click", () => { state.taskPage += 1; renderTaskTable(); });
  document.querySelector("#challenge-account").addEventListener("change", (event) => { state.challengeAccountId = event.target.value || null; loadChallenges(true); });
  document.querySelector("#challenge-type").addEventListener("change", (event) => { state.challengeBizId = Number(event.target.value); state.challengeCategory = "全部"; loadChallenges(true); });
  document.querySelector("#challenge-part-status").addEventListener("change", (event) => { state.challengePartStatus = Number(event.target.value); loadChallenges(true); });
  document.querySelector("#challenge-category").addEventListener("change", (event) => { state.challengeCategory = event.target.value; loadChallenges(true); });
  document.querySelector("#challenge-query").addEventListener("change", (event) => { state.challengeQuery = event.target.value.trim(); loadChallenges(true); });
  document.querySelector("#challenge-query").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); state.challengeQuery = event.currentTarget.value.trim(); loadChallenges(true); } });
  document.querySelector("#challenge-page-size").addEventListener("change", (event) => { state.challengePageSize = Number(event.target.value); loadChallenges(true); });
  document.querySelector("#refresh-challenges").addEventListener("click", () => loadChallenges(false));
  document.querySelector("#accept-all-challenges").addEventListener("click", acceptAllChallenges);
  document.querySelector("#challenge-prev").addEventListener("click", () => { state.challengePage -= 1; loadChallenges(); });
  document.querySelector("#challenge-next").addEventListener("click", () => { state.challengePage += 1; loadChallenges(); });
  document.querySelector("#refresh-all").addEventListener("click", async () => { await Promise.all([loadHot(true), loadStatus()]); toast("数据已刷新"); });
  document.querySelector("#generate-form").addEventListener("submit", createJob);
  document.querySelectorAll('input[name="content-type"]').forEach((input) => input.addEventListener("change", updateContentType));
  document.querySelector("#automation-form").addEventListener("submit", saveAutomation);
  document.querySelectorAll('input[name="automation-content-type"]').forEach((input) => {
    input.addEventListener("change", () => {
      syncAutomationContentTypeUI(input.value);
      state.automationDirty = true;
    });
  });
  document.querySelector("#automation-form").addEventListener("change", (event) => {
    if (event.target.id === "automation-account") {
      selectAutomationAccount(event.target.value);
    } else if (event.target.closest(".auto-account-list")) {
      // handled by dedicated toggle listeners
    } else {
      state.automationDirty = true;
    }
  });
  document.querySelector("#automation-enable-all")?.addEventListener("click", () => setAllAutomationEnabled(true));
  document.querySelector("#automation-disable-all")?.addEventListener("click", () => setAllAutomationEnabled(false));
  document.querySelector("#session-button").addEventListener("click", openAccountDialog);
  document.querySelector("#add-account").addEventListener("click", openAccountDialog);
  document.querySelector("#session-form").addEventListener("submit", saveSession);
  document.querySelector("#close-account").addEventListener("click", closeAccountDialog);
  document.querySelector("#account-dialog").addEventListener("close", stopQrPolling);
  document.querySelector("#refresh-qr").addEventListener("click", startQrLogin);
  document.querySelectorAll("[data-login-mode]").forEach((button) => button.addEventListener("click", () => switchLoginMode(button.dataset.loginMode)));
  document.querySelector("#add-model").addEventListener("click", () => openModelDialog());
  document.querySelector("#model-form").addEventListener("submit", saveModel);
  document.querySelector("#close-model").addEventListener("click", closeModelDialog);
  document.querySelector("#model-dialog").addEventListener("close", () => { state.editingModelId = null; });
  document.querySelector("#model-kind").addEventListener("change", updateModelFields);
  document.querySelector("#close-challenge-dialog").addEventListener("click", closeChallengeDialog);
  document.querySelector("#challenge-accept-detail").addEventListener("click", () => acceptChallenge(state.currentChallenge?.id));
  document.querySelector("#challenge-generate-form").addEventListener("submit", createChallengeJob);
  document.querySelector("#challenge-generate-type").addEventListener("change", updateChallengeGenerateFields);
  document.querySelector("#challenge-dialog").addEventListener("close", () => { state.currentChallenge = null; });
  document.querySelector("#close-dialog").addEventListener("click", closeEditor);
  document.querySelector("#regen-article").addEventListener("click", () => {
    if (!state.editingJobId) return;
    regenerateJob(state.editingJobId, "article");
  });
  document.querySelector("#regen-media").addEventListener("click", () => {
    if (!state.editingJobId) return;
    const t = document.querySelector("#regen-media")?.dataset.target || "cover";
    regenerateJob(state.editingJobId, t);
  });
  document.querySelector("#regen-all").addEventListener("click", () => {
    if (!state.editingJobId) return;
    regenerateJob(state.editingJobId, "all");
  });
  document.querySelector("#save-draft").addEventListener("click", () => saveEditor(false));
  document.querySelector("#upload-draft").addEventListener("click", () => submitEditor("draft"));
  document.querySelector("#publish-now").addEventListener("click", () => submitEditor("publish"));
  document.querySelector("#delete-job").addEventListener("click", deleteEditingJob);
  document.querySelector("#add-app-user").addEventListener("click", () => openUserDialog());
  document.querySelector("#user-form").addEventListener("submit", saveAppUser);
  document.querySelector("#close-user-dialog").addEventListener("click", closeUserDialog);
  document.querySelector("#cancel-user-dialog").addEventListener("click", closeUserDialog);
  document.querySelector("#delete-app-user").addEventListener("click", deleteAppUser);
  document.querySelector("#user-dialog").addEventListener("close", () => { state.editingUserId = null; });
  document.querySelector("#user-query").addEventListener("input", (event) => { state.userFilters.query = event.target.value.trim(); state.userPage = 1; renderAppUsers(); });
  document.querySelector("#user-role-filter").addEventListener("change", (event) => { state.userFilters.role = event.target.value; state.userPage = 1; renderAppUsers(); });
  document.querySelector("#user-status-filter").addEventListener("change", (event) => { state.userFilters.status = event.target.value; state.userPage = 1; renderAppUsers(); });
  document.querySelector("#user-page-size").addEventListener("change", (event) => { state.userPageSize = Number(event.target.value); state.userPage = 1; renderAppUsers(); });
  document.querySelector("#user-prev").addEventListener("click", () => { state.userPage -= 1; renderAppUsers(); });
  document.querySelector("#user-next").addEventListener("click", () => { state.userPage += 1; renderAppUsers(); });
}

async function init() {
  document.querySelector("#today-label").textContent = new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "long", day: "numeric", weekday: "long" }).format(new Date());
  bindEvents();
  updateContentType();
  hydrateIcons();
  await bindMediaPreviewControls();
  loadAuth();
}

init();
