"""양방향 폴더 동기화 엔진.

마지막 동기화 상태(state)와 현재 로컬·원격을 3자 비교해 신규/수정/삭제/충돌을
판단한다. 충돌(양쪽 모두 수정)이면 로컬 사본을 별도 이름으로 보존해 데이터를
잃지 않는다. 로컬 상태 폴더 `.ncsync/`는 동기화 대상에서 제외한다.
"""
import hashlib
import os
import json
from datetime import datetime
from pathlib import Path

STATE_DIR = ".gendisk"
STATE_FILE = "state.json"
HASH_MAX = 8 * 1024 * 1024  # 서버 sync_etag와 동일 기준


def local_etag(path: Path, size: int, mtime_ns: int) -> str:
    """서버 sync_etag와 같은 방식 (8MB 이하는 콘텐츠 해시)."""
    if size <= HASH_MAX:
        h = hashlib.blake2b(digest_size=16)
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return "h" + h.hexdigest()
    return "s" + f"{mtime_ns:x}-{size:x}"


class SyncEngine:
    def __init__(self, client, space: str, local_root: str, log=print):
        self.client = client
        self.space = space
        self.root = Path(local_root)
        self.log = log
        self.state_path = self.root / STATE_DIR / STATE_FILE

    # ---------- 상태 ----------
    def _load_state(self) -> dict:
        try:
            with self.state_path.open(encoding="utf-8") as f:
                s = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            s = {}
        s.setdefault("files", {})   # rel -> {size, mtime_ns, etag}
        s.setdefault("dirs", [])    # [rel]
        return s

    def _save_state(self, state: dict):
        (self.root / STATE_DIR).mkdir(parents=True, exist_ok=True)
        with self.state_path.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)

    # ---------- 스캔 ----------
    def _scan_local(self):
        files, dirs = {}, set()
        for dirpath, dirnames, filenames in os.walk(self.root):
            if STATE_DIR in Path(dirpath).parts:
                continue
            for dn in list(dirnames):
                if os.path.join(dirpath, dn) == str(self.root / STATE_DIR):
                    dirnames.remove(dn)
            for dn in dirnames:
                p = Path(dirpath) / dn
                rel = p.relative_to(self.root).as_posix()
                dirs.add(rel)
            for fn in filenames:
                p = Path(dirpath) / fn
                rel = p.relative_to(self.root).as_posix()
                try:
                    st = p.stat()
                except OSError:
                    continue
                files[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
        return files, dirs

    def _scan_remote(self):
        data = self.client.enumerate(self.space)
        files, dirs = {}, set()
        for item in data.get("items", []):
            if item["is_dir"]:
                dirs.add(item["path"])
            else:
                files[item["path"]] = item["etag"]
        return files, dirs

    # ---------- 한 번의 동기화 ----------
    def run_once(self) -> dict:
        state = self._load_state()
        sfiles = state["files"]
        sdirs = set(state["dirs"])
        lfiles, ldirs = self._scan_local()
        rfiles, rdirs = self._scan_remote()

        summary = {"uploaded": 0, "downloaded": 0,
                   "deleted_local": 0, "deleted_remote": 0, "conflicts": 0}

        # 1) 신규 폴더 생성 (양방향)
        for d in sorted(rdirs - ldirs):
            if d not in sdirs:  # 원격 신규 → 로컬 생성
                (self.root / d).mkdir(parents=True, exist_ok=True)
        for d in sorted(ldirs - rdirs):
            if d not in sdirs:  # 로컬 신규 → 원격 생성
                self.client.mkdir(self.space, d)

        # 2) 파일 조정
        for rel in sorted(set(lfiles) | set(rfiles) | set(sfiles)):
            self._reconcile_file(rel, lfiles, rfiles, sfiles, summary)

        # 3) 빈 폴더 삭제 전파 (상태에 있었고 한쪽에서 사라졌으며 상대쪽이 비었을 때만)
        lfiles2, ldirs2 = self._scan_local()
        rfiles2, rdirs2 = self._scan_remote()
        for d in sorted(sdirs, key=len, reverse=True):
            in_l = (self.root / d).is_dir()
            in_r = d in rdirs2
            if in_r and not in_l:  # 로컬에서 삭제됨
                if not any(p.startswith(d + "/") for p in rfiles2):
                    self.client.delete(self.space, d)
                    summary["deleted_remote"] += 1
            elif in_l and not in_r:  # 원격에서 삭제됨
                lp = self.root / d
                if lp.is_dir() and not any(lp.iterdir()):
                    lp.rmdir()
                    summary["deleted_local"] += 1

        # 4) 상태 갱신 — 양쪽에 모두 존재하는 파일/폴더만 '동기화 완료'로 기록
        new_files = {}
        lfiles3, _ = self._scan_local()
        rfiles3, rdirs3 = self._scan_remote()
        for rel in set(lfiles3) & set(rfiles3):
            st = (self.root / rel).stat()
            new_files[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns,
                              "etag": rfiles3[rel]}
        _, ldirs3 = self._scan_local()
        self._save_state({"files": new_files, "dirs": sorted(ldirs3 & rdirs3)})
        return summary

    def _reconcile_file(self, rel, lfiles, rfiles, sfiles, summary):
        L = lfiles.get(rel)
        R = rfiles.get(rel)
        S = sfiles.get(rel)
        lp = self.root / rel

        local_changed = L is not None and (
            S is None or (L["size"], L["mtime_ns"]) != (S["size"], S["mtime_ns"]))
        remote_changed = R is not None and (S is None or R != S["etag"])

        if L is not None and R is not None:
            if not local_changed and not remote_changed:
                return
            if local_changed and not remote_changed:
                self._upload(rel); summary["uploaded"] += 1
            elif remote_changed and not local_changed:
                self._download(rel, R); summary["downloaded"] += 1
            else:  # 양쪽 모두 변경
                le = local_etag(lp, L["size"], L["mtime_ns"])
                if le == R:  # 내용이 우연히 같음 → 전송 불필요
                    return
                self._conflict_copy(rel)   # 로컬본 보존
                self._download(rel, R)     # 원본은 원격으로 맞춤
                summary["conflicts"] += 1
        elif L is not None and R is None:
            if S is None:  # 로컬 신규
                self._upload(rel); summary["uploaded"] += 1
            elif not local_changed:  # 원격에서 삭제, 로컬 그대로 → 로컬 삭제
                lp.unlink(missing_ok=True); summary["deleted_local"] += 1
            else:  # 원격 삭제 vs 로컬 수정 → 로컬 우선(재업로드)
                self._upload(rel); summary["uploaded"] += 1
        elif L is None and R is not None:
            if S is None:  # 원격 신규
                self._download(rel, R); summary["downloaded"] += 1
            elif not remote_changed:  # 로컬에서 삭제, 원격 그대로 → 원격 삭제
                self.client.delete(self.space, rel); summary["deleted_remote"] += 1
            else:  # 로컬 삭제 vs 원격 수정 → 원격 우선(재다운로드)
                self._download(rel, R); summary["downloaded"] += 1
        # L,R 모두 없음 → 상태에서 자연 제거

    # ---------- 전송 ----------
    def _upload(self, rel):
        data = (self.root / rel).read_bytes()
        self.client.put(self.space, rel, data)
        self.log(f"  ↑ {rel}")

    def _download(self, rel, etag):
        data = self.client.download(self.space, rel)
        lp = self.root / rel
        lp.parent.mkdir(parents=True, exist_ok=True)
        tmp = lp.parent / f".{lp.name}.dltmp"
        tmp.write_bytes(data)
        os.replace(tmp, lp)
        self.log(f"  ↓ {rel}")

    def _conflict_copy(self, rel):
        lp = self.root / rel
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        stem, suffix = lp.stem, lp.suffix
        conflict = lp.parent / f"{stem} (conflict {ts}){suffix}"
        lp.replace(conflict)
        self.log(f"  ⚠ 충돌: 로컬본을 {conflict.name} 으로 보존")
