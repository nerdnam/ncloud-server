/* ncloud 프론트엔드 */
const $ = (id) => document.getElementById(id);

let currentPath = "";
let currentSpace = "home";
let currentUser = null; // {id, username, is_admin}
let spacesById = {};   // id → {id, name, readonly}
let setupMode = false;

/* ---------- 보기 방식 (그리드 / 촘촘히 / 목록) ---------- */
const VIEW_MODES = ["grid", "compact", "list"];
let viewMode = localStorage.getItem("ncloud_view");
if (!VIEW_MODES.includes(viewMode)) viewMode = "grid";

function setView(mode) {
  viewMode = mode;
  localStorage.setItem("ncloud_view", mode);
  const list = $("file-list");
  list.classList.toggle("view-compact", mode === "compact");
  list.classList.toggle("view-list", mode === "list");
  for (const m of VIEW_MODES) {
    $(`view-${m}`).classList.toggle("active", m === mode);
  }
}

for (const m of VIEW_MODES) {
  $(`view-${m}`).addEventListener("click", () => setView(m));
}
setView(viewMode);

/* ---------- API ---------- */
// 로그인 없이도 호출되는 엔드포인트 — 이들의 401은 새로고침하지 않는다
const AUTH_PUBLIC = ["/api/auth/status", "/api/auth/login", "/api/auth/setup"];

async function api(url, options = {}) {
  const res = await fetch(url, options);
  if (res.status === 401 && !AUTH_PUBLIC.some((p) => url.startsWith(p))) {
    location.reload(); // 세션 만료 → 로그인 화면으로
    throw new Error("로그인이 필요합니다");
  }
  if (!res.ok) {
    let msg = `오류 (${res.status})`;
    try {
      const detail = (await res.json()).detail;
      if (typeof detail === "string") msg = detail;
      else if (Array.isArray(detail) && detail[0]?.msg) msg = detail[0].msg; // pydantic 422
    } catch {}
    throw new Error(msg);
  }
  return res.json();
}

const postJSON = (url, body) =>
  api(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

/* ---------- 초기화 ---------- */
async function boot() {
  try {
    const status = await api("/api/auth/status");
    if (status.user) {
      showApp(status.user);
    } else {
      setupMode = status.setup_needed;
      showAuth();
    }
  } catch (err) {
    showAuth();
    $("auth-error").textContent = `서버에 연결할 수 없습니다 — ${err.message}`;
  }
}

function showAuth() {
  $("app-screen").classList.add("hidden");
  $("auth-screen").classList.remove("hidden");
  $("auth-subtitle").textContent = setupMode
    ? "첫 실행입니다. 관리자 계정을 만들어주세요."
    : "로그인";
  $("auth-submit").textContent = setupMode ? "계정 만들기" : "로그인";
}

async function showApp(user) {
  currentUser = user;
  $("auth-screen").classList.add("hidden");
  $("app-screen").classList.remove("hidden");
  $("whoami").textContent = user.is_admin ? `${user.username} (관리자)` : user.username;
  $("admin-btn").classList.toggle("hidden", !user.is_admin);
  // URL 해시(#/저장소/경로)가 있으면 그 위치에서 시작 (딥링크/새로고침 유지)
  const target = parseHash();
  if (target) currentSpace = target.space;
  try {
    await loadSpaces(); // 존재하지 않는 저장소면 home으로 되돌린다
  } catch {
    // 저장소 목록을 못 불러와도 홈은 쓸 수 있게 한다
  }
  loadDir(target && spacesById[target.space] ? target.path : "", { push: false });
}

/* ---------- 브라우저 히스토리 (뒤로가기 = 이전 폴더) ---------- */
function buildHash() {
  const segs = [
    encodeURIComponent(currentSpace),
    ...currentPath.split("/").filter(Boolean).map(encodeURIComponent),
  ];
  return "#/" + segs.join("/");
}

function parseHash() {
  if (!location.hash.startsWith("#/")) return null;
  const segs = location.hash.slice(2).split("/").map((s) => {
    try { return decodeURIComponent(s); } catch { return s; }
  });
  return { space: segs[0] || "home", path: segs.slice(1).join("/") };
}

function syncHistory(push) {
  const newHash = buildHash();
  if (push && location.hash !== newHash) {
    history.pushState(null, "", newHash);
  } else {
    history.replaceState(null, "", newHash);
  }
}

window.addEventListener("popstate", () => {
  // 미리보기가 열려 있으면 뒤로가기는 미리보기 닫기로 동작
  if (!$("preview-modal").classList.contains("hidden")) {
    destroyPreview();
    return;
  }
  if (!currentUser) return;
  const target = parseHash() || { space: "home", path: "" };
  if (!spacesById[target.space]) target.space = "home";
  if (target.space === currentSpace && target.path === currentPath) return;
  currentSpace = target.space;
  currentPath = target.path;
  document.querySelectorAll(".space-item").forEach((el) =>
    el.classList.toggle("active", el.dataset.space === currentSpace)
  );
  updateWriteUI();
  loadDir(target.path, { push: false });
});

/* ---------- 저장소(스페이스) ---------- */
async function loadSpaces() {
  const data = await api("/api/files/spaces");
  spacesById = {};
  const nav = $("space-list");
  nav.innerHTML = "";
  for (const space of data.spaces) {
    spacesById[space.id] = space;
    const btn = document.createElement("button");
    btn.className = "space-item" + (space.id === currentSpace ? " active" : "");
    btn.dataset.space = space.id;
    const icon = space.id === "home" ? "🏠" : "💾";
    btn.innerHTML = `<span>${icon}</span>`;
    const label = document.createElement("span");
    label.textContent = space.name;
    btn.appendChild(label);
    if (space.readonly) {
      const ro = document.createElement("span");
      ro.className = "ro";
      ro.textContent = "🔒";
      ro.title = "읽기 전용";
      btn.appendChild(ro);
    }
    btn.onclick = () => switchSpace(space.id);
    nav.appendChild(btn);
  }
  if (!spacesById[currentSpace]) currentSpace = "home";
  updateWriteUI();
}

function switchSpace(id) {
  currentSpace = id;
  currentPath = ""; // 이전 저장소의 경로가 새 저장소의 쓰기 작업에 쓰이지 않게 즉시 초기화
  document.querySelectorAll(".space-item").forEach((el) =>
    el.classList.toggle("active", el.dataset.space === id)
  );
  updateWriteUI();
  loadDir("");
}

function isReadonly() {
  return !!spacesById[currentSpace]?.readonly;
}

function updateWriteUI() {
  const ro = isReadonly();
  $("readonly-badge").classList.toggle("hidden", !ro);
  $("mkdir-btn").classList.toggle("hidden", ro);
  $("upload-btn").classList.toggle("hidden", ro);
}

/* ---------- 인증 ---------- */
$("auth-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("auth-error").textContent = "";
  try {
    const body = {
      username: $("auth-username").value.trim(),
      password: $("auth-password").value,
    };
    const url = setupMode ? "/api/auth/setup" : "/api/auth/login";
    await postJSON(url, body);
    boot(); // status를 다시 읽어 관리자 여부까지 반영
  } catch (err) {
    $("auth-error").textContent = err.message;
  }
});

$("logout-btn").addEventListener("click", async () => {
  await postJSON("/api/auth/logout", {});
  location.reload();
});

/* ---------- 파일 목록 ---------- */
function fileUrl(endpoint, path) {
  return `/api/files/${endpoint}?space=${encodeURIComponent(currentSpace)}&path=${encodeURIComponent(path)}`;
}

let loadSeq = 0; // 마지막 요청만 화면에 반영 (느린 응답이 최신 화면을 덮는 것 방지)

async function loadDir(path, { push = true } = {}) {
  const seq = ++loadSeq;
  try {
    const data = await api(fileUrl("list", path));
    if (seq !== loadSeq) return; // 더 새로운 요청이 이미 나감
    currentPath = data.path;
    syncHistory(push);
    renderBreadcrumb();
    renderEntries(data.entries);
  } catch (err) {
    if (seq !== loadSeq) return;
    alert(err.message);
    if (path !== "") {
      loadDir("", { push: false }); // 하위 폴더 오류 → 저장소 루트로
    } else if (currentSpace !== "home") {
      // 마운트가 사라진 경우 → 홈으로 복귀하고 목록 갱신
      currentSpace = "home";
      currentPath = "";
      try { await loadSpaces(); } catch {}
      loadDir("", { push: false });
    }
  }
}

function renderBreadcrumb() {
  const nav = $("breadcrumb");
  nav.innerHTML = "";
  const parts = currentPath ? currentPath.split("/") : [];
  const home = document.createElement("a");
  home.href = "#";
  const spaceInfo = spacesById[currentSpace];
  home.textContent =
    currentSpace === "home" ? "🏠 내 파일" : `💾 ${spaceInfo ? spaceInfo.name : currentSpace}`;
  home.onclick = (e) => { e.preventDefault(); loadDir(""); };
  nav.appendChild(home);
  let acc = "";
  parts.forEach((part, i) => {
    acc = acc ? `${acc}/${part}` : part;
    const sep = document.createElement("span");
    sep.className = "sep";
    sep.textContent = "›";
    nav.appendChild(sep);
    if (i === parts.length - 1) {
      const cur = document.createElement("span");
      cur.className = "current";
      cur.textContent = part;
      nav.appendChild(cur);
    } else {
      const a = document.createElement("a");
      a.href = "#";
      a.textContent = part;
      const target = acc;
      a.onclick = (e) => { e.preventDefault(); loadDir(target); };
      nav.appendChild(a);
    }
  });
}

const KIND_ICONS = { folder: "📁", image: "🖼️", video: "🎬", audio: "🎵", file: "📄" };

function formatSize(bytes) {
  if (bytes === 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, n = bytes;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function renderEntries(entries) {
  const list = $("file-list");
  list.innerHTML = "";
  $("empty-hint").classList.toggle("hidden", entries.length > 0);

  for (const entry of entries) {
    const card = document.createElement("div");
    card.className = "entry";

    const icon = document.createElement("div");
    icon.className = "icon";
    if (entry.kind === "image") {
      const img = document.createElement("img");
      img.loading = "lazy";
      img.src = fileUrl("thumb", entry.path);
      img.onerror = () => { icon.textContent = KIND_ICONS.image; };
      icon.appendChild(img);
    } else {
      icon.textContent = KIND_ICONS[entry.kind] || KIND_ICONS.file;
    }
    card.appendChild(icon);

    const name = document.createElement("div");
    name.className = "name";
    name.textContent = entry.name;
    name.title = entry.name;
    card.appendChild(name);

    const meta = document.createElement("div");
    meta.className = "meta";
    const date = new Date(entry.mtime * 1000).toLocaleDateString("ko-KR");
    meta.textContent = entry.is_dir ? date : `${formatSize(entry.size)} · ${date}`;
    card.appendChild(meta);

    const actions = document.createElement("div");
    actions.className = "actions";
    if (!entry.is_dir) {
      const dl = document.createElement("a");
      dl.textContent = "⬇";
      dl.title = "다운로드";
      dl.href = fileUrl("download", entry.path);
      dl.onclick = (e) => e.stopPropagation();
      actions.appendChild(dl);
    }
    if (!isReadonly()) {
      const rn = document.createElement("button");
      rn.textContent = "✏";
      rn.title = "이름 변경";
      rn.onclick = (e) => { e.stopPropagation(); renameEntry(entry); };
      actions.appendChild(rn);
      const del = document.createElement("button");
      del.textContent = "🗑";
      del.title = "삭제";
      del.className = "del";
      del.onclick = (e) => { e.stopPropagation(); deleteEntry(entry); };
      actions.appendChild(del);
    }
    card.appendChild(actions);

    card.onclick = () => {
      if (entry.is_dir) loadDir(entry.path);
      else if (["image", "video", "audio"].includes(entry.kind)) openPreview(entry);
      else location.href = fileUrl("download", entry.path);
    };

    list.appendChild(card);
  }
}

/* ---------- 폴더 생성 / 이름 변경 / 삭제 ---------- */
$("mkdir-btn").addEventListener("click", async () => {
  const name = prompt("새 폴더 이름:");
  if (!name) return;
  try {
    await postJSON("/api/files/mkdir", {
      path: currentPath ? `${currentPath}/${name}` : name,
      space: currentSpace,
    });
    loadDir(currentPath);
  } catch (err) { alert(err.message); }
});

async function renameEntry(entry) {
  const name = prompt("새 이름:", entry.name);
  if (!name || name === entry.name) return;
  try {
    await postJSON("/api/files/rename", {
      path: entry.path,
      new_name: name,
      space: currentSpace,
    });
    loadDir(currentPath);
  } catch (err) { alert(err.message); }
}

async function deleteEntry(entry) {
  const label = entry.is_dir ? "폴더(안의 내용 포함)" : "파일";
  if (!confirm(`"${entry.name}" ${label}을(를) 삭제할까요?`)) return;
  try {
    await postJSON("/api/files/delete", { path: entry.path, space: currentSpace });
    loadDir(currentPath);
  } catch (err) { alert(err.message); }
}

/* ---------- 업로드 ---------- */
$("upload-btn").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", (e) => {
  uploadFiles(e.target.files);
  e.target.value = "";
});

async function uploadFiles(fileList) {
  if (!fileList.length) return;
  if (isReadonly()) {
    alert("읽기 전용 저장소에는 업로드할 수 없습니다");
    return;
  }
  const form = new FormData();
  for (const f of fileList) form.append("files", f);
  const status = $("upload-status");
  status.textContent = `⬆ ${fileList.length}개 파일 업로드 중...`;
  status.classList.remove("hidden");
  try {
    await api(fileUrl("upload", currentPath), { method: "POST", body: form });
    loadDir(currentPath);
  } catch (err) {
    alert(err.message);
  } finally {
    status.classList.add("hidden");
  }
}

/* 드래그 앤 드롭 */
let dragDepth = 0;
document.addEventListener("dragenter", (e) => {
  e.preventDefault();
  if (e.dataTransfer?.types?.includes("Files")) {
    dragDepth++;
    document.body.classList.add("dragging");
  }
});
document.addEventListener("dragleave", () => {
  if (--dragDepth <= 0) {
    dragDepth = 0;
    document.body.classList.remove("dragging");
  }
});
document.addEventListener("dragover", (e) => e.preventDefault());
document.addEventListener("drop", (e) => {
  e.preventDefault();
  dragDepth = 0;
  document.body.classList.remove("dragging");
  if (!$("app-screen").classList.contains("hidden")) {
    uploadFiles(e.dataTransfer.files);
  }
});

/* ---------- 미리보기 ---------- */
let previewPushed = false; // 미리보기 열 때 히스토리 항목을 추가했는지

function openPreview(entry) {
  const modal = $("preview-modal");
  const content = $("preview-content");
  content.innerHTML = "";
  $("preview-name").textContent = entry.name;
  $("preview-download").href = fileUrl("download", entry.path);
  history.pushState({ ncPreview: true }, "", location.hash || buildHash());
  previewPushed = true;

  const rawUrl = fileUrl("raw", entry.path);
  let el;
  if (entry.kind === "image") {
    el = document.createElement("img");
    el.src = rawUrl;
  } else if (entry.kind === "video") {
    el = document.createElement("video");
    el.src = rawUrl;
    el.controls = true;
    el.autoplay = true;
  } else {
    el = document.createElement("audio");
    el.src = rawUrl;
    el.controls = true;
    el.autoplay = true;
  }
  content.appendChild(el);
  modal.classList.remove("hidden");
}

function destroyPreview() {
  previewPushed = false;
  $("preview-modal").classList.add("hidden");
  $("preview-content").innerHTML = ""; // 비디오/오디오 정지
}

function closePreview() {
  if (previewPushed) {
    history.back(); // popstate 핸들러가 destroyPreview()를 호출한다
  } else {
    destroyPreview();
  }
}

$("preview-close").addEventListener("click", closePreview);
document.querySelector(".preview-backdrop").addEventListener("click", closePreview);
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("preview-modal").classList.contains("hidden")) {
    closePreview();
    return;
  }
  document.querySelectorAll(".modal:not(.hidden)").forEach((m) => m.classList.add("hidden"));
});

/* ---------- 공용 모달 ---------- */
document.querySelectorAll(".modal [data-close]").forEach((el) => {
  el.addEventListener("click", () => el.closest(".modal").classList.add("hidden"));
});

/* ---------- 비밀번호 변경 ---------- */
$("pw-btn").addEventListener("click", () => {
  $("pw-form").reset();
  $("pw-error").textContent = "";
  $("pw-modal").classList.remove("hidden");
  $("pw-current").focus();
});

$("pw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("pw-error").textContent = "";
  if ($("pw-new").value !== $("pw-new2").value) {
    $("pw-error").textContent = "새 비밀번호가 서로 다릅니다";
    return;
  }
  try {
    await postJSON("/api/auth/change-password", {
      current_password: $("pw-current").value,
      new_password: $("pw-new").value,
    });
    $("pw-modal").classList.add("hidden");
    alert("비밀번호가 변경되었습니다. 다른 기기의 세션은 로그아웃됩니다.");
  } catch (err) {
    $("pw-error").textContent = err.message;
  }
});

/* ---------- 사용자 관리 (관리자) ---------- */
$("admin-btn").addEventListener("click", () => {
  $("admin-error").textContent = "";
  $("admin-modal").classList.remove("hidden");
  loadUsers();
});

async function loadUsers() {
  try {
    const data = await api("/api/admin/users");
    $("admin-error").textContent = ""; // 이전 작업의 오류 메시지 제거
    renderUsers(data.users);
  } catch (err) {
    $("admin-error").textContent = err.message;
  }
}

function renderUsers(users) {
  const tbody = document.querySelector("#user-table tbody");
  tbody.innerHTML = "";
  for (const u of users) {
    const tr = document.createElement("tr");

    const tdName = document.createElement("td");
    tdName.textContent = u.username;
    tr.appendChild(tdName);

    const tdRole = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = u.is_admin ? "badge" : "badge plain";
    badge.textContent = u.is_admin ? "관리자" : "사용자";
    tdRole.appendChild(badge);
    tr.appendChild(tdRole);

    const tdDate = document.createElement("td");
    tdDate.textContent = (u.created_at || "").slice(0, 10);
    tr.appendChild(tdDate);

    const tdUsage = document.createElement("td");
    tdUsage.textContent = u.usage_bytes ? formatSize(u.usage_bytes) : "0 B";
    tr.appendChild(tdUsage);

    const tdActions = document.createElement("td");
    const wrap = document.createElement("div");
    wrap.className = "user-actions";
    if (u.id === currentUser.id) {
      const me = document.createElement("span");
      me.className = "me-note";
      me.textContent = "본인";
      wrap.appendChild(me);
    } else {
      const pw = document.createElement("button");
      pw.textContent = "비밀번호 재설정";
      pw.onclick = () => adminResetPassword(u);
      wrap.appendChild(pw);

      const role = document.createElement("button");
      role.textContent = u.is_admin ? "관리자 해제" : "관리자 지정";
      role.onclick = () => adminSetRole(u);
      wrap.appendChild(role);

      const del = document.createElement("button");
      del.textContent = "삭제";
      del.className = "danger";
      del.onclick = () => adminDeleteUser(u);
      wrap.appendChild(del);
    }
    tdActions.appendChild(wrap);
    tr.appendChild(tdActions);

    tbody.appendChild(tr);
  }
}

async function adminResetPassword(u) {
  const pw = prompt(`"${u.username}"의 새 비밀번호 (4자 이상):`);
  if (!pw) return;
  try {
    await postJSON("/api/admin/users/reset-password", { user_id: u.id, new_password: pw });
    alert(`"${u.username}"의 비밀번호가 재설정되었습니다. 해당 사용자는 다시 로그인해야 합니다.`);
  } catch (err) {
    alert(err.message);
  }
}

async function adminSetRole(u) {
  const action = u.is_admin ? "관리자 권한을 해제" : "관리자로 지정";
  if (!confirm(`"${u.username}"을(를) ${action}할까요?`)) return;
  try {
    await postJSON("/api/admin/users/set-admin", { user_id: u.id, is_admin: !u.is_admin });
    loadUsers();
  } catch (err) {
    alert(err.message);
  }
}

async function adminDeleteUser(u) {
  if (!confirm(`"${u.username}" 계정을 삭제할까요?`)) return;
  const deleteFiles = confirm("이 사용자의 파일도 함께 삭제할까요?\n(취소를 누르면 파일은 남겨둡니다)");
  try {
    await postJSON("/api/admin/users/delete", { user_id: u.id, delete_files: deleteFiles });
    loadUsers();
  } catch (err) {
    alert(err.message);
  }
}

$("add-user-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("admin-error").textContent = "";
  try {
    await postJSON("/api/admin/users", {
      username: $("new-username").value.trim(),
      password: $("new-password").value,
      is_admin: $("new-is-admin").checked,
    });
    $("add-user-form").reset();
    loadUsers();
  } catch (err) {
    $("admin-error").textContent = err.message;
  }
});

boot();
