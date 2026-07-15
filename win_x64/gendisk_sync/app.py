"""gendisk-sync GUI: 로그인 · 폴더 동기화 · 드라이브 연결 · 시작 옵션.

customtkinter로 macOS 스타일(둥근 카드·토글 스위치·플랫 강조 버튼·시스템 다크/라이트)
창을 만들고, 백그라운드 스레드에서 주기적으로 동기화한다. 설정에 따라 시작 시
자동 로그인 → 드라이브 자동 연결 → 자동 동기화까지 수행한다.
"""
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

from . import autostart
from .client import ApiError, AuthError, GenDiskClient, webdav_preflight
from .config import Config
from .engine import SyncEngine
from .webdav_mount import (
    connect_drive, disconnect_drive, start_webclient_elevated, webclient_running)

# macOS 시스템 강조색 (라이트/다크). accent 버튼에 사용.
ACCENT = ("#007AFF", "#0A84FF")
ACCENT_HOVER = ("#0063CC", "#3D9BFF")
SUCCESS = ("#1C8A3B", "#30D158")
DANGER = ("#C7362F", "#FF453A")
MUTED = ("gray45", "gray60")

# 앱 시작 시 한 번만: 시스템 테마를 따라가고, macOS풍 파란 강조 테마 사용.
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
        self.root = ctk.CTk()
        self.root.title("genDISK")
        self.root.geometry("560x820")
        self.root.minsize(520, 640)
        self._build_ui()
        self.worker = SyncWorker(self)
        self.worker.start()
        self.tray = None
        self._tray_notified = False
        self._build_tray()
        # 닫기(X)는 트레이가 있으면 트레이로 숨기고, 없으면 그냥 종료
        self.root.protocol("WM_DELETE_WINDOW",
                           self._hide_to_tray if self.tray else self._real_quit)
        if startup:
            # 자동 시작이면 트레이만 남기고 창은 숨김 (트레이 없으면 최소화)
            (self._hide_to_tray if self.tray else self.root.iconify)()
        # 시작 시 자동 로그인 → (설정 시) 드라이브 연결 → 동기화 트리거
        if self.cfg.auto_login and self.cfg.username and self.cfg.get_password():
            threading.Thread(target=self._auto_sequence, daemon=True).start()

    # ---------- 시스템 트레이 ----------
    def _build_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            return  # pystray 없으면 트레이 비활성 (닫기 = 종료)
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse([4, 4, 60, 60], fill=(0, 122, 255, 255))
        d.ellipse([20, 22, 44, 46], fill=(255, 255, 255, 255))  # 간단한 디스크 모양
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
        self.root.after(0, lambda: (self.root.deiconify(), self.root.lift()))

    def _tray_quit(self, icon=None, item=None):
        self.root.after(0, self._real_quit)

    def _real_quit(self):
        self._collect(); self.cfg.save()
        self.worker.stop()
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()

    # ---------- UI ----------
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

    def _build_ui(self):
        self.font_title = ctk.CTkFont(family="Segoe UI", size=22, weight="bold")
        self.font_h = ctk.CTkFont(family="Segoe UI", size=14, weight="bold")
        self.font_s = ctk.CTkFont(family="Segoe UI", size=12)
        self.font_mono = ctk.CTkFont(family="Consolas", size=12)

        # 헤더
        header = ctk.CTkFrame(self.root, fg_color="transparent")
        header.pack(fill="x", padx=22, pady=(18, 6))
        ctk.CTkLabel(header, text="☁  genDISK", font=self.font_title).pack(side="left")
        ctk.CTkLabel(header, text="동기화 · 드라이브 연결", font=self.font_s,
                     text_color=MUTED).pack(side="left", padx=(12, 0), pady=(8, 0))

        # 스크롤 가능한 본문
        body = ctk.CTkScrollableFrame(self.root, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=(0, 16))

        # ── 연결 ──
        c = self._card(body, "연결")
        self._field_label(c, "서버 주소").pack(fill="x")
        self.e_url = ctk.CTkEntry(c, placeholder_text="https://cloud.example.com")
        self.e_url.pack(fill="x", pady=(2, 10)); self.e_url.insert(0, self.cfg.server_url)

        row = ctk.CTkFrame(c, fg_color="transparent"); row.pack(fill="x")
        left = ctk.CTkFrame(row, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)
        self._field_label(left, "아이디").pack(fill="x")
        self.e_user = ctk.CTkEntry(left, placeholder_text="아이디")
        self.e_user.pack(fill="x", pady=(2, 0)); self.e_user.insert(0, self.cfg.username)
        right = ctk.CTkFrame(row, fg_color="transparent")
        right.pack(side="left", fill="x", expand=True, padx=(10, 0))
        self._field_label(right, "비밀번호").pack(fill="x")
        self.e_pw = ctk.CTkEntry(right, show="•", placeholder_text="비밀번호")
        self.e_pw.pack(fill="x", pady=(2, 0))
        if self.cfg.get_password():
            self.e_pw.insert(0, self.cfg.get_password())

        self.btn_login = ctk.CTkButton(c, text="로그인", command=self._login,
                                       fg_color=ACCENT, hover_color=ACCENT_HOVER)
        self.btn_login.pack(fill="x", pady=(12, 4))
        self.lbl_login = ctk.CTkLabel(c, text=self._login_state_text(), font=self.font_s,
                                      text_color=MUTED, anchor="w")
        self.lbl_login.pack(fill="x")

        # ── 시작 옵션 ──
        c = self._card(body, "시작 옵션")
        self.var_savecred = tk.BooleanVar(value=self.cfg.save_credentials)
        self.var_autostart = tk.BooleanVar(value=self.cfg.auto_start)
        self.var_autologin = tk.BooleanVar(value=self.cfg.auto_login)
        self.var_autodrive = tk.BooleanVar(value=self.cfg.auto_connect_drive)
        ctk.CTkSwitch(c, text="로그인 정보 저장 (암호화)", variable=self.var_savecred).pack(
            anchor="w", pady=6)
        ctk.CTkSwitch(c, text="Windows 시작 시 자동 실행", variable=self.var_autostart,
                      command=self._apply_autostart).pack(anchor="w", pady=6)
        ctk.CTkSwitch(c, text="프로그램 시작 시 자동 로그인", variable=self.var_autologin).pack(
            anchor="w", pady=6)
        ctk.CTkSwitch(c, text="자동 로그인 후 드라이브 자동 연결", variable=self.var_autodrive).pack(
            anchor="w", pady=6)

        # ── 동기화 ──
        c = self._card(body, "동기화")
        self._field_label(c, "동기화할 저장소").pack(fill="x")
        self.cmb_space = ctk.CTkOptionMenu(c, values=["home"])
        self.cmb_space.set(self.cfg.space); self.cmb_space.pack(fill="x", pady=(2, 10))

        self._field_label(c, "로컬 폴더").pack(fill="x")
        frow = ctk.CTkFrame(c, fg_color="transparent"); frow.pack(fill="x", pady=(2, 10))
        self.e_folder = ctk.CTkEntry(frow, placeholder_text="C:\\Users\\…")
        self.e_folder.pack(side="left", fill="x", expand=True)
        self.e_folder.insert(0, self.cfg.local_folder)
        ctk.CTkButton(frow, text="찾아보기", width=90, command=self._pick_folder).pack(
            side="left", padx=(8, 0))

        irow = ctk.CTkFrame(c, fg_color="transparent"); irow.pack(fill="x")
        self._field_label(irow, "동기화 주기(초)").pack(side="left", pady=(0, 0))
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

        # ── 드라이브 연결 ──
        c = self._card(body, "드라이브 연결 (WebDAV)")
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

        # ── 상태 & 로그 ──
        c = self._card(body, "상태")
        self.lbl_status = ctk.CTkLabel(c, text="대기 중", font=self.font_s,
                                       text_color=MUTED, anchor="w")
        self.lbl_status.pack(fill="x", pady=(0, 6))
        self.txt_log = ctk.CTkTextbox(c, height=150, wrap="word", font=self.font_mono)
        self.txt_log.pack(fill="both", expand=True)
        self.txt_log.configure(state="disabled")

    def _login_state_text(self):
        return f"로그인됨: {self.cfg.username}" if self.cfg.token else "로그인 필요"

    # ---------- 동작 ----------
    def _login(self):
        self._collect()
        url, user, pw = self.e_url.get().strip(), self.e_user.get().strip(), self.e_pw.get()
        if not url or not user or not pw:
            messagebox.showwarning("입력 필요", "서버 주소·아이디·비밀번호를 모두 입력하세요.")
            return
        try:
            c = GenDiskClient(url)
            c.login(user, pw)
            self.cfg.server_url, self.cfg.username, self.cfg.token = url, user, c.token
            if self.cfg.save_credentials:
                self.cfg.set_password(pw)
            else:
                self.cfg.clear_password()
            self.cfg.save()
            self.lbl_login.configure(text=self._login_state_text(), text_color=SUCCESS)
            self._refresh_spaces(c)
            self.log("로그인 성공")
        except AuthError as e:
            messagebox.showerror("로그인 실패", str(e))
        except (ApiError, OSError) as e:
            messagebox.showerror("연결 오류", str(e))

    def _auto_sequence(self):
        """시작 시: 자동 로그인 → (설정 시) 드라이브 연결 → 동기화 트리거."""
        cfg = self.cfg
        pw = cfg.get_password()
        try:
            c = GenDiskClient(cfg.server_url)
            c.login(cfg.username, pw)
            cfg.token = c.token
            cfg.save()
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
        if cfg.enabled:
            self.worker.sync_now()

    def try_relogin(self):
        """세션 만료 시 저장된 정보로 조용히 재로그인."""
        pw = self.cfg.get_password()
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

    def _refresh_spaces(self, client):
        try:
            spaces = [s["id"] for s in client.spaces()]
            self.cmb_space.configure(values=spaces or ["home"])
            if self.cfg.space not in spaces:
                self.cmb_space.set(spaces[0] if spaces else "home")
        except Exception:
            pass

    def _pick_folder(self):
        d = filedialog.askdirectory()
        if d:
            self.e_folder.delete(0, "end"); self.e_folder.insert(0, d)

    def _apply_autostart(self):
        try:
            autostart.sync(self.var_autostart.get())
        except OSError as e:
            messagebox.showerror("자동 실행 등록 실패", str(e))

    def _collect(self):
        self.cfg.server_url = self.e_url.get().strip()
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
        elif self.e_pw.get():
            self.cfg.set_password(self.e_pw.get())

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
        pw = self.e_pw.get() or self.cfg.get_password()
        if not self.cfg.server_url or not self.cfg.username or not pw:
            messagebox.showwarning("정보 필요", "서버 주소·아이디·비밀번호가 필요합니다.")
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
