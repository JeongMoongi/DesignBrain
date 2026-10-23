"""기획서 속성 검사: 기획자가 'DB 등록'한 문서의 노션 속성(기획서 이름, 개요, 상태, 담당자, 마지막 수정일)이 비어 있으면
경고와 함께 채울 값을 정해 슬랙으로 보내고, 승인하면 노션에 입력한다 (슬랙 쪽은 property_slack.py).

- 검사 시점: 노션의 'DB 등록' 버튼(웹훅 /webhook/notion)으로 들어온 동기화만. 알림은 그 버튼을 누른 사람에게 간다.
  전체 재동기화(resync_all.py)나 사서 제안 승인 뒤의 재동기화는 검사하지 않는다.
- 기획서 DB의 문서만 본다: 부모가 DB이고 그 DB에 개요/상태/담당자/마지막 수정일 열이 하나라도 있어야 한다. 아니면 건너뛴다.
- 값은 AI가 짓는 것과 사실로 정하는 것을 나눈다.
    AI: 기획서 이름(제목이 비었을 때), 개요, 상태(DB의 선택지 중에서만)
    사실: 담당자(완료한 사람), 마지막 수정일(노션이 기록한 마지막 수정 시각)
- 같은 문서에 같은 항목이 비어 있는 동안에는 다시 묻지 않는다. 기록은 문서마다 하나 (PROPERTY_CHECK_COLLECTION).
- 검사는 부가 기능이므로 실패해도 동기화 결과에는 영향을 주지 않는다 (run).
"""
import logging
import uuid
from datetime import datetime
from typing import Dict, List, Optional
from qdrant_client.models import PointStruct
import backlog
import clients
import config
import notion_parser
import notion_writer

logger = logging.getLogger(__name__)

NAME, SUMMARY, STATUS, OWNER, MODIFIED = "name", "summary", "status", "owner", "modified"
ORDER = [NAME, SUMMARY, STATUS, OWNER, MODIFIED]
# 항목마다 값을 쓸 수 있는 노션 속성 종류
ACCEPTED_TYPES = {NAME: ("title",), SUMMARY: ("rich_text",), STATUS: ("status", "select"), OWNER: ("people",), MODIFIED: ("date",)}
# AI가 짓는 항목
AI_KEYS = (NAME, SUMMARY, STATUS)

# 기록 상태
OPEN, APPLIED, SKIPPED, DONE = "open", "applied", "skipped", "done"
# 비어 있는 이유: 값이 없음 / DB에 그 속성이 없거나 종류가 달라 자동 입력할 수 없음
EMPTY, UNSUPPORTED = "empty", "unsupported"

DOC_CHARS = 12000
SUMMARY_CHARS = 2000

PROMPT = (
    "너는 게임 기획팀의 기획서 정리 담당 '사서'다. 노션 기획서의 비어 있는 속성 값을 문서 내용을 보고 제안한다.\n"
    "- name: 기획서 이름. 문서가 무엇을 정의하는지 보이는 짧은 명사구 (30자 이내). 요청받았을 때만 쓴다.\n"
    "- summary: 개요. 이 문서가 무엇을 정의하는지 2~3문장. 문서에 적힌 사실만 쓰고 추측·평가·제안은 쓰지 마라. "
    "본문이 거의 없으면 그렇다고 쓴다. 요청받았을 때만 쓴다.\n"
    "- status: 상태. [상태 후보] 중에서 문서 내용으로 판단되는 하나. 판단할 근거가 없으면 빈 문자열. "
    "후보에 없는 값은 쓰지 마라. 요청받았을 때만 쓴다.\n"
    "요청받지 않은 항목은 빈 문자열로 둔다. JSON만 출력한다: {\"name\": \"\", \"summary\": \"\", \"status\": \"\"}"
)


# --- [검사] ---
def label(key: str) -> str:
    """슬랙에 보여줄 항목 이름: 노션 열 이름 그대로 (기획서 이름은 제목 속성이라 고정)"""
    return "기획서 이름" if key == NAME else property_names()[key]


def property_names() -> Dict[str, str]:
    """제목이 아닌 항목의 노션 속성 이름"""
    return {SUMMARY: config.PROPERTY_SUMMARY, STATUS: config.PROPERTY_STATUS,
            OWNER: config.PROPERTY_OWNER, MODIFIED: config.PROPERTY_MODIFIED}


def database_schema(page: Dict, token: str) -> Dict[str, Dict]:
    """문서가 들어 있는 DB의 속성 정의 {속성 이름: {"type", ...}}. 부모가 DB가 아니거나 읽지 못하면 빈 dict"""
    parent = page.get("parent", {}) if page else {}
    if parent.get("type") != "database_id":
        return {}
    database = notion_parser.fetch_object("database_id", parent["database_id"], token)
    return (database or {}).get("properties", {})


def is_planning_db(schema: Dict[str, Dict]) -> bool:
    """개요/상태/담당자/마지막 수정일 열이 하나라도 있는 DB만 기획서 DB로 본다 (몹/아이템 같은 다른 DB의 항목은 검사하지 않는다)"""
    return any(name in schema for name in property_names().values())


def title_property(schema: Dict[str, Dict]) -> Optional[str]:
    return next((name for name, definition in schema.items() if definition.get("type") == "title"), None)


def is_empty(prop: Dict) -> bool:
    kind = (prop or {}).get("type")
    if kind in ("title", "rich_text"):
        return not notion_parser.plain_text(prop.get(kind, []))
    if kind in ("select", "status"):
        return not prop.get(kind)
    if kind == "people":
        return not prop.get("people")
    if kind == "date":
        return not (prop.get("date") or {}).get("start")
    return False


def inspect(page: Dict, schema: Dict[str, Dict]) -> List[Dict]:
    """비어 있는 항목 [{"key", "label", "property", "reason"}] (ORDER 순서).
    마지막 수정일이 노션이 자동으로 채우는 '마지막 수정 시간' 종류면 비어 있을 수 없으므로 검사하지 않는다."""
    props = page.get("properties", {})
    missing = []
    title_name = title_property(schema)
    if title_name and is_empty(props.get(title_name, {})):
        missing.append({"key": NAME, "label": label(NAME), "property": title_name, "reason": EMPTY})
    for key, name in property_names().items():
        definition = schema.get(name)
        if definition is None:
            missing.append({"key": key, "label": label(key), "property": name, "reason": UNSUPPORTED})
        elif definition.get("type") == "last_edited_time":
            continue
        elif definition.get("type") not in ACCEPTED_TYPES[key]:
            missing.append({"key": key, "label": label(key), "property": name, "reason": UNSUPPORTED})
        elif is_empty(props.get(name, {})):
            missing.append({"key": key, "label": label(key), "property": name, "reason": EMPTY})
    return sorted(missing, key=lambda m: ORDER.index(m["key"]))


# --- [제안값] ---
def status_options(schema: Dict[str, Dict]) -> List[str]:
    definition = schema.get(config.PROPERTY_STATUS, {})
    return [o["name"] for o in definition.get(definition.get("type"), {}).get("options", [])]


def kst_date(iso_time: str) -> str:
    """노션의 UTC 시각 → 한국 날짜 (YYYY-MM-DD)"""
    return datetime.fromisoformat(iso_time.replace("Z", "+00:00")).astimezone(backlog.KST).date().isoformat()


def ai_values(doc_data: Dict, wanted: List[str], options: List[str]) -> Dict[str, str]:
    """문서를 읽고 wanted(이름/개요/상태) 값을 짓는다. 상태는 선택지에 있는 값만 받는다"""
    import review
    request = (f"[요청 항목] {', '.join(wanted)}\n[상태 후보] {', '.join(options) or '(없음)'}\n"
               f"[문서 제목] {doc_data.get('title', '')}\n[노션 위치] {' > '.join(doc_data.get('path', [])) or '(최상위)'}\n\n"
               f"[본문]\n{doc_data.get('full_text', '')[:DOC_CHARS]}")
    response = clients.get_openai().responses.create(
        model=config.REFINE_MODEL, instructions=PROMPT, reasoning={"effort": config.REFINE_REASONING_EFFORT}, input=request)
    data = review.parse_json(response.output_text or "")
    values = {key: str(data.get(key) or "").strip() for key in wanted}
    if values.get(STATUS) not in options:
        values.pop(STATUS, None)
    if SUMMARY in values:
        values[SUMMARY] = values[SUMMARY][:SUMMARY_CHARS]
    return {key: value for key, value in values.items() if value}


def build_proposal(page: Dict, doc_data: Dict, missing: List[Dict], schema: Dict[str, Dict], requested_by: Optional[str]) -> Dict[str, Dict]:
    """채울 수 있는 항목마다 {"text": 슬랙에 보여줄 글, "value": 노션에 쓸 값}. 값을 정하지 못한 항목은 넣지 않는다"""
    keys = [m["key"] for m in missing if m["reason"] == EMPTY]
    proposal = {}
    wanted = [k for k in keys if k in AI_KEYS]
    if wanted:
        for key, value in ai_values(doc_data, wanted, status_options(schema)).items():
            proposal[key] = {"text": value, "value": value}
    if OWNER in keys:
        owner = requested_by or (page.get("created_by") or {}).get("id")
        if owner:
            info = notion_writer.user_info(owner)
            proposal[OWNER] = {"text": info.get("name") or "(노션 사용자)", "value": owner}
    if MODIFIED in keys and page.get("last_edited_time"):
        day = kst_date(page["last_edited_time"])
        proposal[MODIFIED] = {"text": day, "value": day}
    return proposal


def notion_property(column_type: str, value: str) -> Dict:
    """노션 속성 종류에 맞는 쓰기 값"""
    if column_type == "title":
        return {"title": notion_writer.rich_text(value)}
    if column_type == "rich_text":
        return {"rich_text": notion_writer.rich_text(value)}
    if column_type in ("select", "status"):
        return {column_type: {"name": value}}
    if column_type == "people":
        return {"people": [{"id": value}]}
    if column_type == "date":
        return {"date": {"start": value}}
    raise ValueError(f"쓸 수 없는 속성 종류: {column_type}")


def notion_properties(proposal: Dict[str, Dict], missing: List[Dict], schema: Dict[str, Dict]) -> Dict[str, Dict]:
    """지금도 비어 있는 항목에만 값을 써서 {속성 이름: 노션 값}. 그새 누가 채운 항목은 덮어쓰지 않는다"""
    return {m["property"]: notion_property(schema[m["property"]]["type"], proposal[m["key"]]["value"])
            for m in missing if m["reason"] == EMPTY and m["key"] in proposal}


# --- [기록: 문서마다 하나] ---
def record_id(page_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"property-check:{page_id}"))


def get_record(page_id: str) -> Optional[Dict]:
    points = clients.get_qdrant().retrieve(collection_name=config.PROPERTY_CHECK_COLLECTION, ids=[record_id(page_id)])
    return points[0].payload if points else None


def save_record(record: Dict) -> None:
    clients.get_qdrant().upsert(collection_name=config.PROPERTY_CHECK_COLLECTION,
                                points=[PointStruct(id=record_id(record["page_id"]), vector={}, payload=record)])


def update_record(page_id: str, **fields) -> None:
    clients.get_qdrant().set_payload(collection_name=config.PROPERTY_CHECK_COLLECTION, payload=fields, points=[record_id(page_id)])


def close_record(page_id: str, status: str, user_id: Optional[str] = None) -> None:
    update_record(page_id, status=status, closed_by=user_id, closed_at=datetime.now(backlog.KST).isoformat())


# --- [실행] ---
def run(doc_data: Dict, token: str, requested_by: Optional[str]) -> Optional[Dict]:
    """동기화가 끝난 문서의 속성을 검사해 비어 있으면 슬랙으로 묻는다. 보낸 기록을 돌려준다 (보내지 않았으면 None).
    실패해도 동기화에는 영향을 주지 않는다."""
    try:
        return _run(doc_data, token, requested_by)
    except Exception:
        logger.exception("🚨 [속성 검사] 실패 (page_id: %s)", doc_data.get("page_id"))
        return None


def _run(doc_data: Dict, token: str, requested_by: Optional[str]) -> Optional[Dict]:
    page_id = doc_data["page_id"]
    page = notion_parser.fetch_page(page_id, token)
    schema = database_schema(page, token) if page else {}
    if not is_planning_db(schema):
        # 알림이 안 오는 이유를 운영 로그에서 찾을 수 있게 남긴다
        logger.info("ℹ️ [속성 검사] '%s' 건너뜀: %s", doc_data.get("title"),
                    "노션 문서를 읽지 못함" if not page else
                    "기획서 DB의 문서가 아님 (DB 밖의 문서이거나, 연동에 그 DB가 공유되지 않았거나, DB에 개요/상태/담당자/마지막 수정일 열이 없음)")
        return None

    missing = inspect(page, schema)
    existing = get_record(page_id)
    if not missing:
        if existing and existing["status"] == OPEN:
            # 묻고 있던 항목을 그새 노션에서 직접 채웠다
            import property_slack
            property_slack.close_message(existing, "✅ 노션에 이미 채워져 있어요.")
            close_record(page_id, DONE)
        return None

    keys = [m["key"] for m in missing]
    if existing and existing["status"] in (OPEN, SKIPPED) and sorted(existing["missing"]) == sorted(keys):
        return None
    if not any(m["reason"] == EMPTY for m in missing):
        logger.info("ℹ️ [속성 검사] '%s': DB에 없는 속성(%s)뿐이라 묻지 않습니다.", doc_data.get("title"), ", ".join(keys))
        return None

    proposal = build_proposal(page, doc_data, missing, schema, requested_by)
    record = {"page_id": page_id, "title": notion_parser.page_title(page) or "", "url": page.get("url", ""),
              "missing": keys, "proposal": proposal, "status_options": status_options(schema), "status": OPEN,
              "notified_at": datetime.now(backlog.KST).isoformat()}
    import property_slack
    sent = property_slack.notify(record, missing, requested_by)
    if not sent:
        return None
    record.update(sent)
    save_record(record)
    return record
