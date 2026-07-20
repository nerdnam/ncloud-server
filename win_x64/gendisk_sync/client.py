"""genDISK 서버와 통신하는 HTTP 클라이언트 (표준 라이브러리만 사용, 외부 의존성 없음).

로그인은 세션 쿠키를 발급하는데, 그 쿠키 값이 곧 세션 토큰이다. 값을 추출해
이후 요청에 Authorization: Bearer 로 실어 보낸다 (서버가 쿠키/Bearer 둘 다 허용).
"""
import gzip
import http.client
import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# 청크(분할) 업로드: 이보다 큰 파일은 조각으로 나눠 올려 앞단(Cloudflare 100MB 등)의
# 요청당 크기 제한·단일 요청 타임아웃을 우회하고, 파일 전체를 메모리에 올리지 않는다.
CHUNK_THRESHOLD = 48 * 1024 * 1024
CHUNK_SIZE = 16 * 1024 * 1024

# 기본 urllib UA(Python-urllib/x)는 Cloudflare 등 WAF가 봇으로 보고 차단(error 1010)한다.
# 브라우저 형태 + 앱 식별자를 함께 보내 정상 클라이언트로 인식되게 한다.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) gendisk-sync/0.1.0"
)


class AuthError(Exception):
    """세션 만료·인증 실패 (재로그인 필요)."""


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"[{status}] {message}")
        self.status = status
        self.message = message


def webdav_preflight(server_url: str, username: str, password: str):
    """드라이브 마운트 전에 서버의 /dav 를 직접 확인한다 (WebClient 없이).
    서버 측 문제(WebDAV 미제공·Cloudflare 차단·인증 실패)면 명확한 메시지로 예외를 던지고,
    정상(207)이면 조용히 통과한다 → 이후 마운트가 실패하면 로컬 WebClient 문제로 좁혀진다."""
    import base64

    url = server_url.rstrip("/") + "/dav/"
    cred = base64.b64encode(f"{username}:{password}".encode()).decode()
    req = urllib.request.Request(url, method="PROPFIND", headers={
        "Authorization": "Basic " + cred,
        "Depth": "0",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/xml",
    })
    try:
        urllib.request.urlopen(req, timeout=15).read()
        return  # 207 등 성공 → 서버 정상
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        cf = _blocked_by_cloudflare(e.code, raw)
        if cf:
            raise RuntimeError("서버의 /dav 접근이 차단됐습니다.\n" + cf)
        if e.code in (404, 405, 501):
            raise RuntimeError(
                "이 서버는 WebDAV(/dav)를 제공하지 않습니다.\n"
                "서버를 WebDAV가 포함된 최신 버전(v0.0.8 이상)으로 업데이트하세요.")
        if e.code == 401:
            raise RuntimeError("WebDAV 인증에 실패했습니다 — 아이디/비밀번호를 확인하세요.")
        raise RuntimeError(f"서버 WebDAV 응답 오류 (HTTP {e.code}).")
    except urllib.error.URLError as e:
        raise RuntimeError(f"서버에 연결할 수 없습니다: {e.reason}")


def webdav_preflight_url(webdav_url: str, username: str, password: str):
    """임의의 WebDAV 주소로 PROPFIND(Depth 0)를 보내 연결 가능성을 확인한다.
    genDISK 전용 `webdav_preflight` 와 달리 경로를 가정하지 않고 준 URL 그대로 검사한다.
    실패 시 사람이 읽을 수 있는 RuntimeError, 성공(2xx/207)이면 조용히 통과."""
    import base64

    if urllib.parse.urlsplit(webdav_url).scheme != "https":
        # http(비암호화)로는 Basic 자격증명이 평문(가역 base64)으로 새어나간다.
        # 확인 요청 자체를 보내지 않고 즉시 안내한다. (Windows도 http Basic 인증을 기본 차단)
        raise RuntimeError(
            "보안상 http(암호화 안 됨) 주소로는 자격증명 확인을 보내지 않습니다.\n"
            "https 주소를 사용하세요.")
    url = webdav_url.rstrip("/") + "/"
    cred = base64.b64encode(f"{username}:{password}".encode()).decode()
    req = urllib.request.Request(url, method="PROPFIND", headers={
        "Authorization": "Basic " + cred,
        "Depth": "0",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/xml",
    })
    try:
        urllib.request.urlopen(req, timeout=15).read()
        return
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        cf = _blocked_by_cloudflare(e.code, raw)
        if cf:
            raise RuntimeError("이 주소 접근이 차단됐습니다.\n" + cf)
        if e.code in (404, 405, 501):
            raise RuntimeError(
                f"이 주소는 WebDAV를 제공하지 않는 것 같습니다 (HTTP {e.code}).\n"
                "주소와 경로를 다시 확인하세요.")
        if e.code == 401:
            raise RuntimeError("인증 실패 — 아이디/비밀번호를 확인하세요.")
        if e.code == 403:
            raise RuntimeError("접근이 거부됐습니다 (HTTP 403) — 권한/경로를 확인하세요.")
        raise RuntimeError(f"WebDAV 응답 오류 (HTTP {e.code}).")
    except urllib.error.URLError as e:
        raise RuntimeError(f"서버에 연결할 수 없습니다: {e.reason}")


def _blocked_by_cloudflare(status: int, body: str) -> str | None:
    """Cloudflare/WAF 차단이면 사용자에게 도움이 되는 안내 메시지를 만든다."""
    low = body.lower()
    if "error code: 1010" in low or "cloudflare" in low and ("cf-ray" in low or "attention required" in low):
        return (
            "Cloudflare가 이 연결을 차단했습니다 (error 1010).\n\n"
            "서버 앞단의 Cloudflare가 이 앱을 봇으로 보고 막은 것입니다. "
            "서버 관리자가 Cloudflare에서 다음 중 하나를 설정해야 합니다:\n"
            " · Bot Fight Mode를 끄거나\n"
            " · /api/* 와 /dav/* 경로에 WAF 예외(Skip) 규칙을 추가"
        )
    return None


class EventStream:
    """서버의 실시간 변경 스트림(SSE, text/event-stream) 한 연결.

    반복(for)하면 파싱된 이벤트(dict, 예: {"space","dir"})를 하나씩 내놓는다.
    스트림이 끊기거나 EOF 면 조용히 반복이 끝난다(→ 호출측이 재연결). 다른 스레드에서
    close() 를 부르면 블록된 읽기가 풀려 반복이 끝난다(종료용). 전용 연결이라 keep-alive
    풀과 섞이지 않는다."""

    def __init__(self, conn, resp):
        self._conn = conn
        self._resp = resp

    def __iter__(self):
        data_lines: list[str] = []
        while True:
            try:
                raw = self._resp.readline()      # HTTPResponse: 청크 해제된 한 줄(개행 포함)
            except Exception:
                return                            # 소켓 오류/닫힘 → 반복 종료(재연결은 호출측)
            if not raw:
                return                            # EOF
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line == "":                        # 빈 줄 = 이벤트 경계
                if data_lines:
                    payload = "\n".join(data_lines)
                    data_lines = []
                    try:
                        yield json.loads(payload)
                    except Exception:
                        pass                      # 파싱 불가한 이벤트는 무시
                continue
            if line.startswith(":"):              # 주석(핑) — 무시
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
            # event:/id:/retry: 등 다른 필드는 이 클라이언트에선 쓰지 않는다

    def close(self):
        for x in (self._resp, self._conn):
            try:
                x.close()
            except Exception:
                pass


class GenDiskClient:
    def __init__(self, base_url: str, token: str | None = None, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        u = urllib.parse.urlsplit(self.base_url)
        self._scheme = (u.scheme or "https").lower()
        self._host = u.hostname or ""
        self._port = u.port
        self._prefix = (u.path or "").rstrip("/")   # base_url 에 경로 접두어가 있으면 유지
        # keep-alive 연결을 스레드별로 보관해 재사용한다. CfAPI 콜백은 여러 스레드에서 동시에
        # 오므로, 스레드마다 자기 연결을 써 서로 막지 않게 한다(단일 공유 연결의 직렬화 회피).
        self._local = threading.local()

    # ---------- 저수준 요청 (스레드별 keep-alive 연결 재사용) ----------
    def _new_conn(self, timeout: int | None = None):
        to = timeout or self.timeout
        if self._scheme == "https":
            return http.client.HTTPSConnection(
                self._host, self._port or 443, timeout=to,
                context=ssl.create_default_context())
        return http.client.HTTPConnection(self._host, self._port or 80, timeout=to)

    def _drop_conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    def close(self):
        """이 스레드의 keep-alive 연결을 닫는다(best-effort)."""
        self._drop_conn()

    def _request(self, method: str, path: str, *, params=None, json_body=None,
                 data: bytes | None = None, content_type: str | None = None,
                 extra_headers: dict | None = None, gzip_ok: bool = True):
        """요청 후 (status, headers(소문자키 dict), body(bytes)) 반환. 4xx/5xx 는 기존처럼
        ApiError/AuthError 로 올린다. 스레드별 keep-alive 연결을 재사용하고, 끊긴 소켓이면
        새 연결로 1회 재시도한다. gzip_ok=False 면 파일 다운로드/Range 처럼 압축을 피한다."""
        full = self._prefix + path
        if params:
            full += "?" + urllib.parse.urlencode(params)
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if gzip_ok:
            headers["Accept-Encoding"] = "gzip"
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if extra_headers:
            headers.update(extra_headers)
        body = data
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif content_type:
            headers["Content-Type"] = content_type

        status = raw = hdrs = None
        for attempt in (1, 2):
            conn = getattr(self._local, "conn", None)
            if conn is None:
                conn = self._new_conn()
                self._local.conn = conn
            try:
                conn.request(method, full, body=body, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()                # 연결 재사용 위해 본문을 전부 소비
                status = resp.status
                hdrs = {k.lower(): v for k, v in resp.getheaders()}
                break
            except (http.client.HTTPException, OSError):
                self._drop_conn()                # 만료/리셋된 keep-alive 소켓 → 새 연결로 재시도
                if attempt == 2:
                    raise

        if raw and hdrs.get("content-encoding", "").lower() == "gzip":
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
        if status >= 400:
            text = raw.decode("utf-8", "replace")
            cf = _blocked_by_cloudflare(status, text)
            if cf:
                raise ApiError(status, cf)
            detail = text
            try:
                detail = json.loads(text).get("detail", text)
            except Exception:
                pass
            if status == 401:
                raise AuthError(detail)
            raise ApiError(status, detail)
        return status, hdrs, raw

    def _json(self, method, path, **kw):
        _status, _hdrs, raw = self._request(method, path, **kw)
        text = raw.decode("utf-8")
        return json.loads(text) if text else {}

    # ---------- 인증 ----------
    def login(self, username: str, password: str) -> str:
        """로그인해 세션 토큰을 얻는다. 성공 시 self.token 설정 후 토큰 반환."""
        url = self.base_url + "/api/auth/login"
        body = json.dumps({"username": username, "password": password}).encode()
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": USER_AGENT, "Accept": "*/*"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            cf = _blocked_by_cloudflare(e.code, raw)
            if cf:
                raise AuthError(cf)
            detail = raw
            try:
                detail = json.loads(raw).get("detail", raw)
            except Exception:
                pass
            raise AuthError(detail)
        except urllib.error.URLError as e:
            raise AuthError(f"서버에 연결할 수 없습니다: {e.reason}")
        # Set-Cookie: ncloud_session=<token>; ... 에서 토큰 추출
        token = None
        for key, value in resp.getheaders():
            if key.lower() == "set-cookie" and value.startswith("ncloud_session="):
                token = value.split(";", 1)[0].split("=", 1)[1]
                break
        if not token:
            raise AuthError("세션 토큰을 받지 못했습니다")
        self.token = token
        return token

    def status(self) -> dict:
        return self._json("GET", "/api/auth/status")

    # ---------- 저장소 ----------
    def spaces(self) -> list[dict]:
        return self._json("GET", "/api/files/spaces")["spaces"]

    def usage(self) -> dict:
        return self._json("GET", "/api/files/usage")

    # ---------- 동기화 ----------
    def open_events(self, last_event_id: str | None = None) -> "EventStream":
        """서버 변경 이벤트 스트림(/api/sync/events, SSE)을 연다. 무기한 열려 있으므로
        스레드로컬 keep-alive 풀과 분리된 '전용' 연결을 쓴다. 반환된 EventStream 을
        반복하면 {space, dir} 이벤트를 받는다. 서버는 25초마다 핑을 보내므로 그보다
        넉넉한 소켓 타임아웃(90s)으로 죽은 연결을 감지한다.

        401 → AuthError(재로그인 필요). 그 외 비정상 응답(미지원 404/405/501 포함) → ApiError."""
        conn = self._new_conn(timeout=90)
        path = self._prefix + "/api/sync/events"
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
        except (http.client.HTTPException, OSError) as e:
            try:
                conn.close()
            except Exception:
                pass
            raise ApiError(0, f"이벤트 스트림 연결 실패: {e}")
        if resp.status == 401:
            try:
                conn.close()
            except Exception:
                pass
            raise AuthError("이벤트 스트림 인증 실패")
        if resp.status != 200:
            try:
                body = resp.read().decode("utf-8", "replace")[:200]
            except Exception:
                body = ""
            try:
                conn.close()
            except Exception:
                pass
            raise ApiError(resp.status, body or "이벤트 스트림 오류")
        ctype = (resp.getheader("Content-Type") or "").lower()
        if "text/event-stream" not in ctype:
            # 200 이지만 SSE 가 아님(프록시/Cloudflare 챌린지 페이지 등) — 스트림으로 오인해
            # 즉시 EOF → 1초 재연결 스핀에 빠지지 않게 오류로 올린다(호출측이 백오프).
            try:
                conn.close()
            except Exception:
                pass
            raise ApiError(resp.status, f"이벤트 스트림 아님 (Content-Type: {ctype or '없음'})")
        return EventStream(conn, resp)

    def enumerate(self, space: str, path: str = "") -> dict:
        return self._json("GET", "/api/sync/enumerate",
                          params={"space": space, "path": path})

    def sync_info(self) -> dict:
        """서버 동기화 기능 정보: {features:[...], ...}. delta/events 지원 여부 판별용."""
        return self._json("GET", "/api/sync/info")

    def sync_delta(self, space: str, cursor: str) -> dict:
        """커서(서버 시각 ns) 이후 생성·수정된 항목만 받는다: {cursor, changed:[{path,...}]}.
        주의: cursor="0" 은 서버가 전체 파일을 해시하는 풀워크라 매우 비싸다 — 쓰지 말 것.
        삭제는 포함되지 않는다(폴더 대조로 확인)."""
        return self._json("GET", "/api/sync/delta",
                          params={"space": space, "cursor": cursor})

    def download(self, space: str, path: str) -> bytes:
        _s, _h, raw = self._request("GET", "/api/files/download",
                                    params={"space": space, "path": path}, gzip_ok=False)
        return raw

    def download_range(self, space: str, path: str, offset: int, length: int) -> bytes:
        """[offset, offset+length) 바이트만 받는다 (온디맨드 하이드레이션용).
        서버는 Range 를 지원해 206 을 준다. 서버가 Range 를 무시하고 200 을 주면
        받은 전체에서 필요한 구간을 잘라 반환한다(안전장치). gzip_ok=False: 파일 바이트는 압축 안 함."""
        end = offset + length - 1
        status, _h, data = self._request(
            "GET", "/api/files/download",
            params={"space": space, "path": path},
            extra_headers={"Range": f"bytes={offset}-{end}"}, gzip_ok=False)
        if status == 200 and (offset or length < len(data)):
            data = data[offset:offset + length]
        return data

    def put(self, space: str, path: str, data: bytes) -> dict:
        return self._json("POST", "/api/sync/put",
                         params={"space": space, "path": path},
                         data=data, content_type="application/octet-stream")

    # ---------- 청크(분할) 업로드 ----------
    def put_smart(self, space: str, path: str, local_path, progress=None) -> None:
        """크기에 따라 업로드 방식을 고른다: 큰 파일은 조각으로 나눠(디스크에서 스트리밍)
        올려 앞단 제한·타임아웃을 우회하고 메모리도 아낀다. 작은 파일은 기존 한 방 업로드.
        같은 경로를 원자적으로 덮어써(overwrite) 동기화 재시도 멱등성을 지킨다.
        progress(done_bytes, total_bytes) 가 있으면 조각마다 호출해 진행률을 보고한다."""
        from pathlib import Path
        p = Path(local_path)
        size = p.stat().st_size
        if size <= CHUNK_THRESHOLD:
            self.put(space, path, p.read_bytes())
            if progress:
                progress(size, size)
            return
        upload_id = self._upload_init(space, path, size)
        with p.open("rb") as f:
            offset = 0
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                self._upload_chunk(upload_id, offset, chunk)
                offset += len(chunk)
                if progress:
                    progress(offset, size)
        self._upload_complete(upload_id)

    def _upload_init(self, space: str, path: str, size: int) -> str:
        # path="" + rel=<정확한 상대경로>, overwrite=True → 서버가 그 경로를 원자적으로 덮어씀
        res = self._json("POST", "/api/files/upload/init",
                        json_body={"space": space, "path": "", "rel": path,
                                   "size": int(size), "overwrite": True})
        return res["upload_id"]

    def _upload_status(self, upload_id: str) -> int:
        return self._json("GET", "/api/files/upload/status",
                         params={"upload_id": upload_id})["received"]

    def _upload_chunk(self, upload_id: str, offset: int, chunk: bytes) -> None:
        attempt = 0
        while True:
            try:
                self._json("PUT", "/api/files/upload/chunk",
                          params={"upload_id": upload_id, "offset": str(offset)},
                          data=chunk, content_type="application/octet-stream")
                return
            except ApiError as e:
                if e.status == 409:
                    # offset 불일치 → 서버가 실제로 받은 지점 확인 후 판단
                    cur = self._upload_status(upload_id)
                    if cur == offset + len(chunk):
                        return                      # 이 조각은 이미 반영됨
                    if cur != offset:
                        raise                       # 재동기화 불가
                    # cur == offset 이면 아래로 떨어져 재전송
                else:
                    raise
            except OSError:                         # 네트워크 오류(URLError 포함) → 백오프 재시도
                attempt += 1
                if attempt > 4:
                    raise
                time.sleep(0.5 * attempt)

    def _upload_complete(self, upload_id: str) -> dict:
        return self._json("POST", "/api/files/upload/complete",
                         params={"upload_id": upload_id})

    def list_dir(self, space: str, path: str = "") -> list[dict]:
        """폴더의 직속 항목 목록. [{name, path, is_dir, size, ...}] (온디맨드 채우기용)."""
        return self._json("GET", "/api/files/list",
                          params={"space": space, "path": path}).get("entries", [])

    def mkdir(self, space: str, path: str):
        try:
            self._json("POST", "/api/files/mkdir",
                      json_body={"path": path, "space": space})
        except ApiError as e:
            if e.status != 409:  # 이미 존재하면 무시
                raise

    def delete(self, space: str, path: str):
        try:
            self._json("POST", "/api/files/delete",
                      json_body={"path": path, "space": space})
        except ApiError as e:
            if e.status != 404:  # 이미 없으면 무시
                raise

    def move(self, space: str, src: str, dst: str,
             src_space: str | None = None, dst_space: str | None = None) -> dict:
        """이동/이름변경 (src -> dst). 폴더 간 + (src_space/dst_space 다르면) 저장소 간 이동."""
        body = {"src": src, "dst": dst, "space": space}
        if src_space:
            body["src_space"] = src_space
        if dst_space:
            body["dst_space"] = dst_space
        return self._json("POST", "/api/files/move", json_body=body)
