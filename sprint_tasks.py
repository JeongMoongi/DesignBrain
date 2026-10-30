"""'내가 할 일' — 질문한 사람 본인이 담당자·확인자·참여자·보고자로 맡은 Jira 카드를 DM 양식으로 만들어 준다. 카드 링크만 보낸다.
에이전트의 도구(my_sprint_tasks)로 붙는다. 도구는 사람을 인자로 받지 않고 질문한 사람(asker.py)만 조회하므로 다른 사람의 카드는 볼 수 없다.

어떤 카드를 보여주나
 - 완료되지 않은 카드 중에서, 본인이 담당자(assignee)·보고자(reporter)이거나 확인자·참여자 필드에 들어 있는 카드.
   확인자·참여자는 팀이 만든 필드라 이름으로 ID를 찾는다 (jira_client.discover_fields). space마다 달라도 이름이 같으면 모두 본다.
 - 활성 스프린트가 있는 space는 그 스프린트에 들어 있는 카드만 (스프린트에 없는 백로그 카드는 뺀다).
 - 활성 스프린트가 없는 space는 완료되지 않은 카드를 모두.
   '활성 스프린트가 있는 space'는 JQL sprint in openSprints()로 활성 스프린트 카드가 하나라도 있는 space다.

DM 양식 (양식은 코드가 만들고, 에이전트는 모델을 다시 부르지 않고 그대로 보낸다)
 1. space별로 나눈다.  2. 그 안에서 담당·확인·참여·보고 순으로 나눈다.
 3. 한 카드는 한 번만 나온다: 여러 역할이면 앞의 역할 묶음에만 (담당이면서 확인이면 '담당'에만).
 4. 간결하게: 카드는 한 줄(링크 하나), 묶음 사이는 빈 줄로 띄운다. 카드는 생략하지 않는다 (길면 슬랙 전송 단계가 줄 단위로 나눠 보낸다).

읽는 양을 줄이기 위해: 본인 카드만 Jira에 직접 조회하고(사람별로 JIRA_CACHE_SECONDS 기억),
'어느 space에 활성 스프린트가 있나'는 project 필드만 읽어 JIRA_SPRINT_CACHE_SECONDS(기본 30분) 동안 여러 사람이 같이 쓴다.
"""
import logging
import re
import threading
from typing import Dict, List, Set, Tuple
import asker
import config
import jira_client
from jira_client import JiraError

logger = logging.getLogger(__name__)

# (이름, 카드 필드). 위에서부터 우선순위: 여러 역할이면 앞의 역할 묶음에 나오고 나머지는 '다른 역할'로 적는다
ROLES = (("담당", "assignee"), ("확인", "approvers"), ("참여", "participants"), ("보고", "reporter"))
ROLE_ORDER = [label for label, _ in ROLES]
CATEGORY_ORDER = {"indeterminate": 0, "new": 1}      # 진행 중인 카드를 먼저
ACCOUNT_CACHE_SECONDS = 86400
ACCOUNT_ID = re.compile(r"^[0-9A-Za-z:_\-]+$")


class AccountNotFound(JiraError):
    """슬랙 사용자에 해당하는 Jira 계정을 찾지 못했거나 하나로 정할 수 없다"""


# --- [카드 읽기] ---
def user_ids(value) -> List[str]:
    """사용자 필드 값(한 명, 여러 명, 비어 있음)에서 accountId 목록"""
    if not value:
        return []
    people = value if isinstance(value, list) else [value]
    return [p["accountId"] for p in people if isinstance(p, dict) and p.get("accountId")]


def normalize(issue: Dict, ids: Dict) -> Dict:
    fields = issue.get("fields", {})
    status, project = fields.get("status") or {}, fields.get("project") or {}
    sprints = [s.get("name", "") for s in (fields.get(ids["sprint"]) or []) if isinstance(s, dict) and s.get("state") == "active"] if ids["sprint"] else []
    category = (status.get("statusCategory") or {}).get("key", "")
    return {"key": issue["key"], "summary": fields.get("summary", ""), "status": status.get("name", ""), "category": category, "done": category == "done",
            "assignee": user_ids(fields.get("assignee")), "reporter": user_ids(fields.get("reporter")),
            "approvers": [uid for fid in ids["approver"] for uid in user_ids(fields.get(fid))],
            "participants": [uid for fid in ids["participant"] for uid in user_ids(fields.get(fid))],
            "project": project.get("key", ""), "project_name": project.get("name", ""), "sprints": sprints, "in_sprint": bool(sprints),
            "url": f"{config.JIRA_BASE_URL}/browse/{issue['key']}"}


_cache_lock = threading.Lock()
_sprint_lock = threading.Lock()
_sprint_keys = {"at": 0.0, "keys": None}
_mine = {}


def fields_to_read(ids: Dict) -> List[str]:
    """카드를 보여주는 데 필요한 필드만 읽는다 (에픽·이슈 유형처럼 안 쓰는 것은 읽지 않는다)"""
    return ["summary", "status", "assignee", "reporter", "project"] + ([ids["sprint"]] if ids["sprint"] else []) + ids["participant"] + ids["approver"]


def sprint_projects() -> Set[str]:
    """활성 스프린트가 있는 space의 키. project 필드만 읽고, 스프린트가 바뀔 때만 달라지므로 JIRA_SPRINT_CACHE_SECONDS 동안 기억한다.
    동시에 묻는 사람들은 같은 결과를 기다려 쓴다"""
    with _sprint_lock:
        if _sprint_keys["keys"] is not None and jira_client.now() - _sprint_keys["at"] < config.JIRA_SPRINT_CACHE_SECONDS:
            return _sprint_keys["keys"]
        issues = jira_client.search("sprint in openSprints() ORDER BY project ASC", ["project"])
        keys = {(i.get("fields", {}).get("project") or {}).get("key", "") for i in issues} - {""}
        _sprint_keys.update(at=jira_client.now(), keys=keys)
        logger.info("🗂️ [Jira] 활성 스프린트가 있는 space %d곳 (카드 %d개)", len(keys), len(issues))
        return keys


def active_overview() -> List[Dict]:
    """활성 스프린트의 모든 카드 (점검 스크립트의 개요용. 기억하지 않는다)"""
    ids = jira_client.discover_fields()
    issues = jira_client.search("sprint in openSprints() ORDER BY project ASC, key ASC", fields_to_read(ids))
    return [normalize(i, ids) for i in issues]


def my_cards(account_id: str) -> Tuple[List[Dict], bool]:
    """본인이 담당자·보고자이거나 확인자·참여자 필드에 들어 있는 완료되지 않은 카드 (모든 space) 와, 확인자·참여자 조건을 못 썼는지.
    사용자 필드의 JQL 연산자는 필드 종류에 따라 거부될 수 있다(HTTP 400). 그러면 담당·보고만으로 다시 찾는다."""
    if not ACCOUNT_ID.match(account_id):
        raise JiraError("Jira 계정 ID 형식이 올바르지 않아요.")
    with _cache_lock:
        cached = _mine.get(account_id)
        if cached and jira_client.now() - cached[0] < config.JIRA_CACHE_SECONDS:
            return cached[1], cached[2]
    ids = jira_client.discover_fields()
    custom = ids["participant"] + ids["approver"]

    def run(with_custom: bool) -> List[Dict]:
        clauses = [f'assignee = "{account_id}"', f'reporter = "{account_id}"']
        if with_custom:
            clauses += [f'cf[{fid.split("_")[1]}] = "{account_id}"' for fid in custom]
        jql = f'statusCategory != Done AND ({" OR ".join(clauses)}) ORDER BY project ASC, key ASC'
        return [normalize(i, ids) for i in jira_client.search(jql, fields_to_read(ids))]
    try:
        cards, partial = run(True), False
    except JiraError as e:
        if e.status != 400 or not custom:
            raise
        logger.warning("⚠️ [Jira] 확인자·참여자 조건을 쓴 검색이 거부돼 담당·보고만으로 찾습니다: %s", e)
        cards, partial = run(False), True
    with _cache_lock:
        _mine[account_id] = (jira_client.now(), cards, partial)
    return cards, partial


# --- [슬랙 사용자 → Jira 계정] ---
_accounts = {}


def account_for(who: asker.Asker) -> str:
    """직접 짝지은 표(JIRA_ACCOUNT_MAP) → 슬랙 프로필의 이메일 → 이름 순서로 Jira 계정 ID를 찾는다. 하나로 정해지지 않으면 AccountNotFound"""
    if config.JIRA_ACCOUNT_MAP.get(who.slack_id):
        return config.JIRA_ACCOUNT_MAP[who.slack_id]
    cached = _accounts.get(who.slack_id)
    if cached and jira_client.now() - cached[0] < ACCOUNT_CACHE_SECONDS:
        return cached[1]
    profile = who.profile()
    email, name = (profile.get("email") or "").strip().lower(), " ".join((profile.get("name") or "").split())
    found = []
    if email:
        users = jira_client.find_users(email)
        exact = [u for u in users if (u.get("emailAddress") or "").lower() == email]
        # Jira가 이메일을 숨기면 확인할 수 없으니, 검색 결과가 한 명뿐일 때만 그 사람으로 본다
        found = exact or (users if len(users) == 1 and not users[0].get("emailAddress") else [])
    if not found and name:
        found = [u for u in jira_client.find_users(name) if " ".join((u.get("displayName") or "").split()) == name]
    if len(found) == 1:
        _accounts[who.slack_id] = (jira_client.now(), found[0]["accountId"])
        return found[0]["accountId"]
    reason = "같은 이름의 Jira 사용자가 여러 명이라 한 명으로 정할 수 없어요" if found else "슬랙 프로필의 이메일과 이름으로 Jira 사용자를 찾지 못했어요"
    raise AccountNotFound(f"{reason}. 관리자에게 슬랙 계정과 Jira 계정을 직접 연결해 달라고 요청해 주세요 (JIRA_ACCOUNT_MAP).")


# --- [보여줄 카드 고르기] ---
def card_order(card: Dict):
    """space → 역할 → 진행 중 먼저 → 카드 번호"""
    number = card["key"].rsplit("-", 1)[-1]
    return (card["project"], ROLE_ORDER.index(card["roles"][0]), CATEGORY_ORDER.get(card["category"], 2), int(number) if number.isdigit() else 0)


def build_report(account_id: str) -> Dict:
    sprint_keys = sprint_projects()
    found, partial = my_cards(account_id)
    ids = jira_client.discover_fields()
    shown, hidden = [], 0
    for card in found:
        if card["done"]:
            continue
        roles = [label for label, field in ROLES if account_id in card[field]]
        if not roles:
            continue
        # 활성 스프린트가 있는 space는 그 스프린트의 카드만, 없는 space는 완료되지 않은 카드를 모두
        if card["in_sprint"] or card["project"] not in sprint_keys:
            shown.append({**card, "roles": roles})
        else:
            hidden += 1
    shown.sort(key=card_order)
    return {"cards": shown, "hidden": hidden, "partial": partial,
            "sprints": sorted({s for c in shown for s in c["sprints"]}),
            "no_sprint_projects": sorted({c["project"] for c in shown if not c["in_sprint"]}),
            "missing_fields": [name for name, key in ((config.JIRA_FIELD_APPROVER, "approver"), (config.JIRA_FIELD_PARTICIPANT, "participant")) if not ids[key]]}


TITLE_CHARS = 50


def slack_text(text: str) -> str:
    """슬랙 링크 글자로 쓸 수 있게 다듬는다: 공백 정리, 길면 줄임, 슬랙이 특별히 읽는 문자(&, <, >, |)는 바꾼다"""
    text = " ".join(text.split())
    if len(text) > TITLE_CHARS:
        text = text[:TITLE_CHARS - 1].rstrip() + "…"
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "｜")


def card_line(card: Dict) -> str:
    # 슬랙 링크 문법을 직접 쓴다 (마크다운 링크는 변환 과정에서 링크마다 🔗가 붙는다)
    return f"• <{card['url']}|{card['key']} {slack_text(card['summary'])}> · {slack_text(card['status'])}"


def format_report(report: Dict) -> str:
    """슬랙에 그대로 보낼 DM 글. space별 → 담당·확인·참여·보고, 카드는 한 줄씩, 묶음 사이는 빈 줄"""
    cards = report["cards"]
    blocks = [f"카드 {len(cards)}개예요 (완료된 카드 제외)" if cards else "맡은 카드가 없어요 (완료된 카드 제외)"]
    for project in sorted({c["project"] for c in cards}):
        in_space = [c for c in cards if c["project"] == project]
        name = in_space[0]["project_name"]
        groups = [f"*{name} ({project})*" if name and name != project else f"*{project}*"]
        role_blocks = []
        for role in ROLE_ORDER:
            group = [c for c in in_space if c["roles"][0] == role]           # 한 카드는 첫 번째 역할 묶음에만 나온다
            if group:
                role_blocks.append("\n".join([f"*{role}*"] + [card_line(c) for c in group]))      # 카드는 생략하지 않고 모두 보여준다
        blocks.append("\n".join(groups) + "\n" + "\n\n".join(role_blocks))
    notes = []
    if report["no_sprint_projects"]:
        notes.append(f"스프린트가 없는 space({', '.join(report['no_sprint_projects'])})는 완료되지 않은 카드를 모두 보여줬어요.")
    for name in report["missing_fields"]:
        notes.append(f"'{name}' 필드를 Jira에서 찾지 못해 그 역할의 카드는 확인하지 못했어요.")
    if report["partial"]:
        notes.append("확인자·참여자 조건으로는 검색하지 못해 담당·보고 카드만 찾았어요.")
    if notes:
        blocks.append("\n".join(f"_{n}_" for n in notes))
    return "\n\n".join(blocks)


# --- [에이전트 도구] ---
TOOL = {
    "type": "function", "name": "my_sprint_tasks",
    "description": "질문한 사람 본인이 담당자·확인자·참여자·보고자로 맡은, 완료되지 않은 Jira 카드를 space별로 정리해 보낸다. 활성 스프린트가 있는 space는 "
                   "그 스프린트의 카드만, 없는 space는 완료되지 않은 카드를 모두 준다. '이번 스프린트에 내가 할 일', '내 작업', '내 카드' 같은 질문에 쓴다. "
                   "인자는 없다. 결과는 완성된 답변이라 그대로 사용자에게 전달된다. 다른 사람의 카드는 조회할 수 없다.",
    "parameters": {"type": "object", "properties": {}}}

PROMPT = (
    "\n\n[Jira 스프린트 질문]\n"
    "'이번 스프린트에 내가 할 일', '내 카드'처럼 질문한 사람 본인의 Jira 작업을 묻는 질문은 my_sprint_tasks 도구만 부르면 된다. "
    "도구가 완성된 답변(space별, 담당·확인·참여·보고 순)을 만들어 그대로 보내므로 다른 말을 덧붙이거나 양식을 바꾸지 마라.\n"
    "- 질문에 다른 요청도 함께 있어 직접 답해야 한다면, 도구 결과의 양식과 링크를 그대로 옮겨라.\n"
    "- 다른 사람의 카드는 조회할 수 없다고 말하라."
)


def tool_my_sprint_tasks(state) -> str:
    """결과가 곧 완성된 답변이다. 성공이든 안내(계정을 못 찾음 등)든 모델이 다시 쓰지 않고 그대로 보낸다"""
    state.progress("🗂️ Jira 카드 확인 중…")
    who = asker.get_asker()
    if who is None:
        text = "질문한 사람을 알 수 없어서 본인 카드를 조회할 수 없어요."
    else:
        try:
            text = format_report(build_report(account_for(who)))
        except JiraError as e:
            text = str(e)
        except Exception:
            logger.exception("🚨 [Jira] 카드 조회 실패")
            text = "Jira에서 카드를 읽는 중 오류가 났어요. 잠시 후 다시 물어봐 주세요."
    state.direct_answer = text
    return text
