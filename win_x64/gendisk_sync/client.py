"""genDISK 서버와 통신하는 HTTP 클라이언트 (표준 라이브러리만 사용, 외부 의존성 없음).

로그인은 세션 쿠키를 발급하는데, 그 쿠키 값이 곧 세션 토큰이다. 값을 추출해
이후 요청에 Authorization: Bearer 로 실어 보낸다 (서버가 쿠키/Bearer 둘 다 허용).
"""
import json
import urllib.error
import urllib.parse
import urllib.request

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


class GenDiskClient:
    def __init__(self, base_url: str, token: str | None = None, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    # ---------- 저수준 요청 ----------
    def _request(self, method: str, path: str, *, params=None, json_body=None,
                 data: bytes | None = None, content_type: str | None = None):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        body = data
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
            return resp
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            cf = _blocked_by_cloudflare(e.code, raw)
            if cf:
                raise ApiError(e.code, cf)
            detail = raw
            try:
                detail = json.loads(raw).get("detail", raw)
            except Exception:
                pass
            if e.code == 401:
                raise AuthError(detail)
            raise ApiError(e.code, detail)

    def _json(self, method, path, **kw):
        resp = self._request(method, path, **kw)
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}

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
    def enumerate(self, space: str, path: str = "") -> dict:
        return self._json("GET", "/api/sync/enumerate",
                          params={"space": space, "path": path})

    def download(self, space: str, path: str) -> bytes:
        resp = self._request("GET", "/api/files/download",
                            params={"space": space, "path": path})
        return resp.read()

    def put(self, space: str, path: str, data: bytes) -> dict:
        return self._json("POST", "/api/sync/put",
                         params={"space": space, "path": path},
                         data=data, content_type="application/octet-stream")

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
