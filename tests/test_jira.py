import json
import re
import types
import pytest
import requests
import agent
import ai_engine
import asker
import clients
import config
import jira_check
import jira_client
import slack_service
import sprint_tasks

SPRINT = jira_client.SPRINT_FIELD_TYPE
FIELDS = [{"id": "summary", "name": "Summary"},
          {"id": "customfield_10020", "name": "Sprint", "custom": True, "schema": {"custom": SPRINT}},
          {"id": "customfield_10101", "name": "확인자", "custom": True},
          {"id": "customfield_10102", "name": "참여자", "custom": True},
          {"id": "customfield_10201", "name": "확인자", "custom": True},      # 다른 space가 따로 만든 같은 이름의 필드
          {"id": "customfield_10999", "name": "확인자 메모", "custom": True}]
ME, OTHER = "acc-me", "acc-other"


def person(account_id):
    return {"accountId": account_id, "displayName": account_id} if account_id else None


SPACE_NAMES = {"CM": "Content", "CBS": "CBS Team", "KB": "Kanban"}


def issue(key, summary, assignee=None, approver=None, participant=None, reporter=None, done=False, approver2=None, sprint=True, category=None):
    """key의 앞부분이 space 키. sprint=False면 활성 스프린트에 들어 있지 않다 (지난 스프린트만 있다)"""
    category = category or ("done" if done else "indeterminate")
    sprints = [{"name": "CM 1 스프린트 MVP PR 12", "state": "active"}] if sprint else []
    return {"key": key, "fields": {
        "summary": summary, "status": {"name": "완료" if category == "done" else "진행 중" if category == "indeterminate" else "대기",
                                       "statusCategory": {"key": category}},
        "assignee": person(assignee), "reporter": person(reporter), "project": {"key": key.split("-")[0], "name": SPACE_NAMES.get(key.split("-")[0], key.split("-")[0])},
        "customfield_10020": sprints + [{"name": "지난 스프린트", "state": "closed"}],
        "customfield_10101": person(approver), "customfield_10201": person(approver2), "customfield_10102": [person(participant)] if participant else []}}


def holds_role(raw, account, with_custom):
    f = raw["fields"]
    ids = [(f["assignee"] or {}).get("accountId"), (f["reporter"] or {}).get("accountId")]
    if with_custom:
        ids += [(f["customfield_10101"] or {}).get("accountId"), (f["customfield_10201"] or {}).get("accountId")]
        ids += [p["accountId"] for p in f["customfield_10102"]]
    return account in ids


class FakeJira:
    """Jira Cloud를 흉내 낸다: 필드 목록, 페이지로 나눠 오는 검색(nextPageToken), 사용자 검색, space 목록.
    '활성 스프린트' 검색과 '내 카드' 검색(JQL의 계정 ID와 조건으로 거름)을 구분해서 답한다"""
    def __init__(self):
        self.calls, self.issues, self.users = [], [], []
        self.page_size = 2
        self.projects = [{"key": "CM"}, {"key": "CBS"}, {"key": "KB"}, {"key": "AB"}]
        self.search_error = None                      # 사용자 필드 조건이 든 '내 카드' 검색에 줄 오류
        self.ignore_status_filter = False             # True면 '완료되지 않은 카드만'이라는 조건을 무시하고 완료된 카드도 돌려준다

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path, body if body is not None else params))
        if path == "/rest/api/3/field":
            return FIELDS
        if path == "/rest/api/3/project/search":
            start = params["startAt"]
            return {"values": self.projects[start:start + 2], "isLast": start + 2 >= len(self.projects)}
        if path == "/rest/api/3/myself":
            return {"displayName": "Brain Bot"}
        if path == "/rest/api/3/user/search":
            q = params["query"].lower()
            return [u for u in self.users if q in (u.get("emailAddress") or "").lower() or q in u.get("displayName", "").lower()]
        assert path == "/rest/api/3/search/jql"
        jql = body["jql"]
        if jql.startswith("sprint in openSprints()"):
            matching = [i for i in self.issues if any(s["state"] == "active" for s in i["fields"]["customfield_10020"])]
        else:
            with_custom = "cf[" in jql
            if with_custom and self.search_error:
                raise self.search_error
            account = re.search(r'assignee = "([^"]+)"', jql).group(1)
            matching = [i for i in self.issues if (self.ignore_status_filter or i["fields"]["status"]["statusCategory"]["key"] != "done")
                        and holds_role(i, account, with_custom)]
        start = int(body.get("nextPageToken", 0))
        more = start + self.page_size < len(matching)
        return {"issues": matching[start:start + self.page_size], **({"nextPageToken": str(start + self.page_size)} if more else {"isLast": True})}

    def my_queries(self):
        return [c[2]["jql"] for c in self.calls if c[1] == "/rest/api/3/search/jql" and c[2]["jql"].startswith("statusCategory")]

    def sprint_queries(self):
        return [c for c in self.calls if c[1] == "/rest/api/3/search/jql" and c[2]["jql"].startswith("sprint in")]

    def my_query_fields(self):
        return [c[2]["fields"] for c in self.calls if c[1] == "/rest/api/3/search/jql" and c[2]["jql"].startswith("statusCategory")]


@pytest.fixture
def jira(monkeypatch):
    fake = FakeJira()
    clock = types.SimpleNamespace(t=1000.0)
    monkeypatch.setattr(jira_client, "request", fake.request)
    monkeypatch.setattr(jira_client, "now", lambda: clock.t)
    monkeypatch.setattr(jira_client, "_fields_cache", {"at": 0.0, "value": None})
    monkeypatch.setattr(jira_client, "tenant_cloud_id", lambda: "cloud-xyz")
    monkeypatch.setattr(sprint_tasks, "_sprint_keys", {"at": 0.0, "keys": None})
    monkeypatch.setattr(sprint_tasks, "_mine", {})
    monkeypatch.setattr(sprint_tasks, "_accounts", {})
    for name, value in (("JIRA_BASE_URL", "https://southpaw.atlassian.net"), ("JIRA_EMAIL", "bot@company.com"), ("JIRA_API_TOKEN", "secret-token"),
                        ("JIRA_CLOUD_ID", ""), ("JIRA_ACCOUNT_MAP", {}), ("JIRA_CACHE_SECONDS", 300), ("JIRA_SPRINT_CACHE_SECONDS", 1800),
                        ("JIRA_FIELD_APPROVER", "확인자"), ("JIRA_FIELD_PARTICIPANT", "참여자")):
        monkeypatch.setattr(config, name, value)
    fake.clock = clock
    return fake


@pytest.fixture(autouse=True)
def fresh_auth_mode(monkeypatch):
    monkeypatch.setattr(jira_client, "_auth", {"mode": "basic"})


def me():
    return asker.Asker("USLACK", lambda: {"name": "박상우", "email": "me@company.com"})


# ---------------- Jira 클라이언트 ----------------
def test_search_follows_next_page_tokens_and_caps_the_result(jira):
    jira.issues = [issue(f"CM-{i}", "카드") for i in range(5)]
    found = jira_client.search("sprint in openSprints()", ["summary"])
    assert [i["key"] for i in found] == [f"CM-{i}" for i in range(5)]
    assert [c[2].get("nextPageToken") for c in jira.calls if c[1].endswith("/search/jql")] == [None, "2", "4"]
    assert len(jira_client.search("sprint in openSprints()", ["summary"], limit=3)) == 3


def test_fields_are_found_by_name_across_spaces_and_cached(jira):
    fields = jira_client.discover_fields()
    assert fields == {"participant": ["customfield_10102"], "approver": ["customfield_10101", "customfield_10201"], "sprint": "customfield_10020"}
    jira_client.discover_fields()
    assert sum(c[1] == "/rest/api/3/field" for c in jira.calls) == 1                         # 한 시간 동안은 다시 묻지 않는다
    jira.clock.t += jira_client.FIELDS_CACHE_SECONDS + 1
    jira_client.discover_fields()
    assert sum(c[1] == "/rest/api/3/field" for c in jira.calls) == 2


def test_visible_projects_are_paged(jira):
    jira.projects = [{"key": k} for k in ("CM", "AB", "XY", "ZZ", "QQ")]
    assert [p["key"] for p in jira_client.visible_projects()] == ["CM", "AB", "XY", "ZZ", "QQ"]


def test_app_accounts_are_not_users(jira):
    jira.users = [{"accountId": "a", "displayName": "박상우", "accountType": "atlassian"}, {"accountId": "b", "displayName": "박상우 봇", "accountType": "app"}]
    assert [u["accountId"] for u in jira_client.find_users("박상우")] == ["a"]


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code, self.content, self._payload = status, b"x", payload or {}

    def json(self):
        return self._payload


def classic_settings(monkeypatch, cloud_id=""):
    for name, value in (("JIRA_BASE_URL", "https://x.atlassian.net"), ("JIRA_EMAIL", "bot@x.com"), ("JIRA_API_TOKEN", "secret-token"), ("JIRA_CLOUD_ID", cloud_id)):
        monkeypatch.setattr(config, name, value)


@pytest.mark.parametrize("status,expected", [(401, "토큰이 만료"), (403, "권한이 부족"), (429, "호출 제한"), (500, "HTTP 500")])
def test_http_errors_become_messages_a_person_can_act_on(monkeypatch, status, expected):
    classic_settings(monkeypatch)
    monkeypatch.setattr(requests, "request", lambda *a, **k: FakeResponse(status))
    with pytest.raises(jira_client.JiraError) as error:
        jira_client.request("GET", "/rest/api/3/myself")
    assert expected in str(error.value) and "secret-token" not in str(error.value)          # 비밀은 메세지에 들어가지 않는다


def test_errors_carry_the_http_status_so_callers_can_react(monkeypatch):
    classic_settings(monkeypatch)
    monkeypatch.setattr(requests, "request", lambda *a, **k: FakeResponse(400))
    with pytest.raises(jira_client.JiraError) as error:
        jira_client.request("POST", "/rest/api/3/search/jql")
    assert error.value.status == 400 and jira_client.JiraError("연결 실패").status is None


def test_network_failure_and_missing_settings_are_reported(monkeypatch):
    with pytest.raises(jira_client.JiraError, match="설정되지 않았어요"):
        jira_client.request("GET", "/x")
    classic_settings(monkeypatch)
    monkeypatch.setattr(requests, "request", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("boom")))
    with pytest.raises(jira_client.JiraError, match="연결하지 못했어요"):
        jira_client.request("GET", "/x")


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("cloud_id", ["", "cloud-123"])
def test_missing_token_scopes_are_told_apart_from_a_bad_token(monkeypatch, status, cloud_id):
    """범위가 부족하면 Atlassian이 401 'scope does not match'를 돌려준다. '토큰이 만료됐다'는 안내로 헷갈리게 하지 않는다"""
    classic_settings(monkeypatch, cloud_id)
    monkeypatch.setattr(requests, "request", lambda *a, **k: FakeResponse(status, {"message": "Unauthorized; scope does not match"}))
    with pytest.raises(jira_client.JiraError) as error:
        jira_client.request("POST", "/rest/api/3/search/jql")
    text = str(error.value)
    assert "범위(scope)가 없어요 (/rest/api/3/search/jql)" in text and "read:jira-work, read:jira-user" in text and "만료" not in text


def test_a_plain_401_is_still_a_token_problem_and_403_names_the_endpoint(monkeypatch):
    classic_settings(monkeypatch)
    monkeypatch.setattr(requests, "request", lambda *a, **k: FakeResponse(401, {"message": "Unauthorized"}))
    with pytest.raises(jira_client.JiraError, match="토큰이 만료됐거나"):
        jira_client.request("GET", "/x")
    monkeypatch.setattr(requests, "request", lambda *a, **k: FakeResponse(403))
    with pytest.raises(jira_client.JiraError, match=r"\(/rest/api/3/field\).*읽기 범위"):
        jira_client.request("GET", "/rest/api/3/field")


def gateway_settings(monkeypatch):
    for name, value in (("JIRA_BASE_URL", "https://southpaw.atlassian.net"), ("JIRA_EMAIL", "bot@serviceaccount.atlassian.com"),
                        ("JIRA_API_TOKEN", "secret-token"), ("JIRA_CLOUD_ID", "cloud-123")):
        monkeypatch.setattr(config, name, value)


def test_bearer_is_tried_when_basic_is_refused_on_the_gateway_and_remembered(monkeypatch):
    """서비스 계정 토큰은 Basic과 Bearer 중 어느 쪽이 되는지 문서마다 달라서, 되는 쪽을 자동으로 고른다"""
    gateway_settings(monkeypatch)
    calls = []

    def fake(method, url, **kw):
        calls.append("basic" if "auth" in kw else "bearer" if "Authorization" in kw["headers"] else "none")
        return FakeResponse(401) if "auth" in kw else FakeResponse(200, {"displayName": "Brain Bot"})
    monkeypatch.setattr(requests, "request", fake)
    assert jira_client.request("GET", "/rest/api/3/myself") == {"displayName": "Brain Bot"}
    assert calls == ["basic", "bearer"] and jira_client.auth_mode() == "bearer"
    jira_client.request("GET", "/rest/api/3/myself")
    assert calls == ["basic", "bearer", "bearer"]                                            # 한 번 정해지면 곧바로 그 방식으로


def test_basic_is_remembered_too_and_both_refused_is_an_auth_error(monkeypatch):
    gateway_settings(monkeypatch)
    calls = []
    monkeypatch.setattr(requests, "request", lambda method, url, **kw: calls.append(1) or FakeResponse(200 if "auth" in kw else 401))
    jira_client.request("GET", "/x")
    jira_client.request("GET", "/x")
    assert len(calls) == 2 and jira_client.auth_mode() == "basic"                           # 처음부터 되면 다시 해 보지 않는다
    monkeypatch.setattr(requests, "request", lambda method, url, **kw: calls.append(1) or FakeResponse(401))
    with pytest.raises(jira_client.JiraError, match="인증에 실패"):
        jira_client.request("GET", "/x")


def test_the_site_address_never_retries_with_bearer(monkeypatch):
    classic_settings(monkeypatch)
    calls = []
    monkeypatch.setattr(requests, "request", lambda method, url, **kw: calls.append(1) or FakeResponse(401))
    with pytest.raises(jira_client.JiraError):
        jira_client.request("GET", "/x")
    assert len(calls) == 1


def test_calls_go_to_the_site_for_classic_tokens_and_to_the_gateway_for_scoped_ones(monkeypatch):
    urls = []
    monkeypatch.setattr(requests, "request", lambda method, url, **kw: urls.append(url) or FakeResponse(200, {"ok": True}))
    classic_settings(monkeypatch)
    monkeypatch.setattr(config, "JIRA_BASE_URL", "https://southpaw.atlassian.net")
    jira_client.request("GET", "/rest/api/3/myself")
    monkeypatch.setattr(config, "JIRA_CLOUD_ID", "cloud-123")
    jira_client.request("GET", "/rest/api/3/myself")
    assert urls == ["https://southpaw.atlassian.net/rest/api/3/myself", "https://api.atlassian.com/ex/jira/cloud-123/rest/api/3/myself"]


def test_cloud_id_is_read_from_the_public_tenant_page_and_failure_is_quiet(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(config, "JIRA_BASE_URL", "https://southpaw.atlassian.net")
    seen = []
    monkeypatch.setattr(requests, "get", lambda url, **kw: seen.append(url) or FakeResponse(200, {"cloudId": "abc-123"}))
    assert jira_client.tenant_cloud_id() == "abc-123" and seen == ["https://southpaw.atlassian.net/_edge/tenant_info"]
    monkeypatch.setattr(requests, "get", lambda url, **kw: (_ for _ in ()).throw(requests.ConnectionError("down")))
    assert jira_client.tenant_cloud_id() == ""


def test_a_pasted_board_address_is_reduced_to_the_site():
    assert config._jira_origin("https://southpaw.atlassian.net/jira/software/projects/CM/boards/1?filter=&groupBy=none") == "https://southpaw.atlassian.net"
    assert config._jira_origin("") == "" and config._jira_origin("southpaw.atlassian.net") == ""


# ---------------- 슬랙 사용자 → Jira 계정 ----------------
def test_account_is_found_by_exact_email_then_by_name(jira):
    jira.users = [{"accountId": ME, "displayName": "박상우", "emailAddress": "me@company.com"},
                  {"accountId": OTHER, "displayName": "박상우2", "emailAddress": "me@company.com.kr"}]
    assert sprint_tasks.account_for(me()) == ME
    assert sprint_tasks.account_for(me()) == ME
    assert sum(c[1] == "/rest/api/3/user/search" for c in jira.calls) == 1                  # 한 번 찾으면 기억한다


def test_when_jira_hides_emails_the_name_or_a_single_result_is_used(jira):
    jira.users = [{"accountId": ME, "displayName": "박상우"}]                                # emailAddress가 숨겨져 온다
    assert sprint_tasks.account_for(me()) == ME
    jira.users = [{"accountId": ME, "displayName": "박상우"}, {"accountId": OTHER, "displayName": "박상우 (외주)"}]
    sprint_tasks._accounts.clear()
    assert sprint_tasks.account_for(me()) == ME                                             # 이메일은 못 찾아도 이름이 정확히 같은 사람


def test_unknown_or_ambiguous_people_are_never_guessed(jira):
    jira.users = [{"accountId": "a", "displayName": "김철수"}]
    with pytest.raises(sprint_tasks.AccountNotFound, match="JIRA_ACCOUNT_MAP"):
        sprint_tasks.account_for(me())
    jira.users = [{"accountId": "a", "displayName": "박상우"}, {"accountId": "b", "displayName": "박상우"}]
    with pytest.raises(sprint_tasks.AccountNotFound, match="여러 명"):
        sprint_tasks.account_for(me())


def test_a_similar_email_is_not_the_same_person(jira):
    """검색은 앞부분이 같은 이메일도 돌려준다. 이메일이 보이는데 다르면 그 사람이 아니다 (남의 카드를 보여주지 않는다)"""
    jira.users = [{"accountId": OTHER, "displayName": "다른사람", "emailAddress": "me@company.com.kr"}]
    with pytest.raises(sprint_tasks.AccountNotFound):
        sprint_tasks.account_for(me())


def test_manual_mapping_wins_over_searching(jira, monkeypatch):
    monkeypatch.setattr(config, "JIRA_ACCOUNT_MAP", {"USLACK": "acc-mapped"})
    assert sprint_tasks.account_for(me()) == "acc-mapped"
    assert not any(c[1] == "/rest/api/3/user/search" for c in jira.calls)


# ---------------- 내 카드 고르기 ----------------
def fill(jira):
    """CM·CBS는 활성 스프린트가 있는 space, KB는 스프린트가 없는 space"""
    jira.issues = [
        issue("CM-1", "넉백 추가량 구현", assignee=ME),
        issue("CM-2", "추가량 검수", approver=ME),
        issue("CM-3", "니사리엘 폭주 연출", assignee=OTHER, participant=ME),
        issue("CM-4", "끝난 작업", assignee=ME, done=True),
        issue("CM-5", "남의 작업", assignee=OTHER, reporter=OTHER, approver=OTHER),
        issue("CM-6", "다른 space가 만든 필드", assignee=ME, approver2=ME, category="new"),
        issue("CM-9", "다음 스프린트로 밀린 백로그", assignee=ME, sprint=False),
        issue("CBS-1", "내가 올린 카드", assignee=OTHER, reporter=ME),
        issue("KB-1", "스프린트 없는 space의 담당", assignee=ME, sprint=False),
        issue("KB-2", "스프린트 없는 space의 참여", participant=ME, reporter=ME, sprint=False),
        issue("KB-3", "끝난 카드", assignee=ME, done=True, sprint=False),
        issue("KB-4", "남의 카드", assignee=OTHER, sprint=False)]


def roles_of(report):
    return {c["key"]: c["roles"] for c in report["cards"]}


def test_every_role_counts_including_the_reporter(jira):
    fill(jira)
    report = sprint_tasks.build_report(ME)
    assert roles_of(report) == {"CM-1": ["담당"], "CM-2": ["확인"], "CM-3": ["참여"], "CM-6": ["담당", "확인"],
                                "CBS-1": ["보고"], "KB-1": ["담당"], "KB-2": ["참여", "보고"]}
    assert report["missing_fields"] == [] and report["partial"] is False


def test_spaces_with_a_sprint_show_only_that_sprint_and_spaces_without_show_everything_unfinished(jira):
    fill(jira)
    report = sprint_tasks.build_report(ME)
    keys = {c["key"] for c in report["cards"]}
    assert "CM-9" not in keys and report["hidden"] == 1                                      # 스프린트가 있는 space의 백로그 카드는 뺀다
    assert {"KB-1", "KB-2"} <= keys and report["no_sprint_projects"] == ["KB"]               # 스프린트가 없는 space는 스프린트와 상관없이 모두
    assert not keys & {"CM-4", "KB-3"}                                                       # 완료된 카드는 어느 쪽에서도 뺀다
    assert report["sprints"] == ["CM 1 스프린트 MVP PR 12"]                                   # 끝난 스프린트 이름은 빠진다


def test_cards_are_ordered_by_space_then_role_then_progress_then_key(jira):
    fill(jira)
    # CBS → CM → KB 순. CM 안에서는 담당(CM-1 진행 중, CM-6 대기) → 확인 → 참여. KB는 담당(KB-1) → 참여(KB-2)
    assert [c["key"] for c in sprint_tasks.build_report(ME)["cards"]] == ["CBS-1", "CM-1", "CM-6", "CM-2", "CM-3", "KB-1", "KB-2"]


def test_finished_cards_are_dropped_even_if_jira_returns_them(jira):
    fill(jira)
    jira.ignore_status_filter = True
    keys = {c["key"] for c in sprint_tasks.build_report(ME)["cards"]}
    assert not keys & {"CM-4", "KB-3"} and {"CM-1", "KB-1"} <= keys


def test_each_person_gets_only_their_own_cards_even_from_the_cache(jira):
    """한 사람의 조회 결과를 캐시에서 다른 사람에게 보여주면 안 된다 (남의 카드가 보이는 가장 위험한 실수)"""
    fill(jira)
    mine = {c["key"] for c in sprint_tasks.build_report(ME)["cards"]}
    theirs = {c["key"] for c in sprint_tasks.build_report(OTHER)["cards"]}
    assert "CM-1" in mine and "CM-1" not in theirs and {"CM-5", "KB-4"} <= theirs and not theirs & {"CM-1", "CM-2", "KB-1"}
    assert {c["key"] for c in sprint_tasks.build_report(ME)["cards"]} == mine               # 다시 물어도(캐시) 그대로


def test_my_cards_are_asked_per_person_and_the_sprint_spaces_are_shared_for_longer(jira):
    fill(jira)
    sprint_tasks.build_report(ME)
    sprint_tasks.build_report(ME)
    sprint_tasks.build_report(OTHER)
    assert len(jira.sprint_queries()) == 4                                                   # 활성 스프린트 카드 7개를 2개씩 4쪽, 사람이 달라도 한 번만
    assert all(c[2]["fields"] == ["project"] for c in jira.sprint_queries())                 # 어느 space인지만 알면 되니 project만 읽는다
    mine_calls = len(jira.my_queries())
    assert {f'assignee = "{ME}"' in q for q in jira.my_queries()} == {True, False}           # 두 사람이 각자 조회했다
    sprint_tasks.build_report(ME)
    assert len(jira.my_queries()) == mine_calls                                              # 같은 사람은 캐시
    jira.clock.t += config.JIRA_CACHE_SECONDS + 1
    sprint_tasks.build_report(ME)
    assert len(jira.my_queries()) > mine_calls                                               # 5분이 지나면 내 카드는 다시 읽는다
    assert len(jira.sprint_queries()) == 4                                                   # 스프린트 있는 space 목록은 30분 동안 그대로
    jira.clock.t += config.JIRA_SPRINT_CACHE_SECONDS + 1
    sprint_tasks.build_report(ME)
    assert len(jira.sprint_queries()) == 8


def test_only_the_fields_that_are_shown_are_read(jira):
    fill(jira)
    sprint_tasks.build_report(ME)
    for fields in jira.my_query_fields():
        assert set(fields) == {"summary", "status", "assignee", "reporter", "project", "customfield_10020",
                               "customfield_10102", "customfield_10101", "customfield_10201"}      # 에픽(parent)이나 이슈 유형은 읽지 않는다


def test_the_search_asks_for_every_role_field_of_every_space(jira):
    fill(jira)
    sprint_tasks.build_report(ME)
    jql = [q for q in jira.my_queries() if f'assignee = "{ME}"' in q][0]
    assert jql.startswith("statusCategory != Done AND (") and f'reporter = "{ME}"' in jql
    assert all(f'cf[{n}] = "{ME}"' in jql for n in ("10102", "10101", "10201"))              # 확인자 필드는 space마다 따로 있어서 모두


def test_an_account_id_that_could_alter_the_query_is_refused(jira):
    with pytest.raises(jira_client.JiraError, match="형식이 올바르지 않아요"):
        sprint_tasks.build_report('x") OR project is not EMPTY OR assignee = ("y')
    assert jira.my_queries() == []


def test_when_jira_rejects_the_participant_and_approver_conditions_the_rest_is_still_found(jira):
    fill(jira)
    jira.search_error = jira_client.JiraError("Jira 요청이 실패했어요 (HTTP 400).", status=400)
    report = sprint_tasks.build_report(ME)
    assert report["partial"] is True
    # 담당·보고로 찾은 카드는 카드에 적힌 확인자·참여자 값까지 반영해 그대로 나오고, 확인·참여로만 걸리던 카드(CM-2, CM-3)만 빠진다
    assert roles_of(report) == {"CM-1": ["담당"], "CM-6": ["담당", "확인"], "CBS-1": ["보고"], "KB-1": ["담당"], "KB-2": ["참여", "보고"]}
    assert "_확인자·참여자 조건으로는 검색하지 못해 담당·보고 카드만 찾았어요._" in sprint_tasks.format_report(report)


def test_other_search_failures_are_not_hidden_behind_the_fallback(jira):
    fill(jira)
    jira.search_error = jira_client.JiraError("Jira 권한이 부족해요", status=403)
    with pytest.raises(jira_client.JiraError, match="권한이 부족"):
        sprint_tasks.build_report(ME)


def test_missing_role_fields_are_reported_not_silently_ignored(jira, monkeypatch):
    fill(jira)
    monkeypatch.setattr(config, "JIRA_FIELD_APPROVER", "검토자")
    report = sprint_tasks.build_report(ME)
    assert report["missing_fields"] == ["검토자"] and "CM-2" not in {c["key"] for c in report["cards"]}
    assert "_'검토자' 필드를 Jira에서 찾지 못해 그 역할의 카드는 확인하지 못했어요._" in sprint_tasks.format_report(report)
    assert all("cf[10101]" not in q and "cf[10201]" not in q for q in jira.my_queries())


def test_every_card_is_shown_even_when_the_list_is_long(jira):
    jira.issues = [issue(f"KB-{n}", f"카드 {n}", reporter=ME, sprint=False) for n in range(1, 26)]
    text = sprint_tasks.format_report(sprint_tasks.build_report(ME))
    assert text.count("• <") == 25 and "외 " not in text and "카드 25개예요" in text          # 생략하지 않는다


def test_a_very_long_dm_is_split_by_lines_so_no_link_is_ever_cut(jira):
    """긴 DM은 슬랙 전송 단계에서 줄 단위로 나뉜다. 카드 한 줄이 링크 하나이므로 링크가 중간에 잘려 깨지는 일이 없어야 한다"""
    jira.issues = [issue(f"{space}-{n}", f"[개발/컨텐츠] 아주 긴 제목의 카드 번호 {n} " + "가나다라마바사" * 5, reporter=ME, sprint=False)
                   for space in ("CM", "KB", "XY") for n in range(1, 61)]
    text = sprint_tasks.format_report(sprint_tasks.build_report(ME))
    chunks = slack_service.split_for_slack(text)
    assert len(chunks) > 2 and all(len(c.encode()) <= slack_service.SLACK_TEXT_BYTES for c in chunks)
    for chunk in chunks:
        for line in chunk.splitlines():
            assert line.count("<") == line.count(">") and (not line.startswith("• <") or line.count("|") == 1)
    assert "\n".join(chunks).count("• <") == 180                                           # 180장이 하나도 빠지거나 겹치지 않는다
    assert all(slack_service.format_markdown_for_slack(c) == c for c in chunks)


def test_dm_is_split_by_space_then_role_and_each_card_appears_once(jira):
    jira.issues = [issue("CM-1", "넉백 추가량 구현", assignee=ME, approver=ME),             # 담당이면서 확인: '담당'에만 나온다
                   issue("CM-2", "추가량 검수", approver=ME),
                   issue("CM-8", "다음으로 밀린 카드", assignee=ME, sprint=False),
                   issue("KB-7", "버그 수정", reporter=ME, sprint=False)]
    text = sprint_tasks.format_report(sprint_tasks.build_report(ME))
    assert text == (
        "카드 3개예요 (완료된 카드 제외)\n"
        "\n"
        "*Content (CM)*\n"
        "*담당*\n"
        "• <https://southpaw.atlassian.net/browse/CM-1|CM-1 넉백 추가량 구현> · 진행 중\n"
        "\n"
        "*확인*\n"
        "• <https://southpaw.atlassian.net/browse/CM-2|CM-2 추가량 검수> · 진행 중\n"
        "\n"
        "*Kanban (KB)*\n"
        "*보고*\n"
        "• <https://southpaw.atlassian.net/browse/KB-7|KB-7 버그 수정> · 진행 중\n"
        "\n"
        "_스프린트가 없는 space(KB)는 완료되지 않은 카드를 모두 보여줬어요._")
    assert text.count("CM-1 ") == 1 and "다른 역할" not in text                              # 여러 역할이어도 한 번만, 군더더기 없이


def test_every_card_is_one_line_with_exactly_one_link_and_nothing_is_added_by_the_slack_conversion(jira):
    fill(jira)
    text = sprint_tasks.format_report(sprint_tasks.build_report(ME))
    card_lines = [l for l in text.splitlines() if l.startswith("• <")]
    assert len(card_lines) == 7 and all(l.count("<https://") == 1 and l.count(">") == 1 for l in card_lines)
    assert "🔗" not in text and text.count("https://") == 7                                 # 주소는 링크 안에만 있고 화면에 드러나지 않는다
    assert slack_service.format_markdown_for_slack(text) == text                             # 슬랙으로 보내는 변환이 링크마다 🔗를 붙이지 않는다
    blocks = text.split("\n\n")
    assert blocks[0].startswith("카드 7개") and all("\n\n" not in b for b in blocks)        # 묶음 사이는 정확히 한 줄 띄운다


def test_titles_are_made_safe_for_slack_links_and_kept_short(jira):
    jira.issues = [issue("KB-1", "[개발/컨텐츠] 넉백 (밸런스) & 연출 <긴급> | 확인", assignee=ME, sprint=False),
                   issue("KB-2", "가" * 80, assignee=ME, sprint=False)]
    lines = sprint_tasks.format_report(sprint_tasks.build_report(ME)).splitlines()
    first = [l for l in lines if "KB-1|" in l][0]
    assert "[개발/컨텐츠] 넉백 (밸런스) &amp; 연출 &lt;긴급&gt; ｜ 확인" in first and first.count("|") == 1     # 링크 글자 안의 | & < >는 바꾼다
    long = [l for l in lines if "KB-2|" in l][0]
    assert "가" * 48 + "…" in long and "가" * 50 not in long


def test_an_empty_result_is_short_and_clear(jira):
    fill(jira)
    assert sprint_tasks.format_report(sprint_tasks.build_report("acc-nobody")) == "맡은 카드가 없어요 (완료된 카드 제외)"


def test_no_notion_documents_are_looked_up_anymore(jira):
    fill(jira)
    text = sprint_tasks.format_report(sprint_tasks.build_report(ME))
    assert "문서" not in text and "notion" not in text.lower()
    assert not hasattr(sprint_tasks, "link_sources") and not hasattr(jira_client, "comments")


# ---------------- 에이전트 도구 ----------------
class State:
    def __init__(self):
        self.messages, self.calls = [], []

    def progress(self, text):
        self.messages.append(text)


def test_tool_answers_only_for_the_person_who_asked(jira):
    fill(jira)
    jira.users = [{"accountId": ME, "displayName": "박상우", "emailAddress": "me@company.com"}]
    token = asker.set_asker(me())
    try:
        text = agent.run_tool(State(), "my_sprint_tasks", "{}")
        assert "/browse/CM-1|" in text and "CM-5" not in text
        # 모델이 다른 사람을 지정하려 해도 받아주지 않는다 (도구에는 사람을 가리키는 인자가 없다)
        refused = agent.run_tool(State(), "my_sprint_tasks", json.dumps({"user": OTHER}))
        assert "CM-5" not in refused and "CM-3" not in refused
    finally:
        asker.reset(token)
    assert sprint_tasks.TOOL["parameters"]["properties"] == {}


def test_tool_result_is_marked_as_the_finished_answer(jira):
    fill(jira)
    jira.users = [{"accountId": ME, "displayName": "박상우", "emailAddress": "me@company.com"}]
    state, token = State(), asker.set_asker(me())
    try:
        text = sprint_tasks.tool_my_sprint_tasks(state)
    finally:
        asker.reset(token)
    assert state.direct_answer == text and text.startswith("카드 7개예요")


def test_tool_explains_why_it_cannot_answer(jira, monkeypatch):
    state = State()
    assert "질문한 사람을 알 수 없어서" in sprint_tasks.tool_my_sprint_tasks(state)           # 질문자 정보가 없다
    assert "알 수 없어서" in state.direct_answer                                              # 안내도 그대로 보낸다
    token = asker.set_asker(me())
    try:
        jira.users = []
        assert "JIRA_ACCOUNT_MAP" in sprint_tasks.tool_my_sprint_tasks(state)                # 계정을 못 찾았다
        monkeypatch.setattr(sprint_tasks, "account_for", lambda who: (_ for _ in ()).throw(jira_client.JiraError("Jira 인증에 실패했어요.")))
        assert sprint_tasks.tool_my_sprint_tasks(state) == "Jira 인증에 실패했어요."
        monkeypatch.setattr(sprint_tasks, "account_for", lambda who: (_ for _ in ()).throw(ValueError("예상 밖 오류 세부사항")))
        text = sprint_tasks.tool_my_sprint_tasks(state)
        assert "오류가 났어요" in text and "세부사항" not in text                              # 내부 오류는 사용자에게 그대로 보이지 않는다
    finally:
        asker.reset(token)
    assert state.messages[0].startswith("🗂️")


def test_jira_tool_and_rules_reach_the_agent_only_when_jira_is_configured(jira, monkeypatch):
    sent = []

    class Responses:
        def create(self, **kwargs):
            sent.append(kwargs)
            usage = types.SimpleNamespace(input_tokens=1, output_tokens=1)
            return types.SimpleNamespace(id="r", output=[], output_text="답", usage=usage)
    monkeypatch.setattr(clients, "get_openai", lambda: types.SimpleNamespace(responses=Responses()))
    monkeypatch.setattr(ai_engine, "finish_answer", lambda query, reply, *args: {"AI_답변": reply})
    agent.ask_agent("이번 스프린트에 내가 할 일 뭐야?", [], {})
    assert "my_sprint_tasks" in [t["name"] for t in sent[0]["tools"]] and sprint_tasks.PROMPT in sent[0]["instructions"]
    monkeypatch.setattr(config, "JIRA_API_TOKEN", "")
    agent.ask_agent("이번 스프린트에 내가 할 일 뭐야?", [], {})
    assert "my_sprint_tasks" not in [t["name"] for t in sent[1]["tools"]] and sprint_tasks.PROMPT not in sent[1]["instructions"]


def test_prompt_lets_the_tool_answer_and_keeps_it_private():
    for rule in ("my_sprint_tasks 도구만 부르면", "그대로 보내므로 다른 말을 덧붙이거나 양식을 바꾸지 마라", "다른 사람의 카드는 조회할 수 없다고"):
        assert rule in sprint_tasks.PROMPT
    assert "그대로 사용자에게 전달된다" in sprint_tasks.TOOL["description"]


# ---------------- 슬랙 질문 처리와 연결 ----------------
def test_the_asker_is_known_during_the_answer_and_forgotten_afterwards(monkeypatch):
    seen = {}

    def fake_ask(query, history, on_progress=None):
        who = asker.get_asker()
        seen.update(slack_id=who.slack_id, profile=who.profile(), profile_again=who.profile())
        seen["lookups_during_answer"] = len(lookups)
        return {"status": "success", "AI_답변": "답변", "page_ids": [], "images": []}
    monkeypatch.setattr(ai_engine, "ask_ai_with_history", fake_ask)
    lookups = []

    class Slack:
        def chat_update(self, **kwargs):
            pass

        def users_info(self, user):
            lookups.append(user)
            return {"user": {"real_name": "박상우", "profile": {"email": "me@company.com"}}}

        def conversations_replies(self, channel, ts, **kwargs):
            return {"messages": []}
    slack_service.process_chat_and_reply(Slack(), lambda text, thread_ts=None: {"ts": "4"}, "C", "1", "1", "<@BOT> 내 스프린트", "USLACK")
    assert seen["slack_id"] == "USLACK" and seen["profile"] == {"name": "박상우", "email": "me@company.com"}
    assert seen["lookups_during_answer"] == 1                                               # 도구가 두 번 물어도 프로필은 한 번만 읽는다
    assert lookups == ["USLACK", "USLACK"]                                                  # (나머지 한 번은 질문 기록을 남기는 기존 코드의 조회)
    assert asker.get_asker() is None


def test_nothing_is_looked_up_when_the_answer_does_not_need_the_asker(monkeypatch):
    monkeypatch.setattr(ai_engine, "ask_ai_with_history", lambda q, h, on_progress=None: {"status": "success", "AI_답변": "답변", "page_ids": [], "images": []})

    class Slack:
        def chat_update(self, **kwargs):
            pass

        def users_info(self, user):
            raise AssertionError("질문자 프로필은 도구가 필요할 때만 읽어야 한다")

        def conversations_replies(self, channel, ts, **kwargs):
            return {"messages": []}
    slack_service.process_chat_and_reply(Slack(), lambda text, thread_ts=None: {"ts": "4"}, "C", "1", "1", "<@BOT> 질문", "USLACK")


# ---------------- 점검 스크립트 ----------------
def test_check_script_walks_through_the_setup(jira):
    fill(jira)
    jira.users = [{"accountId": ME, "displayName": "박상우", "emailAddress": "me@company.com"}]
    lines = []
    assert jira_check.check("me@company.com", out=lines.append) is True
    text = "\n".join(lines)
    assert "인증: ✅ Brain Bot 계정으로 접속했어요. (인증 방식: 이메일:토큰)" in text and "필드 '확인자': ✅ 2개" in text and "필드 '참여자': ✅ 1개" in text
    assert "활성 스프린트 카드: 7개" in text and "5. me@company.com → Jira 계정 acc-me" in text and "/browse/CM-1|" in text
    assert "secret-token" not in text and "사이트 Cloud ID: cloud-xyz ← 범위 지정 토큰이나 서비스 계정이면 .env의 JIRA_CLOUD_ID에 넣으세요" in text
    assert "역할 필드가 채워진 카드: 담당" in text and "/ 보고" in text


def test_check_script_lists_visible_spaces_cards_per_space_and_spaces_without_a_sprint(jira):
    fill(jira)
    lines = []
    assert jira_check.check(out=lines.append) is True
    text = "\n".join(lines)
    assert "봇이 볼 수 있는 space 4개: CM, CBS, KB, AB" in text and "활성 스프린트 카드 space별: CM 6 · CBS 1" in text
    assert "활성 스프린트가 없는 space (완료되지 않은 카드를 모두 보여줘요): KB, AB" in text


def test_check_script_names_the_spaces_the_bot_cannot_see(jira):
    fill(jira)
    lines = []
    assert jira_check.check(out=lines.append, expect=("CM", "NOPE", "HIDDEN")) is False
    assert any("봇이 볼 수 없는 space: NOPE, HIDDEN" in l and "프로젝트 찾아보기" in l for l in lines)
    lines.clear()
    assert jira_check.check(out=lines.append, expect=("CM", "AB")) is True                   # 모두 보이면 통과
    assert not any("볼 수 없는" in l for l in lines)


def test_check_script_points_at_the_broken_step(jira, monkeypatch):
    monkeypatch.setattr(config, "JIRA_API_TOKEN", "")
    lines = []
    assert jira_check.check(out=lines.append) is False and "넣어 주세요" in lines[-1]
    monkeypatch.setattr(config, "JIRA_API_TOKEN", "secret-token")
    monkeypatch.setattr(jira_client, "myself", lambda: (_ for _ in ()).throw(jira_client.JiraError("Jira 인증에 실패했어요.")))
    lines.clear()
    assert jira_check.check(out=lines.append) is False and "❌ Jira 인증에 실패했어요." in lines[-1]


def test_check_script_flags_missing_fields(jira, monkeypatch):
    monkeypatch.setattr(config, "JIRA_FIELD_PARTICIPANT", "없는필드")
    lines = []
    assert jira_check.check(out=lines.append) is False
    assert any("필드 '없는필드': ❌ 못 찾음" in l for l in lines)


# ---------------- 도구 결과를 그대로 답변으로 ----------------
def fn_call(name, call_id="c1"):
    return types.SimpleNamespace(type="function_call", name=name, arguments="{}", call_id=call_id)


class ScriptedAI:
    """첫 응답이 도구 호출이고, 그다음 응답은 모델이 쓴 글. 몇 번 불렸는지 센다"""
    def __init__(self, first_calls):
        self.requests, self.first_calls = [], first_calls

    def create(self, **kwargs):
        self.requests.append(kwargs)
        usage = types.SimpleNamespace(input_tokens=100, output_tokens=10)
        if len(self.requests) == 1:
            return types.SimpleNamespace(id="r1", output=self.first_calls, output_text="", usage=usage)
        return types.SimpleNamespace(id="r2", output=[], output_text="모델이 다시 쓴 답", usage=usage)


@pytest.fixture
def agent_env(jira, monkeypatch):
    fill(jira)
    jira.users = [{"accountId": ME, "displayName": "박상우", "emailAddress": "me@company.com"}]
    finished = []
    monkeypatch.setattr(ai_engine, "finish_answer", lambda query, reply, *args: finished.append(reply) or {"AI_답변": reply, "status": "success"})
    token = asker.set_asker(me())
    yield finished
    asker.reset(token)


def run_agent(monkeypatch, first_calls):
    fake = ScriptedAI(first_calls)
    monkeypatch.setattr(clients, "get_openai", lambda: types.SimpleNamespace(responses=fake))
    return fake, agent.ask_agent("이번 스프린트에 내가 할 일 뭐야?", [], {})


def test_the_card_list_is_sent_as_is_without_asking_the_model_to_rewrite_it(agent_env, monkeypatch):
    fake, result = run_agent(monkeypatch, [fn_call("my_sprint_tasks")])
    assert len(fake.requests) == 1                                                           # 모델은 도구를 고르는 한 번만 불린다
    assert result["AI_답변"].startswith("카드 7개예요 (완료된 카드 제외)") and "모델이 다시 쓴 답" not in result["AI_답변"]
    assert agent_env == []                                                                   # 참고 문서·백로그 문구를 붙이는 단계를 거치지 않는다
    assert result["status"] == "success" and result["page_ids"] == [] and result["images"] == [] and result["used_docs"] == []
    assert result["usage"] == {"input_tokens": 100, "output_tokens": 10, "tool_calls": 1} and result["exports"] == [] and result["summary_requests"] == []


def test_an_explanation_instead_of_cards_is_also_sent_as_is(agent_env, monkeypatch):
    asker.reset(asker.set_asker(me()))
    token = asker.set_asker(asker.Asker("UNKNOWN", lambda: {"name": "없는사람", "email": "none@company.com"}))
    try:
        fake, result = run_agent(monkeypatch, [fn_call("my_sprint_tasks")])
    finally:
        asker.reset(token)
    assert len(fake.requests) == 1 and "JIRA_ACCOUNT_MAP" in result["AI_답변"]


def test_when_other_tools_were_called_too_the_model_continues_as_usual(agent_env, monkeypatch):
    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "search_docs", lambda state, query="": "검색 결과")
    fake, result = run_agent(monkeypatch, [fn_call("my_sprint_tasks"), fn_call("search_docs", "c2")])
    assert len(fake.requests) == 2 and agent_env == ["모델이 다시 쓴 답"]                     # 다른 요청이 섞였으면 모델이 이어서 답한다


def test_ordinary_questions_never_skip_the_model(agent_env, monkeypatch):
    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "search_docs", lambda state, query="": "검색 결과")
    fake, result = run_agent(monkeypatch, [fn_call("search_docs")])
    assert len(fake.requests) == 2 and agent_env == ["모델이 다시 쓴 답"]
