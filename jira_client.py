"""Jira Cloud REST API 읽기 전용 클라이언트 (질문한 사람의 Jira 카드를 읽는 데만 쓴다. sprint_tasks.py가 사용).
- 인증: 봇 전용 계정의 이메일 + API 토큰 (config.JIRA_*). 쓰기 요청은 이 파일에 없다.
- 호출 주소: 범위 지정(scoped) 토큰과 서비스 계정은 https://api.atlassian.com/ex/jira/<Cloud ID>로, 범위 없는 일반 토큰은 사이트 주소로 호출한다.
  JIRA_CLOUD_ID가 있으면 앞의 것을 쓴다. 잘못 고르면 401이 난다.
- 인증 방식: 이메일:토큰(Basic)을 먼저 쓰고, Cloud ID 주소에서 401이면 Bearer(토큰만)로 한 번 다시 해 본다. 서비스 계정 토큰은 Atlassian 문서마다
  Bearer를 쓰는 예와 Basic을 쓰는 예가 있어서, 되는 쪽을 자동으로 고르고 기억한다.
- 검색은 새 주소(/rest/api/3/search/jql)만 쓴다. 예전 /rest/api/3/search는 Jira Cloud에서 제거됐다 (410 Gone).
  새 검색은 한 번에 100개씩이고 nextPageToken으로 다음 쪽을 받는다. fields를 주지 않으면 이슈 ID만 온다.
- 오류는 JiraError로 올린다. 메세지가 그대로 사용자에게 보여도 되는 문장이다 (토큰 같은 비밀은 넣지 않는다).
"""
import logging
import time
from typing import Dict, List, Optional
import requests
import config

logger = logging.getLogger(__name__)

TIMEOUT = 15
PAGE_SIZE = 100
MAX_ISSUES = 2000
FIELDS_CACHE_SECONDS = 3600
SPRINT_FIELD_TYPE = "com.pyxis.greenhopper.jira:gh-sprint"


class JiraError(Exception):
    """사용자에게 이유를 안내할 수 있는 Jira 오류. status는 Jira가 돌려준 HTTP 상태 코드 (연결 실패 등은 None)"""
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def configured() -> bool:
    return bool(config.JIRA_BASE_URL and config.JIRA_EMAIL and config.JIRA_API_TOKEN)


def api_base() -> str:
    return f"https://api.atlassian.com/ex/jira/{config.JIRA_CLOUD_ID}" if config.JIRA_CLOUD_ID else config.JIRA_BASE_URL


def tenant_cloud_id() -> str:
    """사이트의 Cloud ID. 로그인 없이 읽을 수 있는 공개 주소에서 가져온다. 못 읽으면 빈 문자열 (범위 지정 토큰을 쓸 때 JIRA_CLOUD_ID에 넣는다)"""
    try:
        return str(requests.get(f"{config.JIRA_BASE_URL}/_edge/tenant_info", timeout=TIMEOUT).json().get("cloudId") or "")
    except (requests.RequestException, ValueError):
        return ""


# 호출에 성공한 인증 방식 ("basic" | "bearer"). 한 번 정해지면 계속 쓴다
_auth = {"mode": "basic"}


def auth_mode() -> str:
    return _auth["mode"]


def send(method: str, path: str, params: Optional[Dict], body: Optional[Dict], mode: str):
    headers = {"Accept": "application/json"}
    credentials = {}
    if mode == "bearer":
        headers["Authorization"] = f"Bearer {config.JIRA_API_TOKEN}"
    else:
        credentials["auth"] = (config.JIRA_EMAIL, config.JIRA_API_TOKEN)
    try:
        return requests.request(method, api_base() + path, params=params, json=body, timeout=TIMEOUT, headers=headers, **credentials)
    except requests.RequestException as e:
        raise JiraError(f"Jira에 연결하지 못했어요 ({type(e).__name__}).") from e


def error_text(res) -> str:
    """오류 응답에 들어 있는 설명문 (없으면 빈 문자열)"""
    try:
        data = res.json()
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    return " ".join(str(x) for x in [data.get("message"), *(data.get("errorMessages") or [])] if x)


def request(method: str, path: str, params: Optional[Dict] = None, body: Optional[Dict] = None):
    if not configured():
        raise JiraError("Jira 연동이 설정되지 않았어요 (JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN).")
    res = send(method, path, params, body, _auth["mode"])
    if res.status_code == 401 and config.JIRA_CLOUD_ID:
        # Cloud ID 주소(범위 지정 토큰·서비스 계정)에서만 다른 인증 방식을 한 번 더 해 본다
        other = "bearer" if _auth["mode"] == "basic" else "basic"
        retry = send(method, path, params, body, other)
        if retry.status_code != 401:
            logger.info("🔑 [Jira] %s 인증 방식으로 바꿉니다.", other)
            res, _auth["mode"] = retry, other
    if res.status_code in (401, 403) and "scope" in error_text(res).lower():
        # 범위가 부족하면 Atlassian은 401 'Unauthorized; scope does not match'를 돌려준다. 토큰이 틀린 것과 구분해서 알려준다
        raise JiraError(f"토큰에 이 호출에 필요한 범위(scope)가 없어요 ({path}). 토큰을 새로 만들 때 read:jira-work, read:jira-user 범위를 고르세요 "
                        "(범위는 만든 뒤에 바꿀 수 없어요).")
    if res.status_code == 401:
        raise JiraError("Jira 인증에 실패했어요. 봇 계정의 API 토큰이 만료됐거나 잘못됐을 수 있어요. "
                        "범위 지정(scoped) 토큰이나 서비스 계정이라면 JIRA_CLOUD_ID가 필요해요.")
    if res.status_code == 403:
        raise JiraError(f"Jira 권한이 부족해요 ({path}). 봇 계정이 해당 space를 볼 수 있어야 하고, 토큰에 읽기 범위가 있어야 해요.")
    if res.status_code == 429:
        raise JiraError("Jira 호출 제한에 걸렸어요. 잠시 후 다시 물어봐 주세요.")
    if res.status_code >= 400:
        logger.warning("⚠️ [Jira] %s %s → HTTP %d", method, path, res.status_code)
        raise JiraError(f"Jira 요청이 실패했어요 (HTTP {res.status_code}).", status=res.status_code)
    return res.json() if res.content else {}


def search(jql: str, fields: List[str], limit: int = MAX_ISSUES) -> List[Dict]:
    """JQL 검색. nextPageToken을 따라 limit개까지 모은다"""
    issues, token = [], None
    while len(issues) < limit:
        body = {"jql": jql, "fields": fields, "maxResults": PAGE_SIZE}
        if token:
            body["nextPageToken"] = token
        data = request("POST", "/rest/api/3/search/jql", body=body)
        issues += data.get("issues", [])
        token = data.get("nextPageToken")
        if not token or data.get("isLast"):
            break
    if len(issues) >= limit:
        logger.warning("⚠️ [Jira] 검색 결과가 %d개를 넘어 일부만 읽었습니다: %s", limit, jql)
    return issues[:limit]


def find_users(query: str) -> List[Dict]:
    """이메일이나 이름으로 사람을 찾는다 (봇 같은 앱 계정은 뺀다)"""
    users = request("GET", "/rest/api/3/user/search", params={"query": query, "maxResults": 20})
    return [u for u in users if u.get("accountType", "atlassian") == "atlassian"]


def visible_projects() -> List[Dict]:
    """봇 계정이 볼 수 있는 space(프로젝트) 목록. 'Browse projects' 권한이 없는 space는 여기에 나오지 않는다"""
    found, start = [], 0
    while True:
        data = request("GET", "/rest/api/3/project/search", params={"startAt": start, "maxResults": 50})
        values = data.get("values", [])
        found += values
        start += len(values)
        if data.get("isLast", True) or not values:
            return found


def myself() -> Dict:
    return request("GET", "/rest/api/3/myself")


# --- [필드 찾기: 이름으로 찾아 ID를 알아낸다] ---
_fields_cache = {"at": 0.0, "value": None}


def now() -> float:
    return time.monotonic()


def discover_fields(force: bool = False) -> Dict:
    """{"participant": [필드 ID…], "approver": [필드 ID…], "sprint": 스프린트 필드 ID}.
    참여자·확인자는 팀이 만든 사용자 필드라 ID를 미리 알 수 없다. space마다 같은 이름의 다른 필드가 있을 수 있어 이름이 같은 필드를 모두 모은다"""
    if not force and _fields_cache["value"] and now() - _fields_cache["at"] < FIELDS_CACHE_SECONDS:
        return _fields_cache["value"]
    fields = request("GET", "/rest/api/3/field")

    def ids_named(name: str) -> List[str]:
        return [f["id"] for f in fields if (f.get("name") or "").strip() == name.strip()]
    value = {"participant": ids_named(config.JIRA_FIELD_PARTICIPANT), "approver": ids_named(config.JIRA_FIELD_APPROVER),
             "sprint": next((f["id"] for f in fields if (f.get("schema") or {}).get("custom") == SPRINT_FIELD_TYPE), None)}
    _fields_cache.update(at=now(), value=value)
    return value
