/* genDISK 프론트엔드 */
const $ = (id) => document.getElementById(id);

let currentPath = "";
let currentSpace = "home";
let currentUser = null; // {id, username, is_admin}
let spacesById = {};   // id → {id, name, readonly}
let setupMode = false;
let currentEntries = [];        // 현재 폴더의 항목(일괄 작업이 참조)
const selection = new Set();    // 선택된 항목의 path

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

/* ---------- 테마 (다크 / 라이트) ---------- */
(function () {
  const html = document.documentElement;
  const btn = $("theme-btn");
  function apply(theme) {
    html.setAttribute("data-theme", theme);
    if (btn) {
      btn.textContent = theme === "dark" ? "☀️" : "🌙";
      btn.title = theme === "dark" ? "라이트 모드 전환" : "다크 모드 전환";
    }
  }
  // 테마 자체는 <head> 인라인 스크립트가 이미 적용함 — 여기선 버튼 아이콘만 동기화.
  apply(html.getAttribute("data-theme") === "dark" ? "dark" : "light");
  if (btn) {
    btn.addEventListener("click", () => {
      const next = html.getAttribute("data-theme") === "dark" ? "light" : "dark";
      apply(next);
      try { localStorage.setItem("ncloud_theme", next); } catch (e) {}
    });
  }
  // 사용자가 직접 고르지 않았으면 시스템 테마 변경을 따라간다.
  try {
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
      if (!localStorage.getItem("ncloud_theme")) apply(e.matches ? "dark" : "light");
    });
  } catch (e) {}
})();

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
  $("storage-btn").classList.toggle("hidden", !user.is_admin);
  // URL 해시(#/저장소/경로)가 있으면 그 위치에서 시작 (딥링크/새로고침 유지)
  const target = parseHash();
  if (target) currentSpace = target.space;
  try {
    await loadSpaces(); // 존재하지 않는 저장소면 home으로 되돌린다
  } catch {
    // 저장소 목록을 못 불러와도 홈은 쓸 수 있게 한다
  }
  loadUsage();
  loadWinDownload();
  loadDir(target && spacesById[target.space] ? target.path : "", { push: false });
}

async function loadWinDownload() {
  try {
    const info = await api("/api/download/info");
    if (info.windows) {
      const a = $("win-download");
      a.title = `${info.windows.name} (${formatSize(info.windows.size)})`;
      a.classList.remove("hidden");
    }
  } catch {
    // 다운로드 정보는 실패해도 무시
  }
}

async function loadUsage() {
  try {
    const u = await api("/api/files/usage");
    const badge = $("usage-badge");
    badge.textContent = u.quota_bytes
      ? `${formatSize(u.usage_bytes)} / ${formatSize(u.quota_bytes)}`
      : `${formatSize(u.usage_bytes)} 사용`;
    badge.classList.remove("hidden");
  } catch {
    // 사용량 표시는 실패해도 무시
  }
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
  selection.clear();   // 폴더 이동/새로고침 시 선택 초기화(목록은 아래에서 새로 그려짐)
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
  currentEntries = entries;
  $("empty-hint").classList.toggle("hidden", entries.length > 0);

  for (const entry of entries) {
    const card = document.createElement("div");
    card.className = "entry";
    if (selection.has(entry.path)) card.classList.add("selected");

    const check = document.createElement("input");
    check.type = "checkbox";
    check.className = "checkbox";
    check.title = "선택";
    check.checked = selection.has(entry.path);
    check.onclick = (e) => { e.stopPropagation(); toggleSelect(entry, card, e.target.checked); };
    card.appendChild(check);

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
    // 공유는 읽기 전용 저장소에서도 가능(외부 사용자에게 다운로드 링크 제공)
    const sh = document.createElement("button");
    sh.textContent = "🔗";
    sh.title = "공유 링크 만들기";
    sh.onclick = (e) => { e.stopPropagation(); openShareModal(currentSpace, [entry.path], entry.name); };
    actions.appendChild(sh);
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
  updateSelectionUI();
}

/* ---------- 다중 선택 ---------- */
function toggleSelect(entry, card, checked) {
  if (checked) { selection.add(entry.path); card.classList.add("selected"); }
  else { selection.delete(entry.path); card.classList.remove("selected"); }
  updateSelectionUI();
}

function updateSelectionUI() {
  const n = selection.size;
  $("file-list").classList.toggle("has-selection", n > 0);
  $("selection-bar").classList.toggle("hidden", n === 0);
  if (n > 0) {
    $("sel-count").textContent = `${n}개 선택`;
    const ro = isReadonly();
    $("sel-move").classList.toggle("hidden", ro);
    $("sel-delete").classList.toggle("hidden", ro);
    // 공유: 1개면 단일 링크, 여러 개면 컬렉션 링크 (읽기 전용에서도 허용)
    $("sel-share").classList.toggle("hidden", n < 1);
    const allSelected = currentEntries.length > 0 && currentEntries.every((e) => selection.has(e.path));
    $("sel-all").textContent = allSelected ? "전체 해제" : "전체 선택";
  }
}

function clearSelection() {
  selection.clear();
  const list = $("file-list");
  list.querySelectorAll(".entry.selected").forEach((c) => c.classList.remove("selected"));
  list.querySelectorAll(".checkbox:checked").forEach((cb) => { cb.checked = false; });
  updateSelectionUI();
}

function toggleSelectAll() {
  const allSelected = currentEntries.length > 0 && currentEntries.every((e) => selection.has(e.path));
  if (allSelected) selection.clear();
  else currentEntries.forEach((e) => selection.add(e.path));
  $("file-list").querySelectorAll(".entry").forEach((card, i) => {
    const e = currentEntries[i];
    const sel = !!(e && selection.has(e.path));
    card.classList.toggle("selected", sel);
    const cb = card.querySelector(".checkbox");
    if (cb) cb.checked = sel;
  });
  updateSelectionUI();
}

async function bulkDelete() {
  const paths = [...selection];
  if (!paths.length) return;
  if (!confirm(`선택한 ${paths.length}개 항목을 삭제할까요? 삭제한 항목은 복구할 수 없습니다.`)) return;
  let failed = 0;
  for (const p of paths) {
    try { await postJSON("/api/files/delete", { path: p, space: currentSpace }); }
    catch { failed++; }
  }
  clearSelection();
  loadDir(currentPath);
  loadUsage();
  if (failed) alert(`${failed}개 항목을 삭제하지 못했습니다.`);
}

async function bulkMove() {
  const paths = [...selection];
  if (!paths.length) return;
  // 선택한 폴더 자신·그 하위는 이동 대상이 될 수 없다(자기 안으로 이동 금지).
  const blocked = currentEntries
    .filter((e) => e.is_dir && selection.has(e.path))
    .map((e) => e.path);
  const destFolder = await pickFolder(currentPath, blocked);
  if (destFolder === null) return;   // 취소
  let failed = 0, skipped = 0;
  for (const p of paths) {
    const name = p.split("/").pop();
    const dst = destFolder ? `${destFolder}/${name}` : name;
    if (dst === p) { skipped++; continue; }
    try { await postJSON("/api/files/move", { src: p, dst, space: currentSpace }); }
    catch { failed++; }
  }
  clearSelection();
  loadDir(currentPath);
  if (failed) alert(`${failed}개 항목을 이동하지 못했습니다.${skipped ? ` (${skipped}개는 같은 위치라 건너뜀)` : ""}`);
}

/* ---------- 폴더 선택 모달(이동 대상 고르기) ---------- */
let movePickerPath = "";       // 지금 탐색 중인 폴더
let movePickerBlocked = [];    // 대상이 될 수 없는 폴더(선택된 폴더와 그 하위)
let movePickerResolve = null;  // pickFolder 프로미스의 resolve

// 현재 저장소 안에서 목적지 폴더를 골라 그 경로(문자열)를 돌려준다. 취소하면 null.
function pickFolder(startPath, blocked = []) {
  movePickerPath = startPath || "";
  movePickerBlocked = blocked;
  return new Promise((resolve) => {
    movePickerResolve = resolve;
    $("move-error").textContent = "";
    $("move-modal").classList.remove("hidden");
    renderMovePicker();
  });
}

function closeMovePicker(result) {
  $("move-modal").classList.add("hidden");
  if (movePickerResolve) {
    const done = movePickerResolve;
    movePickerResolve = null;
    done(result);
  }
}

function isBlockedDest(path) {
  return movePickerBlocked.some((b) => path === b || path.startsWith(b + "/"));
}

async function renderMovePicker() {
  // 브레드크럼: 저장소 최상위 → 현재 경로의 각 단계
  const crumb = $("move-breadcrumb");
  crumb.innerHTML = "";
  const sp = spacesById[currentSpace];
  const rootLink = document.createElement("a");
  rootLink.textContent = currentSpace === "home" ? "🏠 내 파일" : `💾 ${sp ? sp.name : currentSpace}`;
  rootLink.addEventListener("click", () => { movePickerPath = ""; renderMovePicker(); });
  crumb.appendChild(rootLink);

  const parts = movePickerPath ? movePickerPath.split("/") : [];
  let acc = "";
  parts.forEach((part, i) => {
    acc = acc ? `${acc}/${part}` : part;
    const sep = document.createElement("span");
    sep.className = "sep";
    sep.textContent = "›";
    crumb.appendChild(sep);
    if (i === parts.length - 1) {
      const cur = document.createElement("span");
      cur.className = "current";
      cur.textContent = part;
      crumb.appendChild(cur);
    } else {
      const link = document.createElement("a");
      link.textContent = part;
      const target = acc;
      link.addEventListener("click", () => { movePickerPath = target; renderMovePicker(); });
      crumb.appendChild(link);
    }
  });

  // 목적지가 선택 폴더 자신/하위면 이동 불가 → 확인 버튼 잠금
  $("move-confirm").disabled = isBlockedDest(movePickerPath);

  const list = $("move-list");
  list.innerHTML = '<div class="empty">불러오는 중…</div>';
  $("move-error").textContent = "";
  try {
    const data = await api(fileUrl("list", movePickerPath));
    const folders = data.entries.filter((e) => e.is_dir);
    list.innerHTML = "";
    if (!folders.length) {
      list.innerHTML = '<div class="empty">하위 폴더가 없습니다</div>';
      return;
    }
    for (const f of folders) {
      const btn = document.createElement("button");
      btn.className = "mf";
      const cannot = isBlockedDest(f.path);
      btn.textContent = "📁 " + f.name + (cannot ? "  (이동 대상)" : "");
      btn.disabled = cannot;
      if (!cannot) {
        btn.addEventListener("click", () => { movePickerPath = f.path; renderMovePicker(); });
      }
      list.appendChild(btn);
    }
  } catch (err) {
    list.innerHTML = "";
    $("move-error").textContent = err.message;
  }
}

$("move-confirm").addEventListener("click", () => {
  if (isBlockedDest(movePickerPath)) return;
  closeMovePicker(movePickerPath);
});
$("move-cancel").addEventListener("click", () => closeMovePicker(null));
$("move-backdrop").addEventListener("click", () => closeMovePicker(null));

function bulkDownload() {
  const files = [...selection]
    .map((p) => currentEntries.find((e) => e.path === p))
    .filter((e) => e && !e.is_dir);
  if (!files.length) { alert("다운로드할 파일이 없습니다. (폴더는 일괄 다운로드에서 제외됩니다)"); return; }
  files.forEach((e, i) => {
    setTimeout(() => {
      const a = document.createElement("a");
      a.href = fileUrl("download", e.path);
      a.download = e.name;
      document.body.appendChild(a);
      a.click();
      a.remove();
    }, i * 350);   // 브라우저가 연속 다운로드를 막지 않도록 약간 간격을 둔다
  });
}

$("sel-all").addEventListener("click", toggleSelectAll);
$("sel-clear").addEventListener("click", clearSelection);
$("sel-download").addEventListener("click", bulkDownload);
$("sel-share").addEventListener("click", shareSelected);
$("sel-move").addEventListener("click", bulkMove);
$("sel-delete").addEventListener("click", bulkDelete);

function shareSelected() {
  if (selection.size < 1) return;
  const paths = [...selection];
  const name = paths.length === 1
    ? (currentEntries.find((e) => e.path === paths[0])?.name || paths[0].split("/").pop())
    : `${paths.length}개 항목`;
  openShareModal(currentSpace, paths, name);
}

/* ---------- 외부 공유 링크 ---------- */
let shareTarget = null;   // {space, paths, name}

function shareUrl(token) {
  return `${location.origin}/s/${token}`;
}

function openShareModal(space, paths, name) {
  shareTarget = { space, paths, name };
  $("share-target-name").textContent = name;
  $("share-password").value = "";
  $("share-expiry").value = "7";
  $("share-create-error").textContent = "";
  $("share-form").classList.remove("hidden");
  $("share-result").classList.add("hidden");
  $("share-modal").classList.remove("hidden");
}

$("share-create-btn").addEventListener("click", async () => {
  if (!shareTarget) return;
  $("share-create-error").textContent = "";
  const password = $("share-password").value;
  const expVal = $("share-expiry").value;
  const body = { space: shareTarget.space, paths: shareTarget.paths };
  if (password) body.password = password;
  if (expVal) body.expires_days = parseInt(expVal, 10);
  try {
    const data = await postJSON("/api/shares/create", body);
    const link = shareUrl(data.token);
    $("share-link").value = link;
    const bits = [data.collection ? `${data.count}개 항목` : (data.is_dir ? "폴더" : "파일")];
    if (data.protected) bits.push("🔒 비밀번호 보호");
    bits.push(data.expires_at ? `${new Date(data.expires_at).toLocaleDateString("ko-KR")} 만료` : "무기한");
    $("share-result-meta").textContent = bits.join(" · ");
    $("share-form").classList.add("hidden");
    $("share-result").classList.remove("hidden");
  } catch (err) {
    $("share-create-error").textContent = err.message;
  }
});

$("share-copy").addEventListener("click", () => copyText($("share-link").value, $("share-copy")));

async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // clipboard API 불가 시 폴백
    const inp = $("share-link");
    inp.focus(); inp.select();
    try { document.execCommand("copy"); } catch {}
  }
  if (btn) {
    const old = btn.textContent;
    btn.textContent = "복사됨 ✓";
    setTimeout(() => { btn.textContent = old; }, 1500);
  }
}

/* ---------- 내 공유 관리 ---------- */
$("shares-btn").addEventListener("click", openSharesModal);

async function openSharesModal() {
  $("shares-modal").classList.remove("hidden");
  await loadSharesList();
}

async function loadSharesList() {
  const list = $("shares-list");
  list.innerHTML = '<p class="share-state">불러오는 중…</p>';
  let data;
  try {
    data = await api("/api/shares/list");
  } catch (err) {
    list.innerHTML = `<p class="auth-error">${err.message}</p>`;
    return;
  }
  list.innerHTML = "";
  $("shares-empty").classList.toggle("hidden", data.shares.length > 0);
  for (const s of data.shares) {
    list.appendChild(shareRow(s));
  }
}

function shareRow(s) {
  const row = document.createElement("div");
  row.className = "share-row";

  const info = document.createElement("div");
  info.className = "share-row-info";
  const title = document.createElement("div");
  title.className = "share-row-name";
  title.textContent = `${s.is_dir ? "📁" : "📄"} ${s.name}`;
  info.appendChild(title);

  const badges = document.createElement("div");
  badges.className = "share-row-badges";
  const spaceLabel = s.space === "home" ? "내 파일" : s.space;
  badges.appendChild(badge(spaceLabel, "space"));
  if (s.protected) badges.appendChild(badge("🔒 비밀번호", "pw"));
  if (s.expired) badges.appendChild(badge("만료됨", "expired"));
  else if (s.expires_at) badges.appendChild(badge(`${new Date(s.expires_at).toLocaleDateString("ko-KR")} 만료`, "exp"));
  else badges.appendChild(badge("무기한", "exp"));
  info.appendChild(badges);
  row.appendChild(info);

  const actions = document.createElement("div");
  actions.className = "share-row-actions";
  const copy = document.createElement("button");
  copy.className = "btn subtle";
  copy.textContent = "🔗 복사";
  copy.title = shareUrl(s.token);
  copy.addEventListener("click", () => copyText(shareUrl(s.token), copy));
  actions.appendChild(copy);
  const open = document.createElement("a");
  open.className = "btn subtle";
  open.textContent = "↗ 열기";
  open.href = shareUrl(s.token);
  open.target = "_blank";
  open.rel = "noopener";
  actions.appendChild(open);
  const rev = document.createElement("button");
  rev.className = "btn subtle del";
  rev.textContent = "🗑 해제";
  rev.addEventListener("click", () => revokeShare(s.token, s.name));
  actions.appendChild(rev);
  row.appendChild(actions);
  return row;
}

function badge(text, kind) {
  const b = document.createElement("span");
  b.className = `share-badge share-badge-${kind}`;
  b.textContent = text;
  return b;
}

async function revokeShare(token, name) {
  if (!confirm(`'${name}' 공유를 해제할까요? 이 링크는 더 이상 열리지 않습니다.`)) return;
  try {
    await postJSON("/api/shares/revoke", { token });
  } catch (err) {
    alert(err.message);
    return;
  }
  await loadSharesList();
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
    loadUsage();
  } catch (err) { alert(err.message); }
}

/* ---------- 업로드 ---------- */
$("upload-btn").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", (e) => {
  uploadFiles(e.target.files);
  e.target.value = "";
});
// ── 다중 폴더 업로드 ──
// webkitdirectory 선택기는 한 번에 폴더 하나만 고른다. 여러 폴더를 올리려면
// 고른 폴더들을 대기열(tray)에 쌓아 두었다가 한꺼번에 업로드한다.
// (드래그 앤 드롭은 한 번에 여러 폴더를 놓을 수 있어 아래 drop 핸들러가 이미 처리한다.)
const uploadQueue = new Map();   // path -> {file, path}
function queueFolder(files) {
  if (isReadonly()) { alert("읽기 전용 저장소에는 업로드할 수 없습니다"); return; }
  for (const f of files) {
    const path = f.webkitRelativePath || f.name;
    if (!uploadQueue.has(path)) uploadQueue.set(path, { file: f, path });
  }
  renderUploadTray();
}
function renderUploadTray() {
  const tray = $("upload-tray");
  if (!uploadQueue.size) { tray.classList.add("hidden"); return; }
  const entries = [...uploadQueue.values()];
  const folders = [...new Set(entries.map((e) => e.path.split("/")[0]))];
  const shown = folders.slice(0, 6).join(", ") + (folders.length > 6 ? " …" : "");
  $("tray-summary").textContent =
    `대기 중 · 폴더 ${folders.length}개 · 파일 ${entries.length}개  (${shown})`;
  tray.classList.remove("hidden");
}

$("upload-folder-btn").addEventListener("click", () => $("folder-input").click());
$("tray-add").addEventListener("click", () => $("folder-input").click());
$("folder-input").addEventListener("change", (e) => {
  queueFolder(e.target.files);   // 각 File 의 webkitRelativePath 로 하위 구조 유지
  e.target.value = "";           // 같은 폴더를 다시 고를 수 있도록 초기화
});
$("tray-upload").addEventListener("click", () => {
  const list = [...uploadQueue.values()];
  uploadQueue.clear();
  renderUploadTray();
  uploadFiles(list);
});
$("tray-clear").addEventListener("click", () => { uploadQueue.clear(); renderUploadTray(); });

// items: FileList/File[] (각 File 이 webkitRelativePath 를 가질 수 있음) 또는 {file,path}[]
async function uploadFiles(items) {
  const entries = [];
  for (const it of items) {
    if (it instanceof File) entries.push({ file: it, path: it.webkitRelativePath || it.name });
    else if (it && it.file) entries.push(it);
  }
  if (!entries.length) return;
  if (isReadonly()) {
    alert("읽기 전용 저장소에는 업로드할 수 없습니다");
    return;
  }
  const form = new FormData();
  for (const { file, path } of entries) {
    form.append("files", file, file.name);
    form.append("paths", path);          // 상대경로(하위폴더 포함) — 서버가 구조 보존
  }
  const status = $("upload-status");
  const folders = new Set(
    entries.map((e) => e.path.split("/").slice(0, -1).join("/")).filter(Boolean));
  status.textContent = folders.size
    ? `⬆ ${entries.length}개 파일 (폴더 ${folders.size}개) 업로드 중...`
    : `⬆ ${entries.length}개 파일 업로드 중...`;
  status.classList.remove("hidden");
  try {
    await api(fileUrl("upload", currentPath), { method: "POST", body: form });
    loadDir(currentPath);
    loadUsage();
  } catch (err) {
    alert(err.message);
  } finally {
    status.classList.add("hidden");
  }
}

/* 드롭된 파일/폴더 엔트리를 재귀적으로 읽어 {file, path} 목록으로 만든다 */
async function walkEntries(roots) {
  const out = [];
  for (const entry of roots) await walkEntry(entry, "", out);
  return out;
}
function walkEntry(entry, prefix, out) {
  return new Promise((resolve) => {
    if (entry.isFile) {
      entry.file(
        (f) => { out.push({ file: f, path: prefix + entry.name }); resolve(); },
        () => resolve());
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      const readBatch = () => reader.readEntries(async (ents) => {
        if (!ents.length) { resolve(); return; }   // 더 없으면 종료
        for (const e of ents) await walkEntry(e, prefix + entry.name + "/", out);
        readBatch();                                // readEntries 는 배치로 반환 → 반복
      }, () => resolve());
      readBatch();
    } else {
      resolve();
    }
  });
}

/* 드래그 앤 드롭 (폴더 포함) */
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
  if ($("app-screen").classList.contains("hidden")) return;
  // webkitGetAsEntry 는 이벤트 핸들러 안에서 동기적으로 호출해야 유효하다.
  const items = e.dataTransfer.items;
  const roots = [];
  if (items && items.length) {
    for (let i = 0; i < items.length; i++) {
      const entry = items[i].webkitGetAsEntry && items[i].webkitGetAsEntry();
      if (entry) roots.push(entry);
    }
  }
  if (roots.length) {
    walkEntries(roots).then((list) => uploadFiles(list));   // 폴더 구조 유지
  } else {
    uploadFiles(e.dataTransfer.files);                      // 폴백: 평면 파일
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
  // 이동 모달은 대기 중인 프로미스가 있으므로 취소로 처리해 확실히 resolve한다.
  if (!$("move-modal").classList.contains("hidden")) {
    closeMovePicker(null);
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

/* ---------- QR 로그인 ---------- */
let qrHandle = null;
let qrTimer = null;
let qrExpireAt = 0;
let qrGen = 0; // 발급 세대 — 겹치는 발급/오래된 폴링 응답을 무시하는 데 사용

$("qr-btn").addEventListener("click", () => {
  $("qr-modal").classList.remove("hidden");
  issueQr();
});
$("qr-refresh").addEventListener("click", issueQr);

async function issueQr() {
  const gen = ++qrGen; // 이전 발급/폴링을 모두 무효화
  clearInterval(qrTimer);
  qrTimer = null;
  const status = $("qr-status");
  status.className = "qr-status";
  status.textContent = "코드 발급 중...";
  $("qr-img").src = "";
  try {
    const res = await postJSON("/api/auth/qr/create", { server: location.origin });
    if (gen !== qrGen) return; // 그 사이 새 발급이 시작됨
    qrHandle = res.handle;
    qrExpireAt = Date.now() + res.expires_in * 1000;
    $("qr-img").src = `/api/auth/qr/image?handle=${encodeURIComponent(qrHandle)}`;
    $("qr-img").style.opacity = 1;
    updateQrCountdown();
    qrTimer = setInterval(() => pollQr(gen), 2000);
  } catch (err) {
    if (gen !== qrGen) return;
    status.textContent = err.message;
  }
}

function updateQrCountdown() {
  const remain = Math.max(0, Math.round((qrExpireAt - Date.now()) / 1000));
  $("qr-status").textContent =
    `앱에서 스캔 대기 중... (${Math.floor(remain / 60)}:${String(remain % 60).padStart(2, "0")} 남음)`;
}

async function pollQr(gen) {
  if ($("qr-modal").classList.contains("hidden")) {
    clearInterval(qrTimer); // 모달이 닫히면 폴링 중단
    qrTimer = null;
    return;
  }
  let st;
  try {
    st = await api(`/api/auth/qr/status?handle=${encodeURIComponent(qrHandle)}`);
  } catch {
    return; // 일시적 네트워크 오류는 다음 폴링에서 재시도
  }
  if (gen !== qrGen) return; // 오래된 발급의 응답 — 현재 화면에 반영하지 않음
  const status = $("qr-status");
  if (st.status === "used") {
    clearInterval(qrTimer);
    qrTimer = null;
    status.className = "qr-status ok";
    status.textContent = "✅ 기기가 연결되었습니다!";
  } else if (st.status === "expired") {
    clearInterval(qrTimer);
    qrTimer = null;
    status.textContent = "코드가 만료되었습니다. 새 코드를 발급하세요.";
    $("qr-img").style.opacity = 0.15;
  } else {
    updateQrCountdown();
  }
}

/* ---------- 사용자 관리 (관리자) ---------- */
$("admin-btn").addEventListener("click", () => {
  $("admin-error").textContent = "";
  $("admin-modal").classList.remove("hidden");
  loadUsers();
  loadServerinfoToken();
});

/* ---------- Homepage 위젯 토큰 (관리자) ---------- */
async function loadServerinfoToken() {
  try {
    const data = await api("/api/admin/serverinfo");
    $("serverinfo-token").value = data.token || "";
  } catch (err) {
    /* 토큰 조회 실패는 사용자 목록 오류와 섞지 않는다 */
  }
}

$("serverinfo-gen").addEventListener("click", async () => {
  try {
    const data = await postJSON("/api/admin/serverinfo/generate", {});
    $("serverinfo-token").value = data.token;
  } catch (err) {
    $("admin-error").textContent = err.message;
  }
});

$("serverinfo-clear").addEventListener("click", async () => {
  if (!$("serverinfo-token").value) return;
  if (!confirm("위젯 토큰을 삭제할까요? 이 토큰으로는 더 이상 접근할 수 없습니다.")) return;
  try {
    await postJSON("/api/admin/serverinfo/clear", {});
    $("serverinfo-token").value = "";
  } catch (err) {
    $("admin-error").textContent = err.message;
  }
});

$("serverinfo-copy").addEventListener("click", () => {
  const v = $("serverinfo-token").value;
  if (v) copyText(v, $("serverinfo-copy"));
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
    const used = u.usage_bytes ? formatSize(u.usage_bytes) : "0 B";
    tdUsage.textContent = u.quota_bytes ? `${used} / ${formatSize(u.quota_bytes)}` : `${used} / 무제한`;
    tr.appendChild(tdUsage);

    const tdActions = document.createElement("td");
    const wrap = document.createElement("div");
    wrap.className = "user-actions";
    // 용량 제한은 관리자 자신 포함 누구에게나 설정 가능
    const quota = document.createElement("button");
    quota.textContent = "용량 제한";
    quota.onclick = () => adminSetQuota(u);
    wrap.appendChild(quota);

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

async function adminSetQuota(u) {
  const currentGB = u.quota_bytes ? (u.quota_bytes / 1024 ** 3).toFixed(2).replace(/\.?0+$/, "") : "0";
  const input = prompt(
    `"${u.username}"의 용량 제한 (GB, 0 = 무제한):\n※ 개인 저장소에만 적용됩니다`,
    currentGB
  );
  if (input === null) return;
  const gb = parseFloat(input);
  if (isNaN(gb) || gb < 0) {
    alert("0 이상의 숫자를 입력하세요");
    return;
  }
  try {
    await postJSON("/api/admin/users/quota", {
      user_id: u.id,
      quota_bytes: Math.round(gb * 1024 ** 3),
    });
    loadUsers();
  } catch (err) {
    alert(err.message);
  }
}

/* ---------- 외부 저장소 관리 (관리자) ---------- */
$("storage-btn").addEventListener("click", () => {
  $("storage-error").textContent = "";
  $("storage-modal").classList.remove("hidden");
  loadMounts();
});

async function loadMounts() {
  try {
    const data = await api("/api/admin/mounts");
    $("storage-error").textContent = "";
    renderMounts(data.mounts, data.users);
  } catch (err) {
    $("storage-error").textContent = err.message;
  }
}

function renderMounts(mounts, users) {
  const list = $("storage-list");
  list.innerHTML = "";
  if (!mounts.length) {
    list.innerHTML =
      '<p class="qr-desc">마운트된 외부 저장소가 없습니다. compose.yaml에서 <code>/app/mounts/이름</code>으로 볼륨을 추가하세요.</p>';
    return;
  }
  for (const m of mounts) {
    const card = document.createElement("div");
    card.className = "storage-item";

    const head = document.createElement("div");
    head.className = "storage-head";
    head.textContent = "💾 " + m.name;
    if (m.readonly) {
      const ro = document.createElement("span");
      ro.className = "badge plain";
      ro.textContent = "🔒 읽기 전용";
      head.appendChild(ro);
    }
    card.appendChild(head);

    const usersBox = document.createElement("div");
    usersBox.className = "storage-users";
    if (!users.length) {
      usersBox.innerHTML = '<span class="me-note">일반 사용자가 없습니다 (관리자는 항상 접근 가능)</span>';
    } else {
      const granted = new Set(m.user_ids || []);
      for (const u of users) {
        const label = document.createElement("label");
        label.className = "check-label";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.value = u.id;
        cb.checked = granted.has(u.id);
        cb.onchange = () => saveMountAccess(m.name, usersBox);
        label.appendChild(cb);
        label.appendChild(document.createTextNode(" " + u.username));
        usersBox.appendChild(label);
      }
    }
    card.appendChild(usersBox);
    list.appendChild(card);
  }
}

async function saveMountAccess(mountName, usersBox) {
  const user_ids = [...usersBox.querySelectorAll("input:checked")].map((c) => Number(c.value));
  try {
    await postJSON("/api/admin/mounts/grant", { mount_name: mountName, user_ids });
    $("storage-error").textContent = "";
  } catch (err) {
    $("storage-error").textContent = err.message;
    loadMounts(); // 실패 시 서버 상태로 되돌림
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
