"""gendisk-sync GUI: 로그인 · 폴더 동기화 · 드라이브 연결 · 시작 옵션.

customtkinter로 macOS 스타일(둥근 카드·토글 스위치·플랫 강조 버튼·시스템 다크/라이트)
창을 만든다. macOS 앱과 동일하게 **로그인 화면이 첫 화면**이고, 로그인하면 **설정 화면**으로
전환된다. 백그라운드 스레드에서 주기적으로 동기화하며, 설정에 따라 시작 시 자동 로그인 →
드라이브 자동 연결 → 자동 동기화까지 수행한다.
"""
import threading
import tkinter as tk
from tkinter import filedialog, messagebox
from urllib.parse import urlsplit

import customtkinter as ctk

from . import autostart, single_instance
from .client import (
    ApiError, AuthError, GenDiskClient, webdav_preflight, webdav_preflight_url)
from .config import Config
from .drive import DriveController
from .engine import SyncEngine
from .icon import icon_path, render_icon
from .webdav_manager import WebDavManager
from .webdav_mount import (
    cleanup_stale_webdav, connect_drive, connect_url, disconnect_drive,
    start_webclient_elevated, webclient_running)

# macOS 시스템 강조색 (라이트/다크). accent 버튼에 사용.
ACCENT = ("#007AFF", "#0A84FF")
ACCENT_HOVER = ("#0063CC", "#3D9BFF")
SUCCESS = ("#1C8A3B", "#30D158")
DANGER = ("#C7362F", "#FF453A")
MUTED = ("gray45", "gray60")

# 화면 테마 선택(설정) ↔ customtkinter 모드 매핑
_THEME_LABELS = {"light": "라이트", "dark": "다크", "system": "자동"}
_THEME_MODES = {v: k for k, v in _THEME_LABELS.items()}

# 앱 시작 시 한 번만: 기본은 시스템 테마, macOS풍 파란 강조 테마 사용.
# (실제 적용 모드는 App.__init__ 에서 저장된 설정값으로 덮어쓴다.)
ctk.set_appearance_mode("system")
ctk.set_default_color_theme("blue")


class SyncWorker(threading.Thread):
    """백그라운드 동기화 루프. enabled일 때 interval마다 run_once()."""

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self._stop = threading.Event()
        self._wake = threading.Event()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def sync_now(self):
        self._wake.set()

    def run(self):
        while not self._stop.is_set():
            cfg = self.app.cfg
            if cfg.enabled and cfg.is_ready():
                try:
                    client = GenDiskClient(cfg.server_url, cfg.token)
                    engine = SyncEngine(client, cfg.space, cfg.local_folder, log=self.app.log)
                    self.app.set_status("동기화 중…", SUCCESS)
                    summary = engine.run_once()
                    self.app.log(f"동기화 완료: {summary}")
                    self.app.set_status("대기 중 (마지막 동기화 성공)", SUCCESS)
                except AuthError:
                    self.app.set_status("세션 만료 — 자동 재로그인 시도", DANGER)
                    self.app.try_relogin()
                except (ApiError, OSError) as e:
                    self.app.set_status("동기화 오류", DANGER)
                    self.app.log(f"오류: {e}")
            self._wake.wait(timeout=max(5, self.app.cfg.interval_sec))
            self._wake.clear()


class App:
    def __init__(self, startup: bool = False):
        self.cfg = Config.load()
        # 자동시작이 켜져 있으면 Run 키를 현재 exe(안정 사본) 경로로 자가 치유한다.
        # (바탕화면 exe 를 지우거나 이름을 바꿔도 자동시작이 계속 동작하도록)
        if self.cfg.auto_start:
            try:
                autostart.sync(True)
            except Exception:
                pass
        # 저장된 화면 테마(light/dark/system) 적용
        ctk.set_appearance_mode(self.cfg.appearance if self.cfg.appearance in _THEME_MODES.values() else "system")
        # 이번 세션의 비밀번호(WebDAV 드라이브 연결용). 저장 여부와 무관하게 메모리에 보관.
        self._pw = self.cfg.get_password()
        self.root = ctk.CTk()
        self.root.title("genDISK")
        self.root.geometry("1120x772")   # 2열 배치 — 스크롤 없이 콘텐츠가 맞는 높이
        self.root.minsize(980, 700)
        self._apply_window_icon()
        self._build_ui()
        self.worker = SyncWorker(self)
        self.worker.start()
        self.drive = DriveController(self.cfg, on_reauth=self._drive_reauth, log=self.log)
        self.tray = None
        self._tray_notified = False
        self._build_tray()
        # 두 번째 실행이 신호를 보내면 이 창을 전면화한다(단일 인스턴스).
        try:
            single_instance.start_show_listener(self._bring_to_front)
        except Exception:
            pass
        # genDISK Drive 가 켜져 있고 로그인돼 있으면 시작 시 연결
        if self.cfg.vfs_enabled and self.cfg.token:
            self._start_drive_async()
        # 저장된 일반 WebDAV 연결 중 '자동 연결' 항목을 시작 시 마운트 (genDISK 로그인과 무관)
        if any(m.get("auto") for m in self.cfg.webdav_mounts):
            threading.Thread(target=self._auto_connect_webdav_mounts, daemon=True).start()
        # 닫기(X)는 트레이가 있으면 트레이로 숨기고, 없으면 그냥 종료
        self.root.protocol("WM_DELETE_WINDOW",
                           self._hide_to_tray if self.tray else self._real_quit)
        if startup:
            # 자동 시작이면 트레이만 남기고 창은 숨김 (트레이 없으면 최소화)
            (self._hide_to_tray if self.tray else self.root.iconify)()
        # 시작 시 자동 로그인 → (설정 시) 드라이브 연결 → 동기화 트리거
        if self.cfg.auto_login and self.cfg.username and self.cfg.get_password():
            threading.Thread(target=self._auto_sequence, daemon=True).start()

    # ---------- 창/트레이 아이콘 ----------
    def _apply_window_icon(self):
        """창(제목줄·작업표시줄) 아이콘을 genDISK 마크로. customtkinter 가 200ms 뒤
        기본 아이콘으로 덮으므로 그 이후에도 한 번 더 적용한다."""
        def _set():
            try:
                self.root.iconbitmap(icon_path())
            except Exception:
                pass
        _set()
        self.root.after(300, _set)

    # ---------- 시스템 트레이 ----------
    def _build_tray(self):
        try:
            import pystray
        except Exception:
            return  # pystray 없으면 트레이 비활성 (닫기 = 종료)
        img = render_icon(64)   # 안드로이드 앱과 동일한 마크
        menu = pystray.Menu(
            pystray.MenuItem("열기", self._tray_show, default=True),
            pystray.MenuItem("지금 동기화", lambda i, it: self.root.after(0, self._sync_now)),
            pystray.MenuItem("종료", self._tray_quit),
        )
        try:
            self.tray = pystray.Icon("gendisk-sync", img, "genDISK Sync", menu)
            threading.Thread(target=self.tray.run, daemon=True).start()
        except Exception:
            self.tray = None

    def _hide_to_tray(self):
        self._collect(); self.cfg.save()
        self.root.withdraw()
        if not self._tray_notified and self.tray is not None:
            self._tray_notified = True
            try:
                self.tray.notify("트레이에서 계속 실행됩니다. 아이콘을 눌러 다시 열 수 있어요.",
                                 "genDISK Sync")
            except Exception:
                pass

    def _tray_show(self, icon=None, item=None):
        self._bring_to_front()

    def _bring_to_front(self):
        """창을 복원·전면화한다 (트레이 클릭 / 두 번째 실행 신호). 스레드 안전."""
        def _show():
            try:
                self.root.deiconify()
                self.root.lift()
                self.root.attributes("-topmost", True)
                self.root.after(300, lambda: self.root.attributes("-topmost", False))
                self.root.focus_force()
            except Exception:
                pass
        self.root.after(0, _show)

    def _tray_quit(self, icon=None, item=None):
        self.root.after(0, self._real_quit)

    def _real_quit(self):
        self._collect(); self.cfg.save()
        try:
            single_instance.cleanup()
        except Exception:
            pass
        try:
            self.drive.stop()   # provider 연결만 해제(노드/싱크루트는 유지 → 다음 실행 시 재연결)
        except Exception:
            pass
        self.worker.stop()
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()

    # ---------- UI 공통 ----------
    def _card(self, parent, title):
        """제목이 붙은 둥근 카드. 내용을 담을 안쪽 프레임을 돌려준다."""
        card = ctk.CTkFrame(parent, corner_radius=12)
        card.pack(fill="x", pady=(0, 14))
        if title:
            ctk.CTkLabel(card, text=title, font=self.font_h, anchor="w").pack(
                fill="x", padx=16, pady=(12, 0))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=16, pady=(8, 14))
        return inner

    def _field_label(self, parent, text, **kw):
        return ctk.CTkLabel(parent, text=text, font=self.font_s, text_color=MUTED,
                            anchor="w", **kw)

    def _logo(self, parent):
        """헤더 로고: genDISK G 마크 아이콘 + 'genDISK' 텍스트 (안드로이드 앱과 동일)."""
        img = render_icon(64)  # 고해상도로 렌더 → CTkImage 가 표시 크기로 축소(HiDPI 대응)
        logo = ctk.CTkImage(light_image=img, dark_image=img, size=(28, 28))
        return ctk.CTkLabel(parent, image=logo, text="  genDISK",
                            font=self.font_title, compound="left")

    def _build_ui(self):
        self.font_title = ctk.CTkFont(family="Segoe UI", size=22, weight="bold")
        self.font_h = ctk.CTkFont(family="Segoe UI", size=14, weight="bold")
        self.font_s = ctk.CTkFont(family="Segoe UI", size=12)
        self.font_mono = ctk.CTkFont(family="Consolas", size=12)

        self.container = ctk.CTkFrame(self.root, fg_color="transparent")
        self.container.pack(fill="both", expand=True)

        self.login_frame = self._build_login(self.container)
        self.settings_frame = self._build_settings(self.container)

        # macOS 앱과 동일: 로그인돼 있으면 설정 화면, 아니면 로그인 화면부터.
        if self.cfg.token:
            self._show_settings()
            self._refresh_spaces_async()   # 저장된 토큰으로 시작 시 저장소 목록 채우기
        else:
            self._show_login()

    def _show_login(self):
        self.settings_frame.pack_forget()
        self.login_frame.pack(fill="both", expand=True)

    def _show_settings(self):
        self.login_frame.pack_forget()
        self._refresh_account_labels()
        self.settings_frame.pack(fill="both", expand=True)

    # ---------- 로그인 화면 ----------
    def _build_login(self, parent):
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        card = ctk.CTkFrame(frame, corner_radius=16)
        card.place(relx=0.5, rely=0.5, anchor="center")
        pad = ctk.CTkFrame(card, fg_color="transparent")
        pad.pack(padx=32, pady=28)

        self._logo(pad).pack()
        self.lbl_login_subtitle = ctk.CTkLabel(
            pad, text="로그인하고 파일을 동기화·연결하세요",
            font=self.font_s, text_color=MUTED)
        self.lbl_login_subtitle.pack(pady=(2, 14))

        # 접속 모드 토글 (안드로이드와 동일): genDISK 계정 로그인 / 일반 WebDAV 드라이브 연결
        self._login_mode = "genDISK"
        self.seg_login_mode = ctk.CTkSegmentedButton(
            pad, width=320, values=["genDISK", "WebDAV"], command=self._on_login_mode)
        self.seg_login_mode.set("genDISK")
        self.seg_login_mode.pack(pady=(0, 14))

        self.e_url = ctk.CTkEntry(pad, width=320,
                                  placeholder_text="서버 주소 (예: https://gendisk.cloud)")
        self.e_url.pack(pady=4)
        if self.cfg.server_url:  # 빈 값 insert는 placeholder를 없애므로 값이 있을 때만
            self.e_url.insert(0, self.cfg.server_url)
        self.e_user = ctk.CTkEntry(pad, width=320, placeholder_text="아이디")
        self.e_user.pack(pady=4)
        if self.cfg.username:
            self.e_user.insert(0, self.cfg.username)
        self.e_pw = ctk.CTkEntry(pad, width=320, show="•", placeholder_text="비밀번호")
        self.e_pw.pack(pady=4)
        if self.cfg.get_password():
            self.e_pw.insert(0, self.cfg.get_password())
        self.e_pw.bind("<Return>", lambda e: self._login_submit())

        # WebDAV 모드에서만 보이는 드라이브 문자 선택 (기본 숨김)
        self.frm_login_drive = ctk.CTkFrame(pad, fg_color="transparent")
        self._field_label(self.frm_login_drive, "드라이브 문자").pack(side="left")
        self.cmb_login_drive = ctk.CTkOptionMenu(
            self.frm_login_drive, width=90,
            values=[f"{ch}:" for ch in "DEFGHIJKLMNOPQRSTUVWXYZ"])
        self.cmb_login_drive.set("W:")
        self.cmb_login_drive.pack(side="left", padx=(10, 0))

        self.var_savecred = tk.BooleanVar(value=self.cfg.save_credentials)
        self.sw_savecred = ctk.CTkSwitch(pad, text="로그인 정보 저장 (암호화)",
                                         variable=self.var_savecred)
        self.sw_savecred.pack(anchor="w", pady=(10, 0))

        self.lbl_login_error = ctk.CTkLabel(pad, text="", font=self.font_s,
                                            text_color=DANGER, wraplength=320)
        self.lbl_login_error.pack(pady=(8, 0))

        self.btn_login = ctk.CTkButton(pad, text="로그인", width=320,
                                       command=self._login_submit,
                                       fg_color=ACCENT, hover_color=ACCENT_HOVER)
        self.btn_login.pack(pady=(8, 0))
        return frame

    def _on_login_mode(self, mode):
        """로그인 화면 모드 전환(genDISK ↔ WebDAV)."""
        self._login_mode = mode
        self.lbl_login_error.configure(text="")
        cur = self.e_url.get().strip()
        if mode == "WebDAV":
            # genDISK 주소가 그대로 프리필돼 있으면 비워서 WebDAV placeholder 안내가 보이게 한다.
            if cur == (self.cfg.server_url or ""):
                self.e_url.delete(0, "end")
            self.lbl_login_subtitle.configure(text="WebDAV 서버를 드라이브로 연결합니다")
            self.e_url.configure(placeholder_text="WebDAV 주소 (예: https://호스트:포트/dav)")
            self.frm_login_drive.pack(fill="x", pady=(8, 0), before=self.sw_savecred)
            self.btn_login.configure(text="연결")
        else:
            # WebDAV 로 비웠던 경우 genDISK 주소를 되살린다.
            if not cur and self.cfg.server_url:
                self.e_url.insert(0, self.cfg.server_url)
            self.lbl_login_subtitle.configure(text="로그인하고 파일을 동기화·연결하세요")
            self.e_url.configure(placeholder_text="서버 주소 (예: https://gendisk.cloud)")
            self.frm_login_drive.pack_forget()
            self.btn_login.configure(text="로그인")

    def _login_submit(self):
        """기본 버튼/Enter 처리 — 로그인 화면 모드에 따라 분기."""
        if self._login_mode == "WebDAV":
            self._webdav_login()
        else:
            self._login()

    # ---------- 설정 화면 (로그인 후) ----------
    def _build_settings(self, parent):
        frame = ctk.CTkFrame(parent, fg_color="transparent")

        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", padx=22, pady=(18, 6))
        self._logo(header).pack(side="left")
        ctk.CTkButton(header, text="로그아웃", width=88, command=self._logout,
                      fg_color="transparent", border_width=1,
                      text_color=DANGER, hover_color=("gray90", "gray25")).pack(side="right")

        # 스크롤 없는 일반 프레임 — 창 높이가 콘텐츠에 맞춰 스크롤바가 안 나온다.
        body = ctk.CTkFrame(frame, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        body.grid_columnconfigure(0, weight=1, uniform="col")
        body.grid_columnconfigure(1, weight=1, uniform="col")
        left = ctk.CTkFrame(body, fg_color="transparent")
        left.grid(row=0, column=0, sticky="new", padx=(0, 8))
        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=0, column=1, sticky="new", padx=(8, 0))

        # ══════ 왼쪽 열 ══════
        # ── 계정 ──
        c = self._card(left, "계정")
        self.lbl_acc_server = self._field_label(c, "")
        self.lbl_acc_server.pack(fill="x")
        self.lbl_acc_user = self._field_label(c, "")
        self.lbl_acc_user.pack(fill="x", pady=(2, 0))

        # ── 동기화 ──
        c = self._card(left, "동기화")
        self._field_label(c, "동기화할 저장소").pack(fill="x")
        self.cmb_space = ctk.CTkOptionMenu(c, values=["home"])
        self.cmb_space.set(self.cfg.space); self.cmb_space.pack(fill="x", pady=(2, 10))

        self._field_label(c, "로컬 폴더").pack(fill="x")
        frow = ctk.CTkFrame(c, fg_color="transparent"); frow.pack(fill="x", pady=(2, 10))
        self.e_folder = ctk.CTkEntry(frow, placeholder_text="C:\\Users\\…")
        self.e_folder.pack(side="left", fill="x", expand=True)
        if self.cfg.local_folder:
            self.e_folder.insert(0, self.cfg.local_folder)
        ctk.CTkButton(frow, text="찾아보기", width=90, command=self._pick_folder).pack(
            side="left", padx=(8, 0))

        irow = ctk.CTkFrame(c, fg_color="transparent"); irow.pack(fill="x")
        self._field_label(irow, "동기화 주기(초)").pack(side="left")
        self.e_interval = ctk.CTkEntry(irow, width=90)
        self.e_interval.pack(side="left", padx=(10, 0))
        self.e_interval.insert(0, str(self.cfg.interval_sec))

        self.var_enabled = tk.BooleanVar(value=self.cfg.enabled)
        ctk.CTkSwitch(c, text="자동 동기화 켜기", variable=self.var_enabled,
                      command=self._toggle_enabled).pack(anchor="w", pady=(12, 8))

        brow = ctk.CTkFrame(c, fg_color="transparent"); brow.pack(fill="x")
        ctk.CTkButton(brow, text="설정 저장", width=110, command=self._save).pack(side="left")
        ctk.CTkButton(brow, text="지금 동기화", width=110, command=self._sync_now,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER).pack(side="left", padx=(8, 0))

        # ── 드라이브 연결 (WebDAV) ──
        c = self._card(left, "드라이브 연결 (WebDAV)")
        self._field_label(c, "일반 디스크처럼 사용 — 파일 탐색기에 드라이브로 연결합니다.").pack(fill="x")
        drow = ctk.CTkFrame(c, fg_color="transparent"); drow.pack(fill="x", pady=(8, 8))
        self._field_label(drow, "드라이브 문자").pack(side="left")
        self.cmb_drive = ctk.CTkOptionMenu(drow, width=80,
                                           values=[f"{ch}:" for ch in "NPQRSTVWXYZ"])
        self.cmb_drive.set(self.cfg.drive_letter); self.cmb_drive.pack(side="left", padx=(10, 0))
        ctk.CTkButton(drow, text="연결", width=70, command=self._connect_drive,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER).pack(side="left", padx=(8, 0))
        ctk.CTkButton(drow, text="해제", width=70, command=self._disconnect_drive).pack(
            side="left", padx=(8, 0))
        ctk.CTkButton(c, text="Windows WebClient 서비스 켜기", command=self._start_webclient,
                      fg_color="transparent", border_width=1,
                      text_color=ACCENT, hover_color=("gray90", "gray25")).pack(fill="x")
        ctk.CTkButton(c, text="🧹 끊긴 WebDAV 연결 정리", command=self._cleanup_webdav,
                      fg_color="transparent", border_width=1,
                      text_color=MUTED, hover_color=("gray90", "gray25")).pack(fill="x", pady=(6, 0))
        ctk.CTkButton(c, text="🌐 일반 WebDAV 서버 연결…", command=self._open_webdav_manager,
                      fg_color="transparent", border_width=1,
                      text_color=ACCENT, hover_color=("gray90", "gray25")).pack(fill="x", pady=(6, 0))

        # ══════ 오른쪽 열 ══════
        # ── 시작 옵션 ──
        c = self._card(right, "시작 옵션")
        self.var_autostart = tk.BooleanVar(value=self.cfg.auto_start)
        self.var_autologin = tk.BooleanVar(value=self.cfg.auto_login)
        self.var_autodrive = tk.BooleanVar(value=self.cfg.auto_connect_drive)
        ctk.CTkSwitch(c, text="Windows 시작 시 자동 실행", variable=self.var_autostart,
                      command=self._apply_autostart).pack(anchor="w", pady=6)
        ctk.CTkSwitch(c, text="프로그램 시작 시 자동 로그인", variable=self.var_autologin).pack(
            anchor="w", pady=6)
        ctk.CTkSwitch(c, text="자동 로그인 후 드라이브 자동 연결", variable=self.var_autodrive).pack(
            anchor="w", pady=6)

        # ── 화면 테마 ──
        c = self._card(right, "화면 테마")
        self.seg_theme = ctk.CTkSegmentedButton(
            c, values=list(_THEME_LABELS.values()), command=self._on_theme_change)
        self.seg_theme.set(_THEME_LABELS.get(self.cfg.appearance, "자동"))
        self.seg_theme.pack(fill="x")

        # ── genDISK Drive (온디맨드 클라우드) ──
        c = self._card(right, "genDISK Drive (온디맨드)")
        self._field_label(
            c, "iCloud처럼 탐색기 사이드바에 genDISK 드라이브로 나타납니다.\n"
               "파일은 목록만 먼저 보이고, 열 때 자동으로 내려받습니다(온디맨드).").pack(fill="x")
        self.var_vfs = tk.BooleanVar(value=self.cfg.vfs_enabled)
        ctk.CTkSwitch(c, text="genDISK Drive 연결", variable=self.var_vfs,
                      command=self._toggle_vfs).pack(anchor="w", pady=(8, 0))

        # ── 상태 & 로그 ──
        c = self._card(right, "상태")
        self.lbl_status = ctk.CTkLabel(c, text="대기 중", font=self.font_s,
                                       text_color=MUTED, anchor="w")
        self.lbl_status.pack(fill="x", pady=(0, 6))
        self.txt_log = ctk.CTkTextbox(c, height=150, wrap="word", font=self.font_mono)
        self.txt_log.pack(fill="both", expand=True)
        self.txt_log.configure(state="disabled")
        return frame

    def _refresh_account_labels(self):
        self.lbl_acc_server.configure(text=f"서버   {self.cfg.server_url or '-'}")
        self.lbl_acc_user.configure(text=f"아이디   {self.cfg.username or '-'}")

    # ---------- 동작 ----------
    def _login(self):
        url = self.e_url.get().strip()
        user = self.e_user.get().strip()
        pw = self.e_pw.get()
        if not url or not user or not pw:
            self.lbl_login_error.configure(text="서버 주소·아이디·비밀번호를 모두 입력하세요.")
            return
        self.lbl_login_error.configure(text="")
        self.cfg.save_credentials = self.var_savecred.get()
        try:
            c = GenDiskClient(url)
            c.login(user, pw)
            self.cfg.server_url, self.cfg.username, self.cfg.token = url, user, c.token
            self._pw = pw
            if self.cfg.save_credentials:
                self.cfg.set_password(pw)
            else:
                self.cfg.clear_password()
            self.cfg.save()
            self._refresh_spaces(c)
            self._show_settings()
            self.log("로그인 성공")
            if self.cfg.vfs_enabled:
                self._start_drive_async()
        except AuthError as e:
            self.lbl_login_error.configure(text=str(e))
        except (ApiError, OSError) as e:
            self.lbl_login_error.configure(text=str(e))

    def _webdav_login(self):
        """로그인 화면 WebDAV 모드: 임의 WebDAV 서버를 드라이브로 연결한다.
        성공하면 연결을 목록에 저장하고 관리 창을 연다 (탐색기가 곧 파일 브라우저)."""
        url = self.e_url.get().strip()
        user = self.e_user.get().strip()
        pw = self.e_pw.get()
        drive = self.cmb_login_drive.get()
        if not url or not user or not pw:
            self.lbl_login_error.configure(text="주소·아이디·비밀번호를 모두 입력하세요.")
            return
        if "://" not in url:
            url = "https://" + url
        url = url.rstrip("/")
        if url.lower().startswith("http://") and not messagebox.askyesno(
                "보안 경고",
                "http(암호화 안 됨) 주소입니다. 비밀번호가 평문으로 전송될 수 있고\n"
                "Windows도 기본적으로 http WebDAV의 Basic 인증을 막습니다.\n\n계속할까요?"):
            return
        self.lbl_login_error.configure(text="")
        self.cfg.save_credentials = self.var_savecred.get()
        self.btn_login.configure(state="disabled", text="연결 중…")

        def work():
            try:
                connect_url(drive, url, user, pw)
            except Exception as e:  # noqa: BLE001
                err = str(e)
                diag = ""
                try:
                    webdav_preflight_url(url, user, pw)
                except RuntimeError as pe:
                    diag = "\n" + str(pe)
                except Exception:
                    pass
                if not diag and not webclient_running():
                    diag = "\nWindows 'WebClient' 서비스가 꺼져 있을 수 있습니다."
                self.root.after(0, lambda: self._webdav_login_done(
                    False, err + diag, url, user, pw, drive))
                return
            self.root.after(0, lambda: self._webdav_login_done(True, "", url, user, pw, drive))

        threading.Thread(target=work, daemon=True).start()

    def _webdav_login_done(self, ok, err, url, user, pw, drive):
        self.btn_login.configure(state="normal", text="연결")
        if not ok:
            self.lbl_login_error.configure(text=err)
            return
        self._save_webdav_mount(url, user, pw, drive)
        self.log(f"[WebDAV] {drive} 드라이브로 연결했습니다: {url}")
        messagebox.showinfo(
            "연결됨", f"{drive} 드라이브로 연결했습니다.\n탐색기에서 확인하세요.")
        self._open_webdav_manager(collect=False)

    def _save_webdav_mount(self, url, user, pw, drive):
        """방금 연결한 WebDAV 를 저장 목록에 추가/갱신(같은 url+drive 는 갱신)."""
        from . import secret
        enc = secret.encrypt(pw) or "" if self.var_savecred.get() else ""
        name = urlsplit(url).hostname or url
        for m in self.cfg.webdav_mounts:
            if m.get("url") == url and m.get("drive") == drive:
                m["username"] = user
                m["password_enc"] = enc
                if not m.get("name"):
                    m["name"] = name
                self.cfg.save()
                return
        self.cfg.webdav_mounts.append({
            "name": name, "url": url, "username": user,
            "password_enc": enc, "drive": drive, "auto": False,
        })
        self.cfg.save()

    def _logout(self):
        """세션을 지우고 로그인 화면으로. 명시적 로그아웃이므로 자동 로그인/저장 비번도 끈다.
        (서버 주소·아이디는 재로그인 편의를 위해 유지)"""
        self._collect()
        self.cfg.token = ""
        self._pw = ""
        # 명시적 로그아웃 → 다음 실행에서 자동 로그인하지 않고, 저장된 비번도 제거
        self.cfg.auto_login = False
        self.var_autologin.set(False)
        self.cfg.clear_password()
        self.cfg.save()
        self.e_pw.delete(0, "end")   # 저장 비번을 지웠으니 프리필도 비움
        self.lbl_login_error.configure(text="")
        self._show_login()

    def _auto_sequence(self):
        """시작 시: 자동 로그인 → (설정 시) 드라이브 연결 → 동기화 트리거."""
        cfg = self.cfg
        pw = cfg.get_password()
        try:
            c = GenDiskClient(cfg.server_url)
            c.login(cfg.username, pw)
            cfg.token = c.token
            self._pw = pw
            cfg.save()
            self.root.after(0, self._show_settings)
            self.root.after(0, self._refresh_spaces_async)
            self.set_status("자동 로그인 성공", SUCCESS)
            self.log("자동 로그인 성공")
        except Exception as e:
            self.set_status("자동 로그인 실패", DANGER)
            self.log(f"자동 로그인 실패: {e}")
            return
        if cfg.auto_connect_drive:
            try:
                connect_drive(cfg.drive_letter, cfg.server_url, cfg.username, pw)
                self.log(f"{cfg.drive_letter} 드라이브 자동 연결")
            except Exception as e:
                self.log(f"드라이브 자동 연결 실패: {e}")
        if cfg.vfs_enabled:
            self._start_drive_async()
        if cfg.enabled:
            self.worker.sync_now()

    def try_relogin(self):
        """세션 만료 시 저장된 정보로 조용히 재로그인."""
        pw = self.cfg.get_password() or self._pw
        if not (self.cfg.username and pw):
            return
        try:
            c = GenDiskClient(self.cfg.server_url)
            c.login(self.cfg.username, pw)
            self.cfg.token = c.token
            self.cfg.save()
            self.log("세션 재로그인 성공")
        except Exception as e:
            self.log(f"재로그인 실패: {e}")

    # ---------- genDISK Drive (온디맨드) ----------
    def _drive_reauth(self) -> bool:
        """드라이브 콜백에서 세션 만료 시 저장된 정보로 재로그인 (성공 시 cfg.token 갱신)."""
        pw = self.cfg.get_password() or self._pw
        if not (self.cfg.server_url and self.cfg.username and pw):
            return False
        try:
            c = GenDiskClient(self.cfg.server_url)
            c.login(self.cfg.username, pw)
            self.cfg.token = c.token
            self.cfg.save()
            return True
        except Exception:
            return False

    def _start_drive_async(self):
        try:
            space = self.cmb_space.get() or self.cfg.space   # 위젯 접근은 메인 스레드에서
        except Exception:
            space = self.cfg.space
        def work():
            try:
                self.cfg.space = space
                self.drive.start()
                self.cfg.vfs_enabled = True
                self.cfg.save()
                self.set_status("genDISK Drive 연결됨", SUCCESS)
                self.log("genDISK Drive 를 연결했습니다 (탐색기 사이드바 확인).")
            except Exception as e:  # noqa: BLE001
                self.log(f"genDISK Drive 연결 실패: {e}")
                self.set_status("genDISK Drive 연결 실패", DANGER)
                self.root.after(0, lambda: self.var_vfs.set(False))
        threading.Thread(target=work, daemon=True).start()

    def _toggle_vfs(self):
        if self.var_vfs.get():
            if not (self.cfg.server_url and self.cfg.token):
                messagebox.showwarning("로그인 필요", "먼저 로그인하세요.")
                self.var_vfs.set(False)
                return
            self._start_drive_async()
        else:
            def work():
                self.drive.stop(remove_node=True)
                self.cfg.vfs_enabled = False
                self.cfg.save()
                self.set_status("genDISK Drive 해제됨", MUTED)
            threading.Thread(target=work, daemon=True).start()

    def _refresh_spaces(self, client):
        try:
            spaces = [s["id"] for s in client.spaces()]
            self.cmb_space.configure(values=spaces or ["home"])
            if self.cfg.space not in spaces:
                self.cmb_space.set(spaces[0] if spaces else "home")
        except Exception:
            pass

    def _refresh_spaces_async(self):
        """저장된 토큰으로 시작했을 때 저장소 목록을 백그라운드에서 채운다(네트워크는 스레드에서)."""
        if not (self.cfg.server_url and self.cfg.token):
            return
        def work():
            try:
                spaces = [s["id"] for s in GenDiskClient(self.cfg.server_url, self.cfg.token).spaces()]
            except Exception:
                return
            def apply():
                self.cmb_space.configure(values=spaces or ["home"])
                if self.cfg.space not in spaces:
                    self.cmb_space.set(spaces[0] if spaces else "home")
            self.root.after(0, apply)   # 위젯 갱신은 메인 스레드에서
        threading.Thread(target=work, daemon=True).start()

    def _pick_folder(self):
        d = filedialog.askdirectory()
        if d:
            self.e_folder.delete(0, "end"); self.e_folder.insert(0, d)

    def _apply_autostart(self):
        try:
            autostart.sync(self.var_autostart.get())
        except OSError as e:
            messagebox.showerror("자동 실행 등록 실패", str(e))

    def _on_theme_change(self, label):
        """라이트/다크/자동 선택 → 즉시 적용 + 저장."""
        mode = _THEME_MODES.get(label, "system")
        self.cfg.appearance = mode
        ctk.set_appearance_mode(mode)
        self.cfg.save()

    def _collect(self):
        """설정 화면의 값들을 cfg에 모은다. (서버/아이디/토큰은 로그인에서만 설정)"""
        self.cfg.space = self.cmb_space.get() or "home"
        self.cfg.local_folder = self.e_folder.get().strip()
        self.cfg.drive_letter = self.cmb_drive.get()
        try:
            self.cfg.interval_sec = max(5, int(self.e_interval.get()))
        except ValueError:
            self.cfg.interval_sec = 30
        self.cfg.enabled = self.var_enabled.get()
        self.cfg.save_credentials = self.var_savecred.get()
        self.cfg.auto_start = self.var_autostart.get()
        self.cfg.auto_login = self.var_autologin.get()
        self.cfg.auto_connect_drive = self.var_autodrive.get()
        if not self.cfg.save_credentials:
            self.cfg.clear_password()
        elif self._pw:
            self.cfg.set_password(self._pw)

    def _save(self):
        self._collect()
        self.cfg.save()
        self._apply_autostart()
        self.log("설정을 저장했습니다.")

    def _toggle_enabled(self):
        self._collect(); self.cfg.save(); self.worker.sync_now()

    def _sync_now(self):
        self._collect(); self.cfg.save()
        if not self.cfg.is_ready():
            messagebox.showwarning("설정 필요", "로그인하고 로컬 폴더를 지정하세요.")
            return
        self.worker.sync_now()

    def _connect_drive(self):
        self._collect()
        pw = self._pw or self.cfg.get_password()
        if not self.cfg.server_url or not self.cfg.username or not pw:
            messagebox.showwarning("정보 필요", "로그인 후 다시 시도하세요 (비밀번호가 필요합니다).")
            return
        # 먼저 서버의 /dav 를 직접 확인 → 서버 문제와 로컬(WebClient) 문제를 구분
        try:
            webdav_preflight(self.cfg.server_url, self.cfg.username, pw)
        except RuntimeError as e:
            messagebox.showerror("서버 확인 실패 (서버 측 문제)", str(e))
            return
        # 서버 WebDAV는 정상 → 로컬에서 드라이브로 마운트
        try:
            connect_drive(self.cmb_drive.get(), self.cfg.server_url, self.cfg.username, pw)
            self.log(f"{self.cmb_drive.get()} 드라이브로 연결했습니다.")
        except Exception as e:
            extra = ""
            if not webclient_running():
                extra = ("\n\n▶ 원인: Windows 'WebClient' 서비스가 꺼져 있습니다.\n"
                         "   아래 'WebClient 서비스 켜기' 버튼을 눌러(관리자 승인) 켠 뒤 다시 연결하세요.")
            messagebox.showerror(
                "드라이브 연결 실패 (로컬 측)",
                "서버의 WebDAV는 정상 확인됐습니다. Windows 쪽 문제입니다.\n\n" + str(e) + extra)

    def _start_webclient(self):
        if webclient_running():
            messagebox.showinfo("WebClient", "WebClient 서비스가 이미 실행 중입니다.")
            return
        try:
            start_webclient_elevated()  # UAC 프롬프트
        except Exception as e:
            messagebox.showerror("WebClient 시작 실패", str(e))
            return
        import time
        time.sleep(1.5)
        if webclient_running():
            self.log("WebClient 서비스를 켰습니다. 이제 드라이브 연결을 다시 시도하세요.")
            messagebox.showinfo("WebClient", "WebClient 서비스를 켰습니다.\n'연결'을 다시 눌러주세요.")
        else:
            messagebox.showwarning(
                "WebClient",
                "서비스를 켜지 못했습니다 (관리자 승인 거부 또는 서비스 없음).\n"
                "관리자 PowerShell에서: Set-Service WebClient -StartupType Automatic; Start-Service WebClient")

    def _disconnect_drive(self):
        try:
            disconnect_drive(self.cmb_drive.get())
            self.log(f"{self.cmb_drive.get()} 연결을 해제했습니다.")
        except Exception as e:
            messagebox.showerror("연결 해제 실패", str(e))

    def _open_webdav_manager(self, collect: bool = True):
        """일반(범용) WebDAV 서버 연결 관리 창을 연다.
        collect=False 는 로그인 화면에서 열 때(설정 위젯 값 수집 불필요)."""
        if collect:
            self._collect()   # 설정 화면 값 반영 (그래야 관리자에서 cfg.save 시 유실 없음)
        try:
            win = getattr(self, "_webdav_win", None)
            if win is not None and win.winfo_exists():
                win.lift(); win.focus_force()
                return
            self._webdav_win = WebDavManager(self.root, self.cfg, self.log)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("일반 WebDAV", f"창을 열 수 없습니다: {e}")

    def _auto_connect_webdav_mounts(self):
        """저장된 일반 WebDAV 연결 중 'auto' 항목을 시작 시 마운트한다 (백그라운드)."""
        for m in list(self.cfg.webdav_mounts):
            if not m.get("auto"):
                continue
            drive = m.get("drive", "")
            url = m.get("url", "")
            if not drive or not url:
                continue
            try:
                from . import secret
                pw = secret.decrypt(m.get("password_enc", "")) or ""
                connect_url(drive, url, m.get("username", ""), pw)
                self.log(f"[WebDAV] {drive} 자동 연결: {m.get('name') or url}")
            except Exception as e:  # noqa: BLE001
                self.log(f"[WebDAV] 자동 연결 실패({m.get('name') or url}): {e}")

    def _cleanup_webdav(self):
        """끊긴 WebDAV 드라이브/네트워크 위치 잔여를 제거하고, 자동 연결을 끈다."""
        if not messagebox.askyesno(
                "끊긴 WebDAV 연결 정리",
                "탐색기에 남은 끊긴 WebDAV 네트워크 드라이브(빨간 X)와 잔여 항목을 제거합니다.\n"
                "'자동 로그인 후 드라이브 자동 연결'도 함께 끕니다.\n"
                "(genDISK Drive 온디맨드 기능에는 영향 없음)\n\n계속할까요?"):
            return
        try:
            removed = cleanup_stale_webdav(self.cmb_drive.get() or self.cfg.drive_letter,
                                           self.cfg.server_url)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("정리 실패", str(e))
            return
        # 재발 방지: 자동 드라이브 연결 끄기
        self.cfg.auto_connect_drive = False
        self.var_autodrive.set(False)
        self.cfg.save()
        if removed:
            self.log("WebDAV 정리: " + ", ".join(removed))
            messagebox.showinfo(
                "정리 완료",
                "정리했습니다:\n\n· " + "\n· ".join(removed) +
                "\n\n'자동 드라이브 연결'도 껐습니다.\n탐색기를 새로 열면 사라집니다.")
        else:
            messagebox.showinfo("정리", "제거할 끊긴 WebDAV 항목이 없습니다.\n"
                                        "('자동 드라이브 연결'은 껐습니다.)")

    # ---------- 상태/로그 (스레드 안전) ----------
    def set_status(self, text, color=MUTED):
        self.root.after(0, lambda: self.lbl_status.configure(text=text, text_color=color))

    def log(self, text):
        def _append():
            self.txt_log.configure(state="normal")
            self.txt_log.insert("end", text + "\n")
            self.txt_log.see("end")
            self.txt_log.configure(state="disabled")
        self.root.after(0, _append)

    def run(self):
        self.root.mainloop()


def main(startup: bool = False):
    App(startup=startup).run()
