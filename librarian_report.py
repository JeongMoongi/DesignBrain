"""사서 활동 기록과 주간 요약: 사서가 모델을 쓸 때마다 작업별 토큰을 남기고(config.LIBRARIAN_USAGE_COLLECTION),
질문 기록 주간 요약 끝에 한 주 동안 낸 제안·처리 결과·반려 사유·토큰을 붙인다."""
import logging
import uuid
from collections import Counter
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from qdrant_client.models import PointStruct
import backlog
import clients
import config

logger = logging.getLogger(__name__)

# 사서 작업 이름 (토큰 기록과 주간 요약에 쓴다)
REFINE, REVIEW, SYNTHESIS, REVISE, RECHECK, MAP_UPDATE = "정리", "검토", "종합", "대화로 고치기", "갱신 대조", "지도 갱신 검토"
TASK_ORDER = [REFINE, REVIEW, SYNTHESIS, REVISE, RECHECK, MAP_UPDATE]
# 주간 요약에 보여줄 최대 반려 사유 수
REPORT_REJECTS = 10


def usage_tokens(usages) -> tuple:
    """모델 응답 usage 목록의 (입력, 출력) 토큰 합"""
    usages = [u for u in usages if u]
    return sum(u.input_tokens for u in usages), sum(u.output_tokens for u in usages)


def record_usage(task: str, title: str, input_tokens: int, output_tokens: int) -> None:
    """사서가 모델을 쓴 기록 하나를 남긴다. 기록에 실패해도 사서 작업은 계속한다."""
    now = datetime.now(backlog.KST)
    payload = {"task": task, "title": title, "model": config.REFINE_MODEL, "input_tokens": input_tokens,
               "output_tokens": output_tokens, "at": now.isoformat(timespec="seconds"), "at_ts": now.timestamp()}
    try:
        clients.get_qdrant().upsert(collection_name=config.LIBRARIAN_USAGE_COLLECTION,
                                    points=[PointStruct(id=str(uuid.uuid4()), vector={}, payload=payload)])
    except Exception as e:
        logger.warning("⚠️ [사서] 토큰 기록 실패 (작업에는 영향 없음): %s", e)


def scroll_all(collection: str) -> List[Dict]:
    found, offset = [], None
    while True:
        points, offset = clients.get_qdrant().scroll(collection_name=collection, limit=256, offset=offset, with_payload=True)
        found.extend(p.payload for p in points)
        if offset is None:
            return found


def in_period(value: Optional[str], since: datetime, until: datetime) -> bool:
    if not value:
        return False
    return since <= datetime.fromisoformat(value) < until


def held_at(record: Dict) -> Optional[str]:
    """보류한 시각 (보류는 다시 물을 날짜만 저장하므로 거기서 계산한다)"""
    if not record.get("held_until"):
        return None
    return (datetime.fromisoformat(record["held_until"]) - timedelta(days=backlog.HOLD_DAYS)).isoformat()


def weekly_section(proposals: List[Dict], usage: List[Dict], since: datetime, until: datetime) -> str:
    """주간 요약에 붙이는 사서 활동 부분. 제안도 토큰 기록도 없으면 빈 문자열"""
    created = [p for p in proposals if in_period(p.get("created_at"), since, until)]
    closed = [p for p in proposals if in_period(p.get("resolved_at"), since, until)]
    held = [p for p in proposals if in_period(held_at(p), since, until)]
    usage = [u for u in usage if since.timestamp() <= u.get("at_ts", 0) < until.timestamp()]
    if not (created or closed or held or usage):
        return ""

    lines = ["\n🧹 *사서 활동*"]
    types = Counter(p.get("proposal_type") for p in created)
    lines.append(f"새 제안 {len(created)}건 (결정 {types[backlog.DECISION]} · 작성 {types[backlog.WRITING]})")

    by_status = Counter(p["status"] for p in closed)
    approved = [p for p in closed if p["status"] == backlog.RESOLVED]
    # 고쳐서 통과: 승인 창에서 고쳤거나, 대화로 고친 뒤(제안 내용이 고친 판으로 바뀐다) 통과한 것
    edited = sum(1 for p in approved
                 if p.get("approved_text", "").strip() != p.get("proposal", "").strip() or p.get("chat_history"))
    lines.append(f"처리: 승인 {len(approved)}" + (f" (고쳐서 {edited})" if edited else "") +
                 f" · 보류 {len(held)} · 반려 {by_status[backlog.REJECTED]}"
                 f" · 문서 갱신으로 닫힘 {by_status[backlog.APPLIED] + by_status[backlog.OBSOLETE]}")
    unsettled = Counter(p["status"] for p in proposals if p.get("status") in backlog.UNSETTLED)
    lines.append(f"남은 제안: 대기 {unsettled[backlog.OPEN]} · 보류 {unsettled[backlog.HELD]}")

    rejected = [p for p in closed if p["status"] == backlog.REJECTED]
    if rejected:
        lines.append("\n*반려 사유*")
        lines += [f"• {p.get('content', '')[:60]} — {p.get('reject_reason') or '(사유 없음)'}" for p in rejected[-REPORT_REJECTS:]]

    if usage:
        lines.append(f"\n*토큰* (입력 / 출력, {config.REFINE_MODEL})")
        totals = {}
        for u in usage:
            count, inp, out = totals.get(u["task"], (0, 0, 0))
            totals[u["task"]] = (count + 1, inp + u.get("input_tokens", 0), out + u.get("output_tokens", 0))
        for task in sorted(totals, key=lambda t: TASK_ORDER.index(t) if t in TASK_ORDER else len(TASK_ORDER)):
            count, inp, out = totals[task]
            lines.append(f"• {task} {count}회: {inp:,} / {out:,}")
        lines.append(f"합계: {sum(t[1] for t in totals.values()):,} / {sum(t[2] for t in totals.values()):,}")
    return "\n".join(lines)


def weekly_report(since: datetime, until: datetime) -> str:
    proposals = [r for r in scroll_all(config.BACKLOG_COLLECTION) if r.get("kind") == backlog.PROPOSAL]
    return weekly_section(proposals, scroll_all(config.LIBRARIAN_USAGE_COLLECTION), since, until)
