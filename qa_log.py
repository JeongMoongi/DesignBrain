"""질문/답변 기록: 서비스 개선 방향을 찾기 위해 슬랙 질문마다 질문·답변·사용한 기획서·실패 여부·👍/👎 평가를 남긴다.
벡터 없이 payload만 저장하는 별도 컬렉션(config.QA_LOG_COLLECTION)에 둔다."""
import logging
import uuid
from collections import Counter
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from qdrant_client.models import Filter, FieldCondition, MatchValue, PointStruct
import backlog
import clients
import config

logger = logging.getLogger(__name__)

UP, DOWN = "up", "down"
# 슬랙 리액션 이름 → 평가 (피부색 변형 '+1::skin-tone-2'는 앞부분으로 판단)
FEEDBACK_REACTIONS = {"+1": UP, "thumbsup": UP, "-1": DOWN, "thumbsdown": DOWN}
# 주간 요약에 항목별로 보여줄 최대 질문 수
REPORT_ITEMS = 10


def feedback_of(reaction: str) -> Optional[str]:
    return FEEDBACK_REACTIONS.get(reaction.split("::", 1)[0])


def record(entry: Dict) -> None:
    """질문 한 건을 저장한다. entry에 asked_at(KST ISO)과 asked_at_ts(epoch)를 채워 넣는다."""
    now = datetime.now(backlog.KST)
    payload = {"asked_at": now.isoformat(timespec="seconds"), "asked_at_ts": now.timestamp(), "feedback": {}, **entry}
    clients.get_qdrant().upsert(collection_name=config.QA_LOG_COLLECTION,
                                points=[PointStruct(id=str(uuid.uuid4()), vector={}, payload=payload)])


def set_feedback(answer_ts: str, user_id: str, value: str, added: bool = True) -> bool:
    """답변 메세지(answer_ts)에 단 평가를 기록한다. 리액션을 지우면(added=False) 그 평가였을 때만 지운다
    (👍와 👎를 둘 다 달았다가 하나만 지워도 남은 평가는 유지). 답변 기록이 없으면 False"""
    qdrant = clients.get_qdrant()
    points = qdrant.scroll(collection_name=config.QA_LOG_COLLECTION, limit=1, with_payload=True,
                           scroll_filter=Filter(must=[FieldCondition(key="answer_ts", match=MatchValue(value=answer_ts))]))[0]
    if not points:
        return False
    feedback = dict(points[0].payload.get("feedback") or {})
    if added:
        feedback[user_id] = value
    elif feedback.get(user_id) == value:
        del feedback[user_id]
    qdrant.set_payload(collection_name=config.QA_LOG_COLLECTION, payload={"feedback": feedback}, points=[points[0].id])
    return True


def entries(since: Optional[datetime] = None, until: Optional[datetime] = None) -> List[Dict]:
    """기간 안의 기록 (오래된 순). 기록 수가 많지 않으므로 전부 읽어 거른다."""
    found, offset = [], None
    while True:
        points, offset = clients.get_qdrant().scroll(collection_name=config.QA_LOG_COLLECTION, limit=256,
                                                     offset=offset, with_payload=True)
        found.extend(p.payload for p in points)
        if offset is None:
            break
    if since:
        found = [e for e in found if e.get("asked_at_ts", 0) >= since.timestamp()]
    if until:
        found = [e for e in found if e.get("asked_at_ts", 0) < until.timestamp()]
    return sorted(found, key=lambda e: e.get("asked_at_ts", 0))


def feedback_count(entry: Dict, value: str) -> int:
    return sum(1 for v in (entry.get("feedback") or {}).values() if v == value)


def weekly_report(records: List[Dict], since: datetime, until: datetime) -> str:
    """주간 요약 문구: 다음 작업을 찾는 데 필요한 것(실패, 기획서에 없던 질문, 👎)을 먼저 보여준다"""
    period = f"{since:%m/%d} ~ {(until - timedelta(seconds=1)):%m/%d}"
    if not records:
        return f"📊 *질문 기록 주간 요약* ({period})\n이번 주에는 질문이 없었어요."

    failed = [e for e in records if e.get("status") == "error"]
    no_docs = [e for e in records if e.get("status") == "success" and not e.get("used_docs")]
    disliked = [e for e in records if feedback_count(e, DOWN)]
    askers = {e.get("user_id") for e in records}
    docs = Counter(doc for e in records for doc in e.get("used_docs", []))

    def question_lines(items):
        lines = [f"• {e.get('question', '')[:80]} — {e.get('user_name') or '알 수 없음'}" for e in items[-REPORT_ITEMS:]]
        if len(items) > REPORT_ITEMS:
            lines.insert(0, f"(최근 {REPORT_ITEMS}건만 표시, 전체 {len(items)}건)")
        return "\n".join(lines)

    sections = [
        f"📊 *질문 기록 주간 요약* ({period})",
        f"질문 {len(records)}건 · 질문한 사람 {len(askers)}명 · 👍 {sum(feedback_count(e, UP) for e in records)} · "
        f"👎 {sum(feedback_count(e, DOWN) for e in records)}",
    ]
    for title, items in (("❌ 답변 실패", failed), ("🔍 기획서에서 답을 못 찾은 질문", no_docs), ("👎 아쉬웠던 답변", disliked)):
        if items:
            sections.append(f"\n*{title}* ({len(items)}건)\n{question_lines(items)}")
    if docs:
        sections.append("\n*📚 많이 쓰인 기획서*\n" + "\n".join(f"• {doc} ({n}회)" for doc, n in docs.most_common(5)))
    sections.append("\n전체 기록은 `python export_qa.py --days 7` 로 내려받을 수 있어요.")
    return "\n".join(sections)


def seconds_until_weekly(weekday: int, hour: int, now: Optional[datetime] = None) -> float:
    """다음 weekday(월=0)요일 hour시 정각(한국 시간)까지 남은 초"""
    now = now or datetime.now(backlog.KST)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=(weekday - now.weekday()) % 7)
    if target <= now:
        target += timedelta(days=7)
    return (target - now).total_seconds()
