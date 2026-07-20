"""genDISK Drive — Windows Cloud Files(cldapi) 온디맨드 가상 파일시스템 프로바이더.

%USERPROFILE%\\genDISK 를 싱크루트로 등록하고, 서버 파일을 "플레이스홀더"(디스크상
0바이트, 크기만 표시)로 심는다. 탐색기에서 파일을 열면 Windows 가 FETCH_DATA 콜백을
호출 → 서버에서 실제 바이트를 받아 CfExecute(TRANSFER_DATA) 로 채운다(하이드레이션).

콜백 객체(CF_CALLBACK)는 반드시 살아 있어야 한다(GC 되면 콜백 스레드가 죽는다) →
Provider 인스턴스 속성으로 붙잡아 둔다.
"""
import ctypes
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from ctypes import POINTER, byref

from . import cfapi as C

SECTOR = 4096
# 하이드레이션 전송 조각 크기. 클수록 왕복(라운드트립)이 줄어 대용량 다운로드가 빨라진다.
# 8 MiB = 4KB 배수. 예전 1 MiB 는 2GB 파일에 ~2300 왕복이 필요해 앱이 취소되곤 했다.
CHUNK = 8 << 20  # 8 MiB
# 하이드레이션 시 동시에 받는 조각(=서버 연결) 수. 대역폭을 합산해 대용량 다운로드를
# 빠르게 한다(특히 Cloudflare 처럼 연결당 속도가 제한될 때 효과적). 메모리는 최대
# HYDRATE_WORKERS × CHUNK 로 제한된다. 서버·앞단 부하를 보며 조절.
HYDRATE_WORKERS = 4


def _now_filetime() -> int:
    ft = C.LARGE_INTEGER(0)
    ctypes.windll.kernel32.GetSystemTimeAsFileTime(byref(ft))
    return ft.value


def set_expose_placeholders():
    """이 프로세스가 플레이스홀더를 '플레이스홀더로' 보게 한다(프로바이더 필수)."""
    C.RtlSetProcessPlaceholderCompatibilityMode(bytes([C.PHCM_EXPOSE_PLACEHOLDERS]))


class Provider:
    def __init__(self, root: str, provider_guid: str, fetch_range, list_dir=None,
                 list_spaces=None, upload=None, delete=None, rename=None, mkdir=None,
                 notify=None, progress=None, state_path=None, always_fresh=True,
                 space: str = "home", provider_name: str = "genDISK",
                 identity: bytes = b"genDISK", log=print):
        self.root = os.path.abspath(root)
        self.provider_guid = provider_guid
        self.provider_name = provider_name
        self.identity = identity
        self.fetch_range = fetch_range      # (meta:dict, offset:int, length:int) -> bytes
        self.list_dir = list_dir            # (space:str, rel_posix:str) -> [entry dict]
        self.list_spaces = list_spaces      # () -> [{id,name,readonly}] (다중 저장소 모드)
        self.upload = upload                # (space:str, path:str, local_path:str) -> None (로컬→원격)
        self.delete = delete                # (space:str, path:str) -> None (로컬 삭제 → 서버 삭제)
        self.rename = rename                # (space, src, dst, src_space?, dst_space?) -> None
        self.mkdir = mkdir                  # (space:str, path:str) -> None (새 폴더 생성)
        self.notify = notify                # (msg:str) -> None (토스트 알림, 업로드 진행 표시용)
        self.progress = progress            # (key,name,dir,done,total) -> None (다운로드 진행)
        self.space = space
        self.log = log
        # SMB식 동작: 폴더를 '채움 완료(고정)'로 표시하지 않아 열 때마다 서버 목록을
        # 다시 조회한다 → 탐색기가 항상 서버의 현재 상태를 보여준다(네트워크 드라이브처럼).
        self.always_fresh = bool(always_fresh)
        self._space_map = {}                # 폴더이름 -> space id (다중 저장소)
        # 원격 변경 반영(refresh)용: 지금까지 실제로 열려 채워진 폴더들의 rel 경로.
        # refresh 는 이 폴더들만 서버와 대조해 새 항목을 추가한다(온디맨드 유지 + 작업량 최소).
        # Windows 는 한 번 채워진(고정된) 폴더에 FETCH_PLACEHOLDERS 를 다시 보내지 않으므로,
        # 이 집합을 디스크(state_path)에 영속화해 앱 재시작 후에도 깊은 폴더가
        # 폴링(갱신·삭제 반영)과 upload_scan(드롭 감지) 대상에 남게 한다.
        self._populated_dirs = set()
        self._state_path = state_path
        self._state_lock = threading.Lock()
        self._load_state()
        self._ph_lock = threading.Lock()    # CfCreatePlaceholders 동시호출 직렬화
        self._reconcile_lock = threading.Lock()  # 폴더 대조(refresh/refresh_dir) 직렬화 — 폴링·SSE 경쟁 방지
        self._refresh_err = False           # 직전 _refresh_one 의 목록 조회 실패 여부(연속 실패 중단용)
        self._hydrate_pool = None           # 병렬 다운로드 워커 풀(지연 생성)
        self._upload_seen = {}              # frel -> (size, mtime_ns): 안정성 대기 추적
        self._upload_done = {}              # frel -> (size, mtime_ns): 이미 업로드한 버전(재업로드 방지)
        self._suppress_delete = set()       # refresh 가 로컬에서 지운 것 — 서버 삭제로 전파 금지
        # 볼륨 상대 루트 경로(콜백의 NormalizedPath 는 드라이브 문자 없는 볼륨 상대 경로)
        self._root_volrel = os.path.splitdrive(self.root)[1]
        self.conn_key = None
        self._connected = False
        # GC 방지용 참조 보관
        self._cb_fetch_data = None
        self._cb_fetch_ph = None
        self._cb_delete = None
        self._cb_rename = None
        self._cb_table = None
        self._reg_idbuf = None

    # ------------------------------------------------------- populated 상태 영속화
    MAX_TRACKED_DIRS = 500      # 폴링 대상 상한 — 폴더당 목록 요청 1회이므로 무한 성장 방지

    def _load_state(self):
        """저장된 '채워진 폴더' 목록을 복원한다(로컬에 아직 존재하는 폴더만). best-effort.
        상태 파일이 아직 없으면(업데이트 직후 첫 실행) 로컬에 이미 만들어진 폴더들을
        걸어 한 번 시딩한다 — 이 업데이트 이전 세션에서 열려 '고정'된 폴더는
        FETCH_PLACEHOLDERS 가 다시 안 오므로, 이 마이그레이션이 없으면 영영 잊힌다.
        (provider 연결 전의 로컬 걷기라 네트워크·온디맨드 채우기를 유발하지 않는다.)"""
        if not self._state_path:
            return
        try:
            with open(self._state_path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:  # noqa: BLE001  (없거나 손상 — 로컬 걷기로 시딩)
            self._seed_from_disk()
            return
        try:
            if data.get("root") != self.root:
                self._seed_from_disk()       # 다른 싱크루트의 상태 — 이 루트 기준으로 다시 시딩
                return
            for rel in data.get("populated_dirs", [])[:self.MAX_TRACKED_DIRS]:
                local = self.root if rel == "" else os.path.join(
                    self.root, rel.replace("/", os.sep))
                if os.path.isdir(local):
                    self._populated_dirs.add(rel)
        except Exception:  # noqa: BLE001
            pass

    def _seed_from_disk(self):
        """로컬에 존재하는 폴더 트리를 걸어 populated 집합을 시딩한다(마이그레이션·복구용)."""
        try:
            if not os.path.isdir(self.root):
                return
            self._populated_dirs.add("")
            for dirpath, dirnames, _files in os.walk(self.root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for d in dirnames:
                    rel = os.path.relpath(os.path.join(dirpath, d),
                                          self.root).replace(os.sep, "/")
                    self._populated_dirs.add(rel)
                    if len(self._populated_dirs) >= self.MAX_TRACKED_DIRS:
                        break
                if len(self._populated_dirs) >= self.MAX_TRACKED_DIRS:
                    break
            self._save_state()
        except Exception:  # noqa: BLE001
            pass

    def _save_state(self):
        """'채워진 폴더' 목록을 원자적으로 저장한다. best-effort(실패해도 동작에는 지장 없음)."""
        if not self._state_path:
            return
        try:
            with self._state_lock:           # 스냅샷도 락 안에서 — 동시 저장 간 최신본 보장
                dirs = sorted(list(self._populated_dirs))[:self.MAX_TRACKED_DIRS]
                os.makedirs(os.path.dirname(self._state_path), exist_ok=True)
                tmp = self._state_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"root": self.root, "populated_dirs": dirs},
                              f, ensure_ascii=False)
                os.replace(tmp, self._state_path)
        except Exception:  # noqa: BLE001
            pass

    def _track_populated(self, rel: str):
        """폴더가 채워졌음을 기록(+영속화). 이미 알던 폴더면 저장 생략."""
        if rel not in self._populated_dirs:
            self._populated_dirs.add(rel)
            self._save_state()

    # ------------------------------------------------------------------ 등록
    def register(self):
        reg = C.CF_SYNC_REGISTRATION()
        reg.StructSize = ctypes.sizeof(C.CF_SYNC_REGISTRATION)
        reg.ProviderName = self.provider_name
        reg.ProviderVersion = "1.0"
        idbuf = ctypes.create_string_buffer(self.identity, len(self.identity))
        self._reg_idbuf = idbuf  # 호출 동안 살아 있어야 함
        reg.SyncRootIdentity = ctypes.cast(idbuf, C.LPCVOID)
        reg.SyncRootIdentityLength = len(self.identity)
        reg.FileIdentity = None
        reg.FileIdentityLength = 0
        reg.ProviderId = C.GUID(self.provider_guid)

        pol = C.CF_SYNC_POLICIES()
        pol.StructSize = ctypes.sizeof(C.CF_SYNC_POLICIES)
        pol.Hydration.Primary = C.CF_HYDRATION_POLICY_PROGRESSIVE
        pol.Hydration.Modifier = C.CF_HYDRATION_POLICY_MODIFIER_NONE
        pol.Population.Primary = C.CF_POPULATION_POLICY_FULL
        pol.Population.Modifier = C.CF_POPULATION_POLICY_MODIFIER_NONE
        pol.InSync = C.CF_INSYNC_POLICY_NONE
        pol.HardLink = C.CF_HARDLINK_POLICY_NONE
        pol.PlaceholderManagement = C.CF_PLACEHOLDER_MANAGEMENT_POLICY_DEFAULT

        hr = C.CfRegisterSyncRoot(self.root, byref(reg), byref(pol),
                                  C.CF_REGISTER_FLAG_UPDATE)
        if not C.hr_ok(hr):
            raise OSError(f"CfRegisterSyncRoot 실패 {C.hr_str(hr)}")
        self.log(f"[vfs] registered sync root: {self.root}")

    def unregister(self):
        hr = C.CfUnregisterSyncRoot(self.root)
        self.log(f"[vfs] unregister -> {C.hr_str(hr)}")

    # ------------------------------------------------------------------ 연결
    def connect(self):
        regs = (C.CF_CALLBACK_REGISTRATION * 5)()
        self._cb_fetch_data = C.CF_CALLBACK(self._on_fetch_data)
        self._cb_fetch_ph = C.CF_CALLBACK(self._on_fetch_placeholders)
        self._cb_delete = C.CF_CALLBACK(self._on_delete)
        self._cb_rename = C.CF_CALLBACK(self._on_rename)
        regs[0].Type = C.CF_CALLBACK_TYPE_FETCH_DATA
        regs[0].Callback = self._cb_fetch_data
        regs[1].Type = C.CF_CALLBACK_TYPE_FETCH_PLACEHOLDERS
        regs[1].Callback = self._cb_fetch_ph
        # 로컬 삭제 완료 후 알림 → 서버에서도 삭제 전파(사후 알림이라 삭제를 막지 않음).
        regs[2].Type = C.CF_CALLBACK_TYPE_DELETE_COMPLETION
        regs[2].Callback = self._cb_delete
        # 로컬 이름변경/이동 완료 후 알림 → 서버에서도 이동 전파.
        regs[3].Type = C.CF_CALLBACK_TYPE_RENAME_COMPLETION
        regs[3].Callback = self._cb_rename
        regs[4].Type = C.CF_CALLBACK_TYPE_NONE            # 종료 표식
        regs[4].Callback = C.CF_CALLBACK()               # NULL
        self._cb_table = regs

        conn = C.CF_CONNECTION_KEY()
        hr = C.CfConnectSyncRoot(
            self.root, regs, None,
            C.CF_CONNECT_FLAG_REQUIRE_FULL_FILE_PATH,
            byref(conn))
        if not C.hr_ok(hr):
            raise OSError(f"CfConnectSyncRoot 실패 {C.hr_str(hr)}")
        self.conn_key = conn
        self._connected = True
        self.log(f"[vfs] connected (key={conn.Internal:#x})")
        # 루트는 실제 폴더라 자동 FETCH_PLACEHOLDERS 가 안 온다 → 최상위를 즉시 채운다.
        # 하위 폴더는 플레이스홀더 디렉터리라 열릴 때 FETCH_PLACEHOLDERS 로 채워진다.
        if self.list_dir is not None:
            try:
                self.populate_root()
            except Exception as e:  # noqa: BLE001
                self.log(f"[vfs] populate_root error: {e!r}")

    def _space_entries(self):
        """다중 저장소 모드: 접근 가능한 저장소들을 최상위 폴더 항목으로. 매핑도 갱신."""
        spaces = self.list_spaces() or []
        self._space_map = {}
        out = []
        for s in spaces:
            name = (s.get("name") or s["id"]).strip() or s["id"]
            # 폴더명에 못 쓰는 문자 정리
            for ch in '\\/:*?"<>|':
                name = name.replace(ch, "_")
            if name in self._space_map:            # 이름 충돌 방지
                name = f"{name} ({s['id']})"
            self._space_map[name] = s["id"]
            out.append({"name": name, "path": "", "is_dir": True, "_space": s["id"]})
        return out

    def _children_for(self, rel: str):
        """드라이브 상대경로 rel(posix)의 자식 항목 목록(각 항목에 _space 주입)."""
        if self.list_spaces is not None:
            if rel == "":
                return self._space_entries()
            if not self._space_map:
                self._space_entries()              # 재시작 후 매핑 복구
            parts = rel.split("/")
            sid = self._space_map.get(parts[0])
            if sid is None:
                return []
            subpath = "/".join(parts[1:])
            raw = self.list_dir(sid, subpath) if self.list_dir else []
            return [dict(e, _space=sid) for e in raw]
        # 단일 저장소 모드
        raw = self.list_dir(self.space, rel) if self.list_dir else []
        return [dict(e, _space=self.space) for e in raw]

    def _create_placeholders_in(self, dir_fullpath: str, entries) -> int:
        """로컬 폴더(dir_fullpath)에 entries 를 플레이스홀더로 생성한다. 생성 개수 반환.
        CfCreatePlaceholders 동시호출은 락으로 직렬화(백그라운드 refresh vs 콜백 경쟁 방지)."""
        if not entries:
            return 0
        arr, keep = self._build_placeholders(entries)  # noqa: F841 (호출 동안 살려둔다)
        processed = C.DWORD(0)
        with self._ph_lock:
            hr = C.CfCreatePlaceholders(dir_fullpath, arr, len(entries),
                                        C.CF_CREATE_FLAG_NONE, byref(processed))
        if not C.hr_ok(hr):
            raise OSError(f"CfCreatePlaceholders({dir_fullpath}) {C.hr_str(hr)}")
        return processed.value

    def populate_root(self):
        """드라이브 루트를 플레이스홀더로 심는다(다중 저장소면 저장소 폴더들, 아니면 최상위 파일)."""
        entries = self._children_for("")
        self._track_populated("")
        existing = set(os.listdir(self.root)) if os.path.isdir(self.root) else set()
        fresh = [e for e in entries if e["name"] not in existing]
        if not fresh:
            self.log(f"[vfs] populate_root: nothing new ({len(entries)} entries)")
            return
        n = self._create_placeholders_in(self.root, fresh)
        self.log(f"[vfs] populate_root: seeded {n}/{len(fresh)}")

    def refresh(self, deep: bool = True) -> int:
        """서버와 다시 대조해 다른 기기(폰/웹)가 올린 새 파일을 placeholder 로 추가한다
        → 드라이브에 나타난다. 이미 열려서 DISABLE_ON_DEMAND_POPULATION 로 '고정'된 폴더도
        CfCreatePlaceholders 로 새 항목을 넣을 수 있으므로, 고정된 폴더의 갱신도 이걸로 해결한다.

        대상: (1) 루트, (2) 최상위 저장소 폴더(내 파일=home 등) — 비어 있어도 항상,
        (3) deep=True 면 열었던 폴더들(_populated_dirs, 영속화됨). (2)가 핵심: 홈이 한 번
        열려 고정된 뒤 새 파일이 안 뜨던 문제를 잡는다.

        주기 폴링용(폴백). 실시간 반영은 refresh_dir()(SSE 이벤트 기반)가 담당한다.
        deep=False 면 루트+저장소 폴더만(가벼운 주기용), deep=True 면 열었던 폴더 전부
        (폴더당 목록 요청 1회라 무겁다 — 주기적으로 가끔 + 수동 새로고침에서만).
        연속 3회 목록 조회가 실패하면(서버 접속 불가) 남은 폴더를 포기해 오래 매달리지 않는다.
        안전 원칙: '추가'와 '우리 in-sync 항목의 서버측 삭제 반영'만 한다(사용자 로컬 변경 불가침)."""
        if self.list_dir is None:
            return 0
        targets = {""}
        if self.list_spaces is not None:            # 다중 저장소: 저장소 폴더는 늘 새로고침
            if not self._space_map:
                try:
                    self._space_entries()
                except Exception:  # noqa: BLE001
                    pass
            targets.update(self._space_map.keys())
        if deep:
            targets.update(self._populated_dirs)
        added = 0
        consecutive_err = 0
        for rel in sorted(targets):                 # 얕은 폴더 먼저(부모부터 반영)
            self._refresh_err = False
            added += self._refresh_one(rel)
            if self._refresh_err:
                consecutive_err += 1
                if consecutive_err >= 3:            # 서버 연결 불가로 보임 — 남은 대상 포기
                    self.log("[vfs] refresh: 연속 실패 3회 — 이번 사이클 중단")
                    break
            else:
                consecutive_err = 0
        if added:
            self.log(f"[vfs] refresh: +{added} new placeholder(s) total")
        return added

    def refresh_dir(self, space_id: str, server_dir: str) -> int:
        """서버 변경 이벤트(space_id, server_dir)를 받아 그 폴더 하나만 즉시 대조/반영한다.
        SSE(/api/sync/events) 로 폰·웹·다른 기기의 변경을 실시간으로 드라이브에 나타낸다.
        폴더가 아직 로컬에 없으면(안 열림) 조용히 넘어간다 — 열 때 온디맨드로 채워진다."""
        if self.list_dir is None:
            return 0
        rel = self._drive_rel_for(space_id, server_dir)
        if rel is None:
            return 0
        return self._refresh_one(rel)

    def _drive_rel_for(self, space_id, server_dir):
        """서버 (space_id, server_dir) → 드라이브 상대경로(posix). 매핑 불가면 None."""
        server_dir = (server_dir or "").strip("/")
        if self.list_spaces is not None:
            if not self._space_map:
                try:
                    self._space_entries()
                except Exception:  # noqa: BLE001
                    return None
            name = None
            for nm, sid in self._space_map.items():
                if sid == space_id or self._same_home(sid, space_id):
                    name = nm
                    break
            if name is None:
                return None
            return name if not server_dir else name + "/" + server_dir
        # 단일 저장소 모드
        if space_id == self.space or self._same_home(space_id, self.space):
            return server_dir
        return None

    @staticmethod
    def _same_home(a, b) -> bool:
        """서버가 홈 공간을 ''/'home' 중 무엇으로 부르든 같은 것으로 취급."""
        return a in ("", "home") and b in ("", "home")

    def _safe_to_remove(self, path: str) -> bool:
        """서버 삭제 반영으로 이 경로를 로컬에서 지워도 되는가.
        서브트리 전체가 '우리' placeholder 일 때만 True — 파일은 in-sync(수정 안 됨),
        폴더는 placeholder 이고 내용물 전부가 재귀적으로 안전해야 한다.
        사용자 드롭 실제 파일·수정된(비 in-sync) 파일이 하나라도 있으면 False(데이터 불가침).
        (예전엔 폴더 자신만 in-sync 면 rmtree 해서, 안에 있던 미업로드 파일까지 지워질 수 있었다.)"""
        try:
            st = os.stat(path, follow_symlinks=False)
        except OSError:
            return False
        state = C.CfGetPlaceholderStateFromAttributeTag(
            getattr(st, "st_file_attributes", 0), getattr(st, "st_reparse_tag", 0))
        if state == C.CF_PLACEHOLDER_STATE_INVALID or not (
                state & C.CF_PLACEHOLDER_STATE_PLACEHOLDER):
            return False                     # placeholder 아님 = 사용자 파일/폴더
        if os.path.isdir(path):
            # 폴더 placeholder(서버가 심었거나 업로드로 변환된 것) — 서버발 폴더는 in-sync 표시가
            # 없으므로(온디맨드 채우기 유지용) in-sync 를 요구하지 않는다. 내용물이 전부 안전해야 제거.
            try:
                children = os.listdir(path)
            except OSError:
                return False
            return all(self._safe_to_remove(os.path.join(path, c)) for c in children)
        return bool(state & C.CF_PLACEHOLDER_STATE_IN_SYNC)

    def _refresh_one(self, rel) -> int:
        """폴더 하나(rel, 드라이브 상대 posix)를 서버와 대조: 새 항목 추가 + 서버 삭제분 제거.
        폴링(refresh)과 실시간(refresh_dir)이 같은 폴더를 동시에 건드리지 않도록 _reconcile_lock
        으로 직렬화한다(이중 삭제/생성 경쟁 방지). 반환값은 이번에 추가된 placeholder 수."""
        local = self.root if rel == "" else os.path.join(
            self.root, rel.replace("/", os.sep))
        if not os.path.isdir(local):
            return 0
        try:
            # 서버 목록 조회(네트워크)는 락 밖에서 — 락을 잡은 채 60초 HTTP 를 기다리면
            # 다른 폴더의 실시간(SSE) 반영까지 그 뒤로 직렬화되기 때문.
            entries = self._children_for(rel)
        except Exception as e:  # noqa: BLE001
            self._refresh_err = True         # refresh() 의 연속 실패 중단 판단용
            self.log(f"[vfs] refresh '{rel}' skip: {e!r}")
            return 0
        with self._reconcile_lock:
            try:
                existing = set(os.listdir(local))
            except Exception as e:  # noqa: BLE001
                self.log(f"[vfs] refresh '{rel}' skip: {e!r}")
                return 0
            added = 0
            fresh = [e for e in entries if e["name"] not in existing]
            if fresh:
                try:
                    added = self._create_placeholders_in(local, fresh)
                    self.log(f"[vfs] refresh '{rel or '/'}': +{added}")
                except OSError as e:
                    self.log(f"[vfs] refresh create '{rel}': {e!r}")
            self._reconcile_deletions_locked(rel, local, entries, existing)
            return added

    def _reconcile_deletions_locked(self, rel, local, entries, existing):
        """서버 목록(entries)에 없는 로컬 항목 중 '우리 것'(재귀 안전 검사 통과)만 제거한다.
        _reconcile_lock 을 보유한 상태에서 호출할 것. 로컬 제거가 다시 서버 삭제로
        전파되지 않게 _suppress_delete 로 막는다(오탐 데이터 손실 방지)."""
        server_names = {e["name"] for e in entries}
        for lname in existing:
            if (lname in server_names or lname.lower() == "desktop.ini"
                    or lname.startswith(".")):
                continue
            child = os.path.join(local, lname)
            if not self._safe_to_remove(child):
                continue                 # 사용자 신규/수정/드롭 파일(포함한 폴더)은 절대 안 건드림
            crel = lname if rel == "" else rel + "/" + lname
            self._suppress_delete.add(crel)
            try:
                if os.path.isdir(child):
                    import shutil
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    os.remove(child)
                self._populated_dirs.discard(crel)
                self._save_state()
                self.log(f"[vfs] removed (server-deleted): {crel}")
            except OSError as e:  # noqa: BLE001
                self._suppress_delete.discard(crel)
                self.log(f"[vfs] remove '{crel}': {e!r}")

    # ---------------------------------------------------------------- 로컬→원격 업로드
    def _upload_target(self, frel: str):
        """드라이브 상대경로(frel) → (space_id, 서버 경로). 매핑 불가면 None."""
        if self.list_spaces is not None:
            if not self._space_map:
                try:
                    self._space_entries()
                except Exception:  # noqa: BLE001
                    return None
            parts = frel.split("/")
            sid = self._space_map.get(parts[0])
            if sid is None:
                return None
            return sid, "/".join(parts[1:])
        return self.space, frel

    def _mark_uploaded(self, local_path: str, identity: dict):
        """업로드된 실제 파일을 in-sync 플레이스홀더로 변환 → '동기화 보류중' 해소."""
        ident = json.dumps(identity).encode("utf-8")
        idbuf = ctypes.create_string_buffer(ident, len(ident))
        h = C.CreateFileW(local_path, C.GENERIC_READ | C.GENERIC_WRITE,
                          C.FILE_SHARE_READ | C.FILE_SHARE_WRITE | C.FILE_SHARE_DELETE,
                          None, C.OPEN_EXISTING, C.FILE_FLAG_BACKUP_SEMANTICS, None)
        if not h or h == C.INVALID_HANDLE_VALUE:
            raise OSError(f"CreateFileW 실패(err={ctypes.get_last_error()}): {local_path}")
        try:
            hr = C.CfConvertToPlaceholder(h, ctypes.cast(idbuf, C.LPCVOID), len(ident),
                                          C.CF_CONVERT_FLAG_MARK_IN_SYNC, None, None)
            if not C.hr_ok(hr):
                raise OSError(f"CfConvertToPlaceholder {C.hr_str(hr)}")
        finally:
            C.CloseHandle(h)

    def upload_scan(self) -> int:
        """드라이브에 새로 드롭된 '실제 파일'(플레이스홀더 아님)을 찾아 서버로 올리고
        in-sync 플레이스홀더로 바꿔 '동기화 보류중'을 해소한다.
        - 플레이스홀더(우리 파일)는 reparse point 로 판별해 건너뛴다.
        - 두 폴 주기 동안 크기·수정시각이 안 변한 '안정된' 파일만 올린다(쓰는 중 방지).
        - 이미 올린 버전은 재업로드하지 않는다.
        대상 폴더는 refresh 와 동일(루트 + 저장소 폴더 + 이번 세션에 연 폴더)."""
        if self.upload is None and self.mkdir is None:
            return 0
        targets = set(self._populated_dirs)
        targets.add("")
        if self.list_spaces is not None:
            if not self._space_map:
                try:
                    self._space_entries()
                except Exception:  # noqa: BLE001
                    pass
            targets.update(self._space_map.keys())
        uploaded = 0
        live = set()
        for rel in list(targets):
            local = self.root if rel == "" else os.path.join(
                self.root, rel.replace("/", os.sep))
            if not os.path.isdir(local):
                continue
            try:
                entries = list(os.scandir(local))
            except OSError:
                continue
            for de in entries:
                name = de.name
                if name.lower() == "desktop.ini" or name.startswith("."):
                    continue
                try:
                    is_dir = de.is_dir(follow_symlinks=False)
                    st = de.stat(follow_symlinks=False)
                except OSError:
                    continue
                attrs = getattr(st, "st_file_attributes", 0)
                state = C.CfGetPlaceholderStateFromAttributeTag(
                    attrs, getattr(st, "st_reparse_tag", 0))
                is_ph = state != C.CF_PLACEHOLDER_STATE_INVALID and (
                    state & C.CF_PLACEHOLDER_STATE_PLACEHOLDER)
                frel = name if rel == "" else rel + "/" + name
                if is_dir:
                    # 우리 placeholder 폴더면 스킵. 사용자가 만든 새 폴더면 서버 mkdir + placeholder 변환.
                    if is_ph or self.mkdir is None or self._upload_done.get(frel) == "dir":
                        continue
                    live.add(frel)
                    tgt = self._upload_target(frel)
                    if tgt and tgt[1]:
                        try:
                            self.mkdir(tgt[0], tgt[1])
                            self._mark_uploaded(
                                de.path, {"space": tgt[0], "path": tgt[1], "dir": True})
                            self._upload_done[frel] = "dir"
                            self._track_populated(frel)      # 하위 파일도 다음 스캔에 포함
                            self.log(f"[vfs] mkdir on server: {tgt[0]}:{tgt[1]}")
                        except Exception as e:  # noqa: BLE001
                            self.log(f"[vfs] mkdir '{frel}': {e!r}")
                    continue
                # ---- 파일 ----
                # 오프라인(디하이드레이트)= 로컬 데이터 없음 → 드롭 아님(우리 placeholder). 올리려고
                # 읽으면 하이드레이션 실패(무한 루프)하므로 반드시 스킵. in-sync 도 우리 파일이라 스킵.
                if attrs & C.FILE_ATTRIBUTE_OFFLINE:
                    continue
                if state != C.CF_PLACEHOLDER_STATE_INVALID and (
                        state & C.CF_PLACEHOLDER_STATE_IN_SYNC):
                    continue
                key = (st.st_size, st.st_mtime_ns)
                live.add(frel)
                if self._upload_done.get(frel) == key:      # 이미 이 버전 올림
                    continue
                if self._upload_seen.get(frel) != key:      # 아직 변하는 중 → 다음 폴에서 재확인
                    self._upload_seen[frel] = key
                    continue
                tgt = self._upload_target(frel)             # 안정됨 → 업로드
                if tgt is None:
                    continue
                sid, server_path = tgt
                if not server_path:
                    continue
                try:
                    if self.notify:
                        self.notify(f"⬆ 업로드 중: {name}")
                    self.upload(sid, server_path, de.path)
                    self._upload_done[frel] = key           # 재업로드 방지(변환 실패해도 서버엔 올라감)
                    try:
                        self._mark_uploaded(
                            de.path, {"space": sid, "path": server_path, "dir": False})
                    except Exception as e:  # noqa: BLE001
                        self.log(f"[vfs] mark in-sync '{frel}': {e!r} (서버 업로드는 성공)")
                    uploaded += 1
                    self._upload_seen.pop(frel, None)
                    self.log(f"[vfs] uploaded '{frel}' -> {sid}:{server_path}")
                    if self.notify:
                        self.notify(f"✅ 업로드 완료: {name}")
                except Exception as e:  # noqa: BLE001
                    self.log(f"[vfs] upload '{frel}' error: {e!r}")
                    if self.notify:
                        self.notify(f"⚠ 업로드 실패: {name}")
        # list() 는 C 레벨에서 원자적 스냅샷 — CfAPI 콜백(_on_delete/_on_rename)이 동시에
        # pop 해도 '순회 중 dict 변경' 예외가 나지 않는다.
        for k in list(self._upload_seen):
            if k not in live:
                self._upload_seen.pop(k, None)
        if uploaded:
            self.log(f"[vfs] upload_scan: {uploaded} file(s)")
        return uploaded

    def disconnect(self):
        if self._hydrate_pool is not None:
            self._hydrate_pool.shutdown(wait=False)   # 진행 중 다운로드는 알아서 끝남
            self._hydrate_pool = None
        if self._connected and self.conn_key is not None:
            hr = C.CfDisconnectSyncRoot(self.conn_key)
            self.log(f"[vfs] disconnect -> {C.hr_str(hr)}")
            self._connected = False

    # -------------------------------------------------------------- 플레이스홀더
    def seed(self, items):
        """items: [{'name': str, 'size': int, 'identity': dict}] — 루트에 심는다."""
        n = len(items)
        if n == 0:
            return 0
        arr = (C.CF_PLACEHOLDER_CREATE_INFO * n)()
        keep = []
        now = _now_filetime()
        for i, it in enumerate(items):
            ci = arr[i]
            ci.RelativeFileName = it["name"]
            keep.append(it["name"])
            ci.FsMetadata.FileSize = int(it["size"])
            bi = ci.FsMetadata.BasicInfo
            bi.CreationTime = now
            bi.LastAccessTime = now
            bi.LastWriteTime = now
            bi.ChangeTime = now
            bi.FileAttributes = C.FILE_ATTRIBUTE_NORMAL
            idjson = json.dumps(it["identity"]).encode("utf-8")
            idbuf = ctypes.create_string_buffer(idjson, len(idjson))
            keep.append(idbuf)
            ci.FileIdentity = ctypes.cast(idbuf, C.LPCVOID)
            ci.FileIdentityLength = len(idjson)
            ci.Flags = C.CF_PLACEHOLDER_CREATE_FLAG_MARK_IN_SYNC
        processed = C.DWORD(0)
        hr = C.CfCreatePlaceholders(self.root, arr, n, C.CF_CREATE_FLAG_NONE,
                                    byref(processed))
        if not C.hr_ok(hr):
            # 개별 항목 결과도 찍어 원인 파악
            for i in range(n):
                self.log(f"[vfs] seed[{i}] {items[i]['name']} -> {C.hr_str(arr[i].Result)}")
            raise OSError(f"CfCreatePlaceholders 실패 {C.hr_str(hr)}")
        self.log(f"[vfs] seeded {processed.value}/{n} placeholders")
        return processed.value

    # ------------------------------------------------------------------ 콜백
    def _on_fetch_data(self, info_p, params_p):
        req_off = req_len = 0
        info = None
        rel = None
        local_path = None
        meta = {}
        try:
            info = info_p[0]
            fdp = ctypes.cast(params_p, POINTER(C.FETCH_DATA_PARAMS))[0]
            req_off = int(fdp.FileOffset)
            req_len = int(fdp.RequiredLength)
            file_size = int(info.FileSize)
            meta = {}
            if info.FileIdentity and info.FileIdentityLength:
                raw = ctypes.string_at(info.FileIdentity, info.FileIdentityLength)
                meta = json.loads(raw.decode("utf-8"))
            path = info.NormalizedPath or ""
            rel = self._rel_from_normalized(path)
            local_path = self.root if rel == "" else os.path.join(
                self.root, rel.replace("/", os.sep))
            self.log(f"[vfs] FETCH_DATA {path} off={req_off} len={req_len} "
                     f"size={file_size} meta={meta}")
            self._hydrate(info.ConnectionKey, info.TransferKey,
                          meta, file_size, req_off, req_len, local_path)
        except Exception as e:  # noqa: BLE001
            # identity 가 옛 경로(rename 전)라 404 나면, 현재 로컬 이름으로 매핑해 재시도하고
            # identity 를 자가복구한다. (이미 깨진 placeholder 도 열면 스스로 고쳐짐)
            if getattr(e, "status", None) == 404 and info is not None and rel:
                tgt = self._upload_target(rel)
                if tgt and tgt[1] and (tgt[0], tgt[1]) != (meta.get("space"), meta.get("path")):
                    meta2 = {"space": tgt[0], "path": tgt[1], "dir": False}
                    self.log(f"[vfs] FETCH_DATA 404 → retry as {tgt[0]}:{tgt[1]} (self-heal)")
                    try:
                        self._hydrate(info.ConnectionKey, info.TransferKey,
                                      meta2, file_size, req_off, req_len, local_path)
                        if local_path:
                            self._update_identity(local_path, meta2)
                        return
                    except Exception as e2:  # noqa: BLE001
                        self.log(f"[vfs] self-heal failed: {e2!r}")
            self.log(f"[vfs] FETCH_DATA error: {e!r}")
            if info is not None:
                try:
                    self._transfer_fail(info.ConnectionKey, info.TransferKey,
                                        req_off, req_len)
                except Exception as e2:  # noqa: BLE001
                    self.log(f"[vfs] fail-report error: {e2!r}")

    def _pool(self) -> ThreadPoolExecutor:
        if self._hydrate_pool is None:
            self._hydrate_pool = ThreadPoolExecutor(
                max_workers=HYDRATE_WORKERS, thread_name_prefix="gendisk-hydrate")
        return self._hydrate_pool

    def _hydrate(self, conn, xfer, meta, file_size, req_off, req_len, local_path=None):
        start = req_off - (req_off % SECTOR)
        req_end = req_off + req_len
        end = min(file_size, ((req_end + SECTOR - 1) // SECTOR) * SECTOR)
        if end <= start:
            end = min(file_size, start + SECTOR)
        offsets = list(range(start, end, CHUNK))
        if not offsets:
            return

        def fetch(off):
            # Range 응답은 정확히 요청 길이만 준다(서버 206). want 만큼만 받아 조각이 딱 맞는다.
            return self.fetch_range(meta, off, min(CHUNK, end - off))

        # 여러 조각을 동시에 다운로드(프리페치)하되 전송은 순서대로(이 콜백 스레드에서만) 한다.
        # → 네트워크(느림)는 병렬로 대역폭을 합치고, CfExecute(빠름)는 직렬이라 스레드 안전.
        # 메모리는 창(window=HYDRATE_WORKERS) 크기로 제한된다.
        pool = self._pool()
        inflight = {}       # index -> Future(bytes)
        nxt = 0
        transferred = 0
        # 다운로드 진행 표시(큰 파일만): done=파일 내 현재 위치, total=파일 크기. 순차 복사면
        # off 가 커지며 0→100% 가 된다. 트래커는 갱신이 멈추면 알아서 사라진다(별도 종료 불필요).
        pkey = ("d:" + local_path) if local_path else None
        show_prog = bool(self.progress and pkey and file_size > CHUNK)
        dname = os.path.basename(local_path) if local_path else ""
        while nxt < len(offsets) and len(inflight) < HYDRATE_WORKERS:
            inflight[nxt] = pool.submit(fetch, offsets[nxt]); nxt += 1
        for i in range(len(offsets)):
            data = inflight.pop(i).result()
            if nxt < len(offsets):          # 창 유지: 다음 조각 미리 제출
                inflight[nxt] = pool.submit(fetch, offsets[nxt]); nxt += 1
            if not data:
                raise IOError(f"빈 응답 off={offsets[i]}")
            if not self._transfer(conn, xfer, offsets[i], data):
                # 앱이 취소(썸네일 종료·파일 닫힘 등) — 정상. 남은 in-flight 는 알아서 끝나고 버려진다.
                self.log(f"[vfs] hydrate canceled at off={offsets[i]} (정상)")
                # 큰 다운로드가 중간에 취소되면 부분 데이터로 파일이 꼬이지 않게(과거 손상 원인)
                # 백그라운드에서 깨끗한 플레이스홀더로 되돌린다(best-effort). 작은 미리보기는 제외.
                if transferred > CHUNK and local_path:
                    self._schedule_dehydrate(local_path)
                return
            transferred += len(data)
            if show_prog:
                self.progress(pkey, dname, "down",
                              min(file_size, offsets[i] + len(data)), file_size)

    def _transfer(self, conn, xfer, offset, data: bytes) -> bool:
        """데이터 한 조각을 Windows 로 전송. 성공 True, 앱이 취소했으면 False(중단 신호),
        그 외 실패는 예외."""
        op = C.CF_OPERATION_INFO()
        op.StructSize = ctypes.sizeof(C.CF_OPERATION_INFO)
        op.Type = C.CF_OPERATION_TYPE_TRANSFER_DATA
        op.ConnectionKey = conn
        op.TransferKey = xfer
        p = C.TRANSFER_DATA_PARAMS()
        p.ParamSize = ctypes.sizeof(C.TRANSFER_DATA_PARAMS)
        p.Flags = C.CF_OPERATION_TRANSFER_DATA_FLAG_NONE
        p.CompletionStatus = C.STATUS_SUCCESS
        buf = (ctypes.c_char * len(data)).from_buffer_copy(data)
        p.Buffer = ctypes.cast(buf, C.LPCVOID)
        p.Offset = offset
        p.Length = len(data)
        hr = C.CfExecute(byref(op), ctypes.cast(byref(p), C.LPCVOID))
        if C.is_canceled(hr):
            return False
        if not C.hr_ok(hr):
            raise OSError(f"CfExecute(TRANSFER_DATA) {C.hr_str(hr)}")
        return True

    def _transfer_fail(self, conn, xfer, offset, length):
        """하이드레이션 실패를 Windows 에 알려 열기가 매달리지 않게 한다."""
        op = C.CF_OPERATION_INFO()
        op.StructSize = ctypes.sizeof(C.CF_OPERATION_INFO)
        op.Type = C.CF_OPERATION_TYPE_TRANSFER_DATA
        op.ConnectionKey = conn
        op.TransferKey = xfer
        p = C.TRANSFER_DATA_PARAMS()
        p.ParamSize = ctypes.sizeof(C.TRANSFER_DATA_PARAMS)
        p.Flags = C.CF_OPERATION_TRANSFER_DATA_FLAG_NONE
        p.CompletionStatus = C.STATUS_UNSUCCESSFUL
        p.Buffer = None
        p.Offset = offset
        p.Length = max(0, length)
        C.CfExecute(byref(op), ctypes.cast(byref(p), C.LPCVOID))

    # ------------------------------------------------------------ 삭제 전파 / 손상 복구
    def _on_delete(self, info_p, params_p):
        """로컬에서 파일/폴더 삭제가 끝난 뒤 알림 → 서버에서도 삭제한다.
        (DELETE_COMPLETION 은 사후 알림이라 로컬 삭제를 막거나 지연시키지 않는다.)"""
        try:
            info = info_p[0]
            rel = self._rel_from_normalized(info.NormalizedPath)
            if not rel:
                return
            self._upload_seen.pop(rel, None)
            self._upload_done.pop(rel, None)
            if rel in self._suppress_delete:      # refresh 가 지운 것 → 서버 삭제 전파 금지
                self._suppress_delete.discard(rel)
                return
            tgt = self._upload_target(rel)
            if tgt is None or not tgt[1] or self.delete is None:
                return
            sid, server_path = tgt
            self.delete(sid, server_path)
            self.log(f"[vfs] deleted on server: {sid}:{server_path}")
        except Exception as e:  # noqa: BLE001
            self.log(f"[vfs] delete propagate error: {e!r}")

    def _on_rename(self, info_p, params_p):
        """로컬 이름변경/이동 완료 후 → 서버에서도 이동(move). 같은 저장소 안에서만."""
        try:
            info = info_p[0]
            rp = ctypes.cast(params_p, POINTER(C.RENAME_COMPLETION_PARAMS))[0]
            old_rel = self._rel_from_normalized(rp.SourcePath or "")
            new_rel = self._rel_from_normalized(info.NormalizedPath or "")
            if not old_rel or not new_rel or old_rel == new_rel:
                return
            for m in (self._upload_seen, self._upload_done):
                m.pop(old_rel, None)
            o = self._upload_target(old_rel)
            n = self._upload_target(new_rel)
            if not o or not n or not o[1] or not n[1] or self.rename is None:
                return
            if o[0] != n[0]:
                # 저장소 간 이동(내 파일↔work 등) — 서버가 src_space/dst_space 로 처리(shutil.move).
                self.rename(n[0], o[1], n[1], src_space=o[0], dst_space=n[0])
                self.log(f"[vfs] moved across spaces: {o[0]}:{o[1]} -> {n[0]}:{n[1]}")
            else:
                self.rename(o[0], o[1], n[1])
                self.log(f"[vfs] renamed on server: {o[0]}: {o[1]} -> {n[1]}")
            # 로컬 placeholder 의 FileIdentity 를 새 경로로 갱신 + in-sync 표시.
            # (안 하면 열 때 옛 경로로 FETCH_DATA → 404, upload_scan 이 오해해 계속 업로드.)
            new_local = self.root if new_rel == "" else os.path.join(
                self.root, new_rel.replace("/", os.sep))
            self._update_identity(
                new_local, {"space": n[0], "path": n[1], "dir": os.path.isdir(new_local)})
        except Exception as e:  # noqa: BLE001
            self.log(f"[vfs] rename propagate error: {e!r}")

    def _schedule_dehydrate(self, local_path: str):
        threading.Thread(target=self._dehydrate, args=(local_path,),
                         name="gendisk-dehydrate", daemon=True).start()

    def _dehydrate(self, local_path: str):
        """플레이스홀더를 온디맨드(빈) 상태로 되돌린다 — 취소로 남은 부분 데이터를 버려
        파일이 꼬이지 않게 하고, 다음에 열면 깨끗하게 다시 받는다. best-effort."""
        h = C.CreateFileW(local_path, C.GENERIC_READ | C.GENERIC_WRITE,
                          C.FILE_SHARE_READ | C.FILE_SHARE_WRITE | C.FILE_SHARE_DELETE,
                          None, C.OPEN_EXISTING, C.FILE_FLAG_BACKUP_SEMANTICS, None)
        if not h or h == C.INVALID_HANDLE_VALUE:
            return   # 사용 중 등으로 못 열면 조용히 포기
        try:
            hr = C.CfDehydratePlaceholder(h, 0, -1, C.CF_DEHYDRATE_FLAG_NONE, None)
            if C.hr_ok(hr):
                self.log(f"[vfs] reset(dehydrate) {local_path}")
            else:
                self.log(f"[vfs] dehydrate {C.hr_str(hr)}: {local_path}")
        except Exception as e:  # noqa: BLE001
            self.log(f"[vfs] dehydrate error: {e!r}")
        finally:
            C.CloseHandle(h)

    def _update_identity(self, local_path: str, identity: dict):
        """placeholder 의 FileIdentity(서버 경로)를 갱신하고 in-sync 로 표시한다.
        이름변경 후 옛 경로가 남아 FETCH_DATA 가 404 나던 것을 고친다. best-effort."""
        ident = json.dumps(identity).encode("utf-8")
        idbuf = ctypes.create_string_buffer(ident, len(ident))
        h = C.CreateFileW(local_path, C.GENERIC_READ | C.GENERIC_WRITE,
                          C.FILE_SHARE_READ | C.FILE_SHARE_WRITE | C.FILE_SHARE_DELETE,
                          None, C.OPEN_EXISTING, C.FILE_FLAG_BACKUP_SEMANTICS, None)
        if not h or h == C.INVALID_HANDLE_VALUE:
            return
        try:
            hr = C.CfUpdatePlaceholder(h, None, ctypes.cast(idbuf, C.LPCVOID), len(ident),
                                       None, 0, C.CF_UPDATE_FLAG_MARK_IN_SYNC, None, None)
            if not C.hr_ok(hr):
                self.log(f"[vfs] CfUpdatePlaceholder {C.hr_str(hr)}: {local_path}")
        except Exception as e:  # noqa: BLE001
            self.log(f"[vfs] update identity error: {e!r}")
        finally:
            C.CloseHandle(h)

    # 콜백의 NormalizedPath(볼륨 상대) → 서버 상대 경로(posix)
    def _rel_from_normalized(self, normp: str) -> str:
        p = (normp or "").replace("/", "\\")
        base = self._root_volrel
        if p.lower().startswith(base.lower()):
            p = p[len(base):]
        return p.strip("\\").replace("\\", "/")

    def _build_placeholders(self, entries):
        """서버 목록 → CF_PLACEHOLDER_CREATE_INFO 배열 + (호출 동안 살릴) keepalive."""
        n = len(entries)
        arr = (C.CF_PLACEHOLDER_CREATE_INFO * n)()
        keep = []
        now = _now_filetime()
        for i, e in enumerate(entries):
            ci = arr[i]
            name = e["name"]
            ci.RelativeFileName = name
            keep.append(name)
            is_dir = bool(e.get("is_dir"))
            bi = ci.FsMetadata.BasicInfo
            bi.CreationTime = bi.LastAccessTime = bi.LastWriteTime = bi.ChangeTime = now
            bi.FileAttributes = (C.FILE_ATTRIBUTE_DIRECTORY if is_dir
                                 else C.FILE_ATTRIBUTE_NORMAL)
            ci.FsMetadata.FileSize = 0 if is_dir else int(e.get("size") or 0)
            ident = json.dumps({"space": e.get("_space", self.space), "path": e["path"],
                                "dir": is_dir}).encode("utf-8")
            idbuf = ctypes.create_string_buffer(ident, len(ident))
            keep.append(idbuf)
            ci.FileIdentity = ctypes.cast(idbuf, C.LPCVOID)
            ci.FileIdentityLength = len(ident)
            # 파일: in-sync(디하이드레이트 상태). 디렉터리: in-sync 표시하면 "이미 채워짐"으로
            # 간주돼 FETCH_PLACEHOLDERS 가 안 온다 → 디렉터리는 표시하지 않아 온디맨드로 채운다.
            ci.Flags = (C.CF_PLACEHOLDER_CREATE_FLAG_NONE if is_dir
                        else C.CF_PLACEHOLDER_CREATE_FLAG_MARK_IN_SYNC)
        return arr, keep

    def _on_fetch_placeholders(self, info_p, params_p):
        """폴더를 열면 서버에서 자식 목록을 받아 플레이스홀더로 채운다.

        SMB식(always_fresh): '채움 완료(DISABLE_ON_DEMAND_POPULATION)' 표시를 하지 않아
        다음에 열 때도 이 콜백이 다시 온다 → 폴더를 열 때마다 항상 서버의 현재 목록.
        이미 로컬에 있는 항목은 빼고 전송하고(재조회 시 중복 생성 방지), 전송 후에는
        서버에서 사라진 항목을 정리한다(안전 검사 통과분만) — 삭제까지 즉시 반영."""
        info = None
        try:
            info = info_p[0]
            rel = self._rel_from_normalized(info.NormalizedPath)
            self._track_populated(rel)      # upload_scan·deep refresh 대상에 포함(+영속화)
            local = self.root if rel == "" else os.path.join(
                self.root, rel.replace("/", os.sep))
            entries = self._children_for(rel)
            try:
                existing = set(os.listdir(local))
            except OSError:
                existing = set()
            fresh = [e for e in entries if e["name"] not in existing]
            self.log(f"[vfs] FETCH_PLACEHOLDERS dir='{rel}' -> "
                     f"{len(entries)} entries (+{len(fresh)} new)")
            arr, keep = self._build_placeholders(fresh)  # noqa: F841 (keep alive)
            op = C.CF_OPERATION_INFO()
            op.StructSize = ctypes.sizeof(C.CF_OPERATION_INFO)
            op.Type = C.CF_OPERATION_TYPE_TRANSFER_PLACEHOLDERS
            op.ConnectionKey = info.ConnectionKey
            op.TransferKey = info.TransferKey
            p = C.TRANSFER_PLACEHOLDERS_PARAMS()
            p.ParamSize = ctypes.sizeof(C.TRANSFER_PLACEHOLDERS_PARAMS)
            p.Flags = (C.CF_OPERATION_TRANSFER_PLACEHOLDERS_FLAG_NONE
                       if self.always_fresh else
                       C.CF_OPERATION_TRANSFER_PLACEHOLDERS_FLAG_DISABLE_ON_DEMAND_POPULATION)
            p.PlaceholderTotalCount = len(fresh)
            p.PlaceholderArray = ctypes.cast(arr, C.LPVOID) if fresh else None
            p.PlaceholderCount = len(fresh)
            p.EntriesProcessed = 0
            hr = C.CfExecute(byref(op), ctypes.cast(byref(p), C.LPCVOID))
            if not C.hr_ok(hr):
                self.log(f"[vfs] TRANSFER_PLACEHOLDERS -> {C.hr_str(hr)}")
            # 전송(탐색기 응답)을 먼저 끝낸 뒤 서버에서 사라진 항목을 정리 — SMB처럼
            # 삭제도 열 때 바로 반영된다. (백그라운드 reconcile 과는 락으로 직렬화)
            if self.always_fresh:
                with self._reconcile_lock:
                    try:
                        existing2 = set(os.listdir(local))
                    except OSError:
                        existing2 = set()
                    self._reconcile_deletions_locked(rel, local, entries, existing2)
        except Exception as e:  # noqa: BLE001
            self.log(f"[vfs] FETCH_PLACEHOLDERS error: {e!r}")
            if info is not None:
                try:
                    op = C.CF_OPERATION_INFO()
                    op.StructSize = ctypes.sizeof(C.CF_OPERATION_INFO)
                    op.Type = C.CF_OPERATION_TYPE_TRANSFER_PLACEHOLDERS
                    op.ConnectionKey = info.ConnectionKey
                    op.TransferKey = info.TransferKey
                    p = C.TRANSFER_PLACEHOLDERS_PARAMS()
                    p.ParamSize = ctypes.sizeof(C.TRANSFER_PLACEHOLDERS_PARAMS)
                    # 실패해도 '채움 완료'로 고정하지 않는다(always_fresh) — 서버가 복구되면
                    # 다음 열기에서 다시 시도된다(SMB 가 끊겼다 붙는 것과 동일).
                    p.Flags = (C.CF_OPERATION_TRANSFER_PLACEHOLDERS_FLAG_NONE
                               if self.always_fresh else
                               C.CF_OPERATION_TRANSFER_PLACEHOLDERS_FLAG_DISABLE_ON_DEMAND_POPULATION)
                    p.EntriesProcessed = 0
                    C.CfExecute(byref(op), ctypes.cast(byref(p), C.LPCVOID))
                except Exception:
                    pass
