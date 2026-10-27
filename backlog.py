"""백로그: 기획서와 실제 결정 사항의 차이를 기록해 두었다가, 관련 질문의 답변 끝에 참고 문구로 붙이고
담당자에게 해결될 때까지 매일 확인 DM을 보낸다. 기획서와 별도 컬렉션(config.BACKLOG_COLLECTION)에 저장한다.

사람이 등록한 백로그 외에, 사서가 '완료'된 문서를 검토해 낸 제안(kind=proposal)도 같은 곳에 둔다.
제안은 담당자가 [이대로 통과]/[고쳐서 통과]하면 노션에 반영되고, [보류]/[반려]하면 닫힌다."""
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from qdrant_client.models import Filter, FieldCondition, MatchAny, MatchValue, PointStruct
import clients
import config

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
OPEN, RESOLVED = "open", "resolved"
# 사서 제안의 상태 (RESOLVED는 승인되어 노션에 반영됨).
# APPLIED/OBSOLETE는 문서가 갱신됐을 때 사서가 대조해 닫은 것: 문서 수정으로 해결됨 / 가리키던 부분이 바뀌어 무효
HELD, REJECTED = "held", "rejected"
APPLIED, OBSOLETE = "applied", "obsolete"
# 보류는 '맞지만 지금은 못 함': 이 기간 동안 리마인더에서 쉬었다가 다시 묻는다. 그동안에도 답변 참고 문구와 문서 갱신 대조에는 남는다
HOLD_DAYS = 14
# 아직 결론이 나지 않은 상태 (답변 참고 문구, 문서 갱신 대조, 담당자 버튼 처리 대상)
UNSETTLED = [OPEN, HELD]
PROPOSAL = "proposal"
# 제안 종류: 원본 문서의 결정 기록에 덧붙일 결정 / 새로 만들 문서
DECISION, WRITING = "decision", "writing"
# 무엇을 찾았나: 문서끼리 어긋남 / 비어 있음 / 흩어진 주제 (슬랙 한 줄 요약의 아이콘)
CONFLICT, MISSING, TOPIC = "conflict", "missing", "topic"

# 답변에 붙일 백로그 선택 기준 (질문 벡터와 백로그 내용의 코사인 유사도).
# 실측(2026-10-13): 관련 질문 0.40~0.54, 같은 기획서의 무관한 질문 0.27, 다른 주제 0.10~0.21
# - 답변이 참고한 기획서에 연결된 백로그: 질문과 어느 정도만 관련 있으면 붙인다
# - 연결된 기획서가 다른 백로그: 질문과 내용이 강하게 겹칠 때만 붙인다
LINKED_MIN_SCORE = 0.33
UNLINKED_MIN_SCORE = 0.40
MAX_NOTES_PER_ANSWER = 3
# 답변 끝의 참고 문구에서 '기획자 확인 대기' 항목은 이 머리말을 한 번만 보여주고 묶는다 (ai_engine.format_notes)
WAITING_NOTE_PREFIX = "아직 기획자 확인을 기다리는 부분이에요 — "

# 슬랙에서 쓰는 백로그 명령 키워드와 등록 안내 문구 (대화 기록에서 이 메세지들을 거를 때도 쓴다)
BACKLOG_KEYWORD = "백로그"
BACKLOG_CONFIRM_PREFIX = "📝 백로그를 등록했어요"

# 슬랙 멘션 형식: <@U123> 또는 <@U123|이름>
MENTION_PATTERN = re.compile(r"^\s*<@([UW][A-Z0-9]+)(?:\|[^>]*)?>\s*(.*)$", re.S)
# 노션 페이지 URL 끝의 32자리 ID
NOTION_PAGE_ID_PATTERN = re.compile(r"notion\.(?:so|com)/[^\s|>)]*?([0-9a-f]{32})(?![0-9a-f])")


def parse_command(text: str):
    """'<@담당자> 내용' 을 (담당자 ID, 내용)으로. 형식이 틀리면 None"""
    m = MENTION_PATTERN.match(text or "")
    if not m or not m.group(2).strip():
        return None
    return m.group(1), m.group(2).strip()


def page_ids_in_text(text: str) -> List[str]:
    """메세지에 들어 있는 노션 링크에서 페이지 ID(하이픈 형식)를 순서대로 추출"""
    ids = []
    for raw in NOTION_PAGE_ID_PATTERN.findall(text or ""):
        page_id = str(uuid.UUID(raw))
        if page_id not in ids:
            ids.append(page_id)
    return ids


def linked_docs(page_ids: List[str]) -> List[Dict[str, str]]:
    """인덱스에 있는 문서만 골라 제목/URL과 함께 돌려준다 (링크는 있지만 인덱싱되지 않은 문서는 제외)"""
    docs = []
    for page_id in page_ids:
        points = clients.get_qdrant().scroll(
            collection_name=config.COLLECTION_NAME, limit=1, with_payload=["title", "url"],
            scroll_filter=Filter(must=[FieldCondition(key="page_id", match=MatchValue(value=page_id))]),
        )[0]
        if points:
            docs.append({"page_id": page_id, "title": points[0].payload.get("title", ""), "url": points[0].payload.get("url", "")})
    return docs


NOTE_ENDING = "확인하셨습니다."
# 참고 문구가 원문 단어를 이만큼 이상 그대로 담고 있어야 한다 (요약하다 사실이 빠지는 것을 막음)
NOTE_MIN_WORD_COVERAGE = 0.8
NOTE_EXAMPLES = [
    ("UI 팝업 닫기 버튼 위치가 우상단으로 바뀌었는데 공통 UI 기획서는 아직 좌상단으로 되어 있음",
     "UI 팝업 닫기 버튼 위치가 우상단으로 바뀌었는데 공통 UI 기획서는 아직 좌상단으로 되어 있는 것을 확인하셨습니다."),
    ("파트너 영입 비용 300골드로 확정. 리더/파트너 기획서 수치 수정 필요",
     "파트너 영입 비용이 300골드로 확정되어 리더/파트너 기획서의 수치 수정이 필요한 것을 확인하셨습니다."),
    # '발견함/확인함'은 '확인하셨습니다'와 뜻이 겹치므로 떼어 낸다
    ("Tank 돌진 공격에 넉백을 넣기로 했었는데 챕터 3 몹 기획서에는 빠져 있는 것을 발견함",
     "Tank 돌진 공격에 넉백을 넣기로 했었는데 챕터 3 몹 기획서에는 빠져 있는 것을 확인하셨습니다."),
    ("상점 갱신 비용은 회차마다 초기화하기로 함",
     "상점 갱신 비용은 회차마다 초기화하기로 한 것을 확인하셨습니다."),
]

def note_covers_content(note: str, content: str) -> bool:
    words = [w for w in re.findall(r"[0-9A-Za-z가-힣_]+", content) if len(w) > 1]
    if not words:
        return True
    # 어미는 바뀔 수 있으므로 단어 앞 2글자로 비교
    return sum(1 for w in words if w[:2] in note) / len(words) >= NOTE_MIN_WORD_COVERAGE

def write_note(reporter_name: Optional[str], content: str) -> str:
    """답변 끝에 붙일 참고 문구를 등록 시점에 한 번 만들어 저장한다 (답변마다 문구가 달라지지 않도록).
    형식이 틀리거나 원문 내용이 빠지면 원문을 그대로 쓰는 기본 문구를 사용한다."""
    subject = f"{reporter_name} 님이" if reporter_name else "팀원이"
    fallback = f"{subject} 남긴 메모: {content}"
    messages = [{"role": "system", "content": (
        "기획팀 슬랙 봇이 답변 끝에 덧붙일 참고 문구의 본문을 만든다. 백로그 메모를 '~것을 확인하셨습니다.'로 끝나는 한 문장으로 바꿔라.\n"
        "메모의 단어(기획서 이름, 대상, 수치, 결정 내용)는 하나도 빼거나 바꾸지 말고 어미만 자연스럽게 고친다. "
        "새로운 내용을 덧붙이지 말고, 주어는 쓰지 말고, 문장만 출력한다."
    )}]
    for memo, sentence in NOTE_EXAMPLES:
        messages += [{"role": "user", "content": memo}, {"role": "assistant", "content": sentence}]
    messages.append({"role": "user", "content": content})
    try:
        completion = clients.get_openai().chat.completions.create(model=config.CHAT_MODEL, temperature=0, messages=messages)
        body = completion.choices[0].message.content.strip()
    except Exception as e:
        logger.warning("백로그 참고 문구 생성 실패, 기본 문구 사용: %s", e)
        return fallback
    # 한 문장('~습니다.'가 한 번)이어야 한다: '…발견하셨습니다. 확인하셨습니다.' 같은 중복 어미를 거른다
    if not body.endswith(NOTE_ENDING) or body.count("습니다") != 1 or not note_covers_content(body, content):
        logger.warning("백로그 참고 문구가 형식/내용 검증을 통과하지 못해 기본 문구를 사용합니다: %s", body)
        return fallback
    return f"{subject} {body}"


def add(reporter_id: str, reporter_name: Optional[str], assignee_id: str, assignee_name: Optional[str],
        content: str, docs: List[Dict[str, str]], source: Dict[str, str]) -> Dict:
    backlog_id = str(uuid.uuid4())
    record = {
        "backlog_id": backlog_id,
        "content": content,
        "note": write_note(reporter_name, content),
        "reporter_id": reporter_id,
        "reporter_name": reporter_name,
        "assignee_id": assignee_id,
        "assignee_name": assignee_name,
        "page_ids": [d["page_id"] for d in docs],
        "docs": docs,
        "status": OPEN,
        "created_at": datetime.now(KST).isoformat(),  # 같은 초에 등록돼도 순서가 유지되도록 마이크로초까지
        "source": source,
    }
    vector = clients.get_embedding(content)
    clients.get_qdrant().upsert(collection_name=config.BACKLOG_COLLECTION,
                                  points=[PointStruct(id=backlog_id, vector=vector, payload=record)])
    logger.info("📝 [백로그 등록] %s | 담당자: %s | 기획서: %s", content, assignee_id, [d["title"] for d in docs])
    return record


def notes_for_answer(query_vector: List[float], answer_page_ids: List[str]) -> List[str]:
    """답변에 덧붙일 미해결 백로그 참고 문구 (관련도 높은 순)"""
    hits = clients.get_qdrant().query_points(
        collection_name=config.BACKLOG_COLLECTION, query=query_vector, limit=10, with_payload=True,
        query_filter=Filter(must=[FieldCondition(key="status", match=MatchAny(any=UNSETTLED))]),
    ).points
    answer_pages = set(answer_page_ids)
    notes, shown = [], []
    for hit in hits:
        linked = bool(answer_pages & set(hit.payload.get("page_ids", [])))
        if hit.score >= (LINKED_MIN_SCORE if linked else UNLINKED_MIN_SCORE):
            notes.append(hit.payload["note"])
            shown.append(hit.id)
        if len(notes) >= MAX_NOTES_PER_ANSWER:
            break
    # 팀원 질문에 나온 백로그는 리마인더에서 먼저 묻는다 (기록 실패는 답변에 영향을 주지 않는다)
    if shown:
        try:
            clients.get_qdrant().set_payload(collection_name=config.BACKLOG_COLLECTION, points=shown,
                                             payload={"last_answer_at": datetime.now(KST).isoformat()})
        except Exception as e:
            logger.warning("⚠️ [백로그] 답변 표시 기록 실패: %s", e)
    return notes


def get(backlog_id: str) -> Optional[Dict]:
    points = clients.get_qdrant().retrieve(collection_name=config.BACKLOG_COLLECTION, ids=[backlog_id], with_payload=True)
    return points[0].payload if points else None


def open_by_assignee() -> Dict[str, List[Dict]]:
    """담당자 ID별 미해결 백로그 (오래된 순)"""
    grouped, offset = {}, None
    while True:
        points, offset = clients.get_qdrant().scroll(
            collection_name=config.BACKLOG_COLLECTION, limit=256, offset=offset, with_payload=True,
            scroll_filter=Filter(must=[FieldCondition(key="status", match=MatchValue(value=OPEN))]),
        )
        for p in points:
            grouped.setdefault(p.payload["assignee_id"], []).append(p.payload)
        if offset is None:
            break
    for items in grouped.values():
        items.sort(key=lambda r: r["created_at"])
    return grouped


def resolve(backlog_id: str, user_id: str) -> str:
    """담당자만 해결 처리할 수 있다. 결과: resolved / already / not_assignee / not_found"""
    result = check_open(backlog_id, user_id)
    if result != "ok":
        return result
    close(backlog_id, user_id, RESOLVED)
    return "resolved"


def check_open(backlog_id: str, user_id: str) -> str:
    """담당자가 처리할 수 있는 미해결(보류 포함) 백로그인지: ok / already / not_assignee / not_found"""
    record = get(backlog_id)
    if record is None:
        return "not_found"
    if record["assignee_id"] != user_id:
        return "not_assignee"
    if record["status"] not in UNSETTLED:
        return "already"
    return "ok"


def close(backlog_id: str, user_id: str, status: str, **fields) -> None:
    """백로그를 닫는다 (해결/승인, 보류, 반려). fields는 승인한 글, 반려 사유, 노션 반영 결과 등"""
    clients.get_qdrant().set_payload(
        collection_name=config.BACKLOG_COLLECTION, points=[backlog_id],
        payload={"status": status, "resolved_at": datetime.now(KST).isoformat(timespec="seconds"), "resolved_by": user_id, **fields},
    )
    logger.info("✅ [백로그 %s] %s (처리: %s)", status, backlog_id, user_id)


def add_proposal(finding: Dict, target: Dict[str, str], related: List[Dict[str, str]],
                 assignee_id: Optional[str], assignee_name: Optional[str], review_id: str) -> Dict:
    """사서 검토에서 나온 제안 하나를 저장한다. target은 검토한 문서 {"page_id", "title", "url"}.
    finding: {"type": decision|writing, "title", "summary", "category", "problem", "proposal", "new_doc_title"}"""
    backlog_id = str(uuid.uuid4())
    title = finding["title"]
    record = {
        "backlog_id": backlog_id,
        "kind": PROPOSAL,
        "proposal_type": finding["type"],
        "content": title,
        # 슬랙에서 맨 위에 보이는 한 줄 요약과 그 종류 (예전 제안에는 없어 제목을 대신 보여준다)
        "summary": finding.get("summary", ""),
        "category": finding.get("category", ""),
        "problem": finding.get("problem", ""),
        "proposal": finding.get("proposal", ""),
        # 사서가 근거 없이 새로 정한 부분 (승인 전에 확인하도록 슬랙에 따로 보여준다)
        "assumptions": finding.get("assumptions", ""),
        "new_doc_title": finding.get("new_doc_title", ""),
        # 답변 끝 참고 문구: 이 부분은 아직 기획자 확인을 기다린다는 것을 알린다
        "note": f"{WAITING_NOTE_PREFIX}{title}",
        "reporter_id": "",
        # 슬랙에 보이는 이름 (사서는 기능을 가리키는 내부 이름이라 사용자에게는 '사서'로 보인다)
        "reporter_name": "사서",
        "assignee_id": assignee_id or "",
        "assignee_name": assignee_name,
        "page_ids": [target["page_id"]],
        "docs": [target] + [d for d in related if d["page_id"] != target["page_id"]],
        # 근거 문서 전부. 승인된 지도(종합) 문서는 이 원본들이 바뀌면 갱신을 검토한다 (synthesis.check_dependents)
        "source_page_ids": list(dict.fromkeys([target["page_id"]] + [d["page_id"] for d in related])),
        "status": OPEN,
        "created_at": datetime.now(KST).isoformat(),
        "source": {"review_id": review_id},
    }
    vector = clients.get_embedding(f"{title}\n{record['problem']}")
    clients.get_qdrant().upsert(collection_name=config.BACKLOG_COLLECTION,
                                points=[PointStruct(id=backlog_id, vector=vector, payload=record)])
    return record


# 사서 제안은 쌓인 것 중 이 비율만(최소/최대 개수 안에서) 매일 묻는다. 사람이 등록한 백로그는 모두 묻는다
REMINDER_PROPOSAL_RATIO = 0.1
REMINDER_PROPOSAL_MIN = 1
REMINDER_PROPOSAL_MAX = 5
# 이 기간 안에 답변 참고 문구로 나온 제안(팀원들이 실제로 궁금해한 부분)을 먼저 묻는다
RECENT_ANSWER_DAYS = 7


def reminder_selection(records: List[Dict], now: Optional[datetime] = None) -> tuple:
    """담당자 한 명의 미해결 백로그에서 오늘 물을 것을 고른다. (고른 것, 사서 제안 전체 수)
    사서 제안 순서: 최근 답변에 나온 것 → 가장 오래 묻지 않은 것 → 오래된 것 (돌아가며 모두 묻게 된다)"""
    now = now or datetime.now(KST)
    people = [r for r in records if r.get("kind") != PROPOSAL]
    proposals = [r for r in records if r.get("kind") == PROPOSAL]
    if not proposals:
        return people, 0
    count = min(REMINDER_PROPOSAL_MAX, max(REMINDER_PROPOSAL_MIN, int(len(proposals) * REMINDER_PROPOSAL_RATIO)))
    recent = (now - timedelta(days=RECENT_ANSWER_DAYS)).isoformat()
    proposals.sort(key=lambda r: (not (r.get("last_answer_at", "") >= recent), r.get("last_reminded_at", ""), r["created_at"]))
    return people + proposals[:count], len(proposals)


def mark_reminded(records: List[Dict]) -> None:
    ids = [r["backlog_id"] for r in records if r.get("kind") == PROPOSAL]
    if ids:
        clients.get_qdrant().set_payload(collection_name=config.BACKLOG_COLLECTION, points=ids,
                                         payload={"last_reminded_at": datetime.now(KST).isoformat()})


def open_for_page(page_id: str) -> List[Dict]:
    """이 문서에 연결된 미해결(보류 포함) 백로그 (사람이 등록한 것과 사서 제안 모두)"""
    points, offset, records = [], None, []
    while True:
        points, offset = clients.get_qdrant().scroll(
            collection_name=config.BACKLOG_COLLECTION, limit=256, offset=offset, with_payload=True,
            scroll_filter=Filter(must=[FieldCondition(key="page_ids", match=MatchValue(value=page_id)),
                                       FieldCondition(key="status", match=MatchAny(any=UNSETTLED))]))
        records += [p.payload for p in points]
        if offset is None:
            return sorted(records, key=lambda r: r["created_at"])


def hold(backlog_id: str, user_id: str) -> str:
    """보류: HOLD_DAYS 뒤 리마인더에 다시 나오도록 한다. 다시 물을 날짜(YYYY-MM-DD)를 돌려준다"""
    until = datetime.now(KST) + timedelta(days=HOLD_DAYS)
    update(backlog_id, status=HELD, held_by=user_id, held_until=until.isoformat())
    return f"{until:%Y-%m-%d}"


def reopen_expired_holds(now: Optional[datetime] = None) -> int:
    """보류 기간이 끝난 제안을 다시 미해결로 돌린다 (리마인더 순환에 다시 들어간다). 기간 없이 보류된 예전 기록은 그대로 둔다"""
    now = (now or datetime.now(KST)).isoformat()
    points, offset, expired = [], None, []
    while True:
        points, offset = clients.get_qdrant().scroll(
            collection_name=config.BACKLOG_COLLECTION, limit=256, offset=offset, with_payload=True,
            scroll_filter=Filter(must=[FieldCondition(key="status", match=MatchValue(value=HELD))]))
        expired += [p.id for p in points if p.payload.get("held_until") and p.payload["held_until"] <= now]
        if offset is None:
            break
    if expired:
        clients.get_qdrant().set_payload(collection_name=config.BACKLOG_COLLECTION, points=expired, payload={"status": OPEN})
    return len(expired)


def update(backlog_id: str, **fields) -> None:
    clients.get_qdrant().set_payload(collection_name=config.BACKLOG_COLLECTION, points=[backlog_id], payload=fields)


def proposal_by_chat(chat_ts: str) -> Optional[Dict]:
    """'대화로 고치기' 스레드(첫 메세지 ts)에 연결된 제안"""
    points = clients.get_qdrant().scroll(
        collection_name=config.BACKLOG_COLLECTION, limit=1, with_payload=True,
        scroll_filter=Filter(must=[FieldCondition(key="chat_ts", match=MatchValue(value=chat_ts))]))[0]
    return points[0].payload if points else None


def proposals_for_page(page_id: str) -> List[Dict]:
    """이 문서에 대해 사서가 냈던 제안 전부 (상태 무관, 오래된 순). 같은 제안을 다시 내지 않도록 검토에 넘긴다"""
    records, offset = [], None
    while True:
        points, offset = clients.get_qdrant().scroll(
            collection_name=config.BACKLOG_COLLECTION, limit=256, offset=offset, with_payload=True,
            scroll_filter=Filter(must=[FieldCondition(key="page_ids", match=MatchValue(value=page_id))]),
        )
        records += [p.payload for p in points if p.payload.get("kind") == PROPOSAL]
        if offset is None:
            return sorted(records, key=lambda r: r["created_at"])


def seconds_until_next(hour: int, now: Optional[datetime] = None) -> float:
    """다음 hour시 정각(한국 시간)까지 남은 초"""
    now = now or datetime.now(KST)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()
