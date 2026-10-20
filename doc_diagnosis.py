"""문서 진단: 동기화한 기획서가 AI가 읽기 어려운 구조인지 규칙으로 찾아 기록하고, 담당자에게 알린다.
패턴을 모으는 단계라 문서를 고치거나 판단하지 않고 사실만 남긴다 (어떻게 처리할지는 모인 패턴을 보고 정한다).

진단 결과는 문서마다 최신 것 하나를 별도 컬렉션에 저장한다 → diagnosis_report.py로 전체 패턴을 모아 본다."""
import logging
from datetime import datetime
from typing import Dict, List
from qdrant_client.models import PointStruct
import backlog
import clients
import config
import search

logger = logging.getLogger(__name__)

# 진단 종류: (이름, 설명). 리포트에 이 순서로 나온다
KINDS = {
    "linked_view": "읽을 수 없는 DB 뷰",
    "empty_heading": "내용이 비어 있는 제목",
    "short_body": "본문이 거의 없음",
    "unshared_link": "읽을 수 없는 페이지 링크",
    "unshared_relation": "읽을 수 없는 관계형 참조",
    "same_title": "같은 제목의 다른 문서",
}
# 제목을 빼고 이 글자 수보다 적으면 본문이 거의 없는 문서로 본다
SHORT_BODY_CHARS = 100
DETAIL_SHOWN = 5


def empty_headings(outline: List[tuple]) -> List[str]:
    """바로 다음이 같거나 상위 단계의 제목이거나 문서 끝인 제목 (사이에 본문도 하위 제목도 없음).
    읽을 수 없는 DB 뷰만 있는 제목은 뷰로 따로 알리므로 비어 있다고 하지 않는다."""
    empty = []
    for i, event in enumerate(outline):
        if event[0] != "heading":
            continue
        following = next((e for e in outline[i + 1:] if e[0] in ("heading", "content", "linked_view")), None)
        if following is None or (following[0] == "heading" and following[1] <= event[1]):
            empty.append(event[2])
    return empty


def diagnose(doc_data: Dict, documents: Dict[str, dict]) -> List[Dict]:
    """[{"kind", "count", "details"}]. documents는 search.get_documents() (같은 제목 확인용)"""
    outline = doc_data.get("outline", [])
    findings = []

    def add(kind, details, count=None):
        if details:
            findings.append({"kind": kind, "count": count if count is not None else len(details), "details": details})

    add("linked_view", [e[1] for e in outline if e[0] == "linked_view"])
    add("empty_heading", empty_headings(outline))
    title = doc_data.get("title", "")
    body = doc_data.get("full_text", "").replace(f"# {title}", "", 1).strip()
    if len(body) < SHORT_BODY_CHARS:
        add("short_body", [f"{len(body)}자"], count=1)
    links = [e for e in outline if e[0] == "unshared_link"]
    add("unshared_link", [f"{len(links)}개"] if links else [], count=len(links))
    relations = [e for e in outline if e[0] == "unshared_relation"]
    add("unshared_relation", [f"{e[1]} ({e[2]}개)" for e in relations], count=sum(e[2] for e in relations))
    # 문서 이름은 제목이거나, 같은 제목이 여러 곳에 있으면 '상위 > 제목'이다
    others = [doc["path"] or name for name, doc in documents.items()
              if doc_data["page_id"] not in doc["page_ids"] and (name == title or name.endswith(f" > {title}"))]
    add("same_title", others)
    return findings


def format_report(doc_data: Dict, findings: List[Dict]) -> str:
    title, url = doc_data.get("title", "제목 없음"), doc_data.get("url", "")
    lines = [f"📋 *문서 진단* : <{url}|{title}>" if url else f"📋 *문서 진단* : {title}"]
    for f in findings:
        shown = ", ".join(f["details"][:DETAIL_SHOWN])
        more = f" 외 {len(f['details']) - DETAIL_SHOWN}개" if len(f["details"]) > DETAIL_SHOWN else ""
        lines.append(f"• {KINDS[f['kind']]} ({f['count']}) : {shown}{more}")
    return "\n".join(lines)


def save(doc_data: Dict, findings: List[Dict]) -> None:
    point = PointStruct(id=doc_data["page_id"], vector={}, payload={
        "page_id": doc_data["page_id"], "title": doc_data.get("title", ""), "url": doc_data.get("url", ""),
        "path": " > ".join(doc_data.get("path", [])), "findings": findings,
        "checked_at": datetime.now(backlog.KST).isoformat()})
    clients.get_qdrant().upsert(collection_name=config.DIAGNOSIS_COLLECTION, points=[point])


def delete(page_id: str) -> None:
    clients.get_qdrant().delete(collection_name=config.DIAGNOSIS_COLLECTION, points_selector=[page_id])


def notify(text: str) -> None:
    """담당자에게 DM. 슬랙 봇이 떠 있지 않으면(스크립트 실행 등) 보내지 않는다"""
    import slack_bot
    if not config.DIAGNOSIS_REPORT_USER_ID or slack_bot.slack_app is None:
        return
    slack_bot.slack_app.client.chat_postMessage(channel=config.DIAGNOSIS_REPORT_USER_ID, text=text)


def run(doc_data: Dict, notify_owner: bool = False) -> List[Dict]:
    """동기화가 끝난 문서를 진단해 저장하고, notify_owner면 발견한 것이 있을 때 담당자에게 알린다.
    진단은 부가 기능이므로 실패해도 동기화 결과에는 영향을 주지 않는다."""
    try:
        findings = diagnose(doc_data, search.get_documents())
        save(doc_data, findings)
        if findings:
            logger.info("📋 [문서 진단] '%s': %s", doc_data.get("title"), ", ".join(KINDS[f["kind"]] for f in findings))
            if notify_owner:
                notify(format_report(doc_data, findings))
        return findings
    except Exception:
        logger.exception("🚨 [문서 진단] 실패 (page_id: %s)", doc_data.get("page_id"))
        return []
