"""사서: 저장된 원본 기획서를 해석자(agent.py)가 읽기 쉬운 정리본으로 다시 써서 별도 컬렉션(REFINED_COLLECTION)에 저장한다.

원본 동기화(indexing)가 끝나면 문서를 작업 목록(LIBRARIAN_JOB_COLLECTION)에 올리고, 별도 프로세스가 하나씩 정리한다.
정리에 수십 초가 걸리므로 슬랙 답변 서버와 프로세스를 나눈다. 둘은 DB로만 주고받는다.

    python librarian.py                # 작업 목록을 계속 확인하며 정리 (VM의 designbrain-librarian 서비스)
    python librarian.py --all          # 인덱스된 모든 문서를 지금 정리 (원본이 바뀌지 않은 문서는 건너뜀, --workers로 동시 처리)
    python librarian.py <page_id>...   # 지정한 문서만 지금 정리
    python librarian.py --dry-run <page_id>   # 저장하지 않고 정리본만 출력

정리본 청크의 payload는 원본 청크와 같은 모양(page_id, title, path, url, text, item, images)이라 검색/답변 코드를 그대로 쓴다.
sources에는 정리에 쓴 원본 문서 목록이 들어간다 (출처. 원본이 다시 동기화되면 그 원본으로 만든 정리본을 다시 정리한다).

기획자가 '완료'해 들어온 작업(requested_by = 누른 사람의 노션 ID)은 정리한 뒤 검토(review.py)까지 해서,
충돌/누락/새 주제 제안을 그 사람에게 슬랙으로 보낸다. 같은 원본은 한 번만 검토한다.
"""
import argparse
import hashlib
import logging
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Dict, List
from qdrant_client.models import PointStruct, Filter, FieldCondition, MatchValue, HasIdCondition
import backlog
import clients
import config
import doc_diagnosis
import indexing

logger = logging.getLogger("librarian")

REFINE_PROMPT = (
    "너는 게임 기획팀의 기획서 정리 담당 '사서'다. 노션 원본 기획서를 다른 AI(해석자)가 질문에 답할 때 읽을 정리본으로 다시 써라.\n\n"
    "[반드시 지킬 것]\n"
    "- 원본의 사실(이름, 수치, 조건, 규칙, 예외, 순서)을 하나도 빼지 마라. 원본에 없는 내용은 더하지 마라. 추측·평가·제안도 쓰지 마라.\n"
    "- 항목명, 수치, 용어는 원문 글자 그대로 쓴다.\n"
    "- 꾸밈말, 반복, 빈 서식은 걷어내고 '대상 — 속성: 값'처럼 짧은 문장과 목록으로 쓴다. 표의 행은 한 줄에 '열 이름: 값'으로.\n"
    "- 원본에 제목만 있고 내용이 없는 부분은 따로 줄을 만들지 말고, 섹션 끝에 '내용 없음: 제목1, 제목2'처럼 한 줄로 모아 남긴다.\n"
    "- 다른 데이터를 가리키는 이름(예: 각인 A='협공')과 '🔗 참고 링크' 줄은 그대로 남긴다.\n"
    "- 원본 끝의 '📌 결정 기록'은 기획자가 승인한 결정이다. 본문과 다르면 결정 기록을 따라 해당 부분을 정리하고 "
    "'(결정 기록 반영, 날짜)'를 붙인다. 결정 기록 자체도 마지막 '## 결정 기록' 섹션에 남긴다.\n\n"
    "[형식]\n"
    "- 첫 섹션은 '## 개요': 이 문서가 무엇을 정의하는지, 노션 위치로 보아 무엇에 속하는지 2~4문장. 본문이 거의 없으면 그렇다고 쓴다.\n"
    "- 이어서 주제별로 '## 주제 이름' 섹션을 나눈다.\n"
    "- [DB 항목 목록]에 있는 항목은 항목마다 '## 항목: 항목 이름' 섹션 하나로 쓴다. 항목 이름은 목록의 글자 그대로 쓴다.\n"
    "- 정리본만 출력하라. 머리말이나 맺음말은 쓰지 마라."
)
# 원본이 이보다 길면 나눠서 정리한다 (출력이 길어지면 빠뜨리는 내용이 생기기 쉬우므로)
REFINE_PART_CHARS = 16000
# 정리본 섹션이 이보다 길면 줄 단위로 나눠 청크로 저장한다
REFINED_CHUNK_CHARS = 1500
ITEM_HEADING = "## 항목: "
JOB_POLL_SECONDS = 10
JOB_MAX_ATTEMPTS = 3


# --- [작업 목록] ---
def job_id(page_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"librarian-job:{page_id}"))

def enqueue(page_id: str, requested_by: str = None) -> None:
    """정리할 문서를 작업 목록에 올린다. 이미 있으면 요청 시각만 새로 바뀐다 (같은 문서는 한 번만 정리).
    requested_by(‘완료’를 누른 사람의 노션 ID)가 있으면 정리 후 검토해 그 사람에게 제안을 보낸다.
    기다리는 작업에 이미 요청자가 있으면 요청자 없는 재동기화가 지우지 않는다."""
    qdrant = clients.get_qdrant()
    if not requested_by:
        waiting = qdrant.retrieve(collection_name=config.LIBRARIAN_JOB_COLLECTION, ids=[job_id(page_id)])
        requested_by = waiting[0].payload.get("requested_by") if waiting else None
    qdrant.upsert(collection_name=config.LIBRARIAN_JOB_COLLECTION, points=[
        PointStruct(id=job_id(page_id), vector={}, payload={"page_id": page_id, "requested_at": time.time(), "attempts": 0,
                                                            "requested_by": requested_by})])

SYNTHESIS = "synthesis"

def synthesis_job_id(topic: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"librarian-synthesis:{''.join(topic.split()).lower()}"))

def enqueue_synthesis(topic: str, reason: str, question: str, doc_names: List[str]) -> None:
    """@Brain이 요청한 종합 문서 작업을 올린다 (synthesis.py). 같은 주제가 기다리고 있으면 하나로 합쳐진다"""
    clients.get_qdrant().upsert(collection_name=config.LIBRARIAN_JOB_COLLECTION, points=[
        PointStruct(id=synthesis_job_id(topic), vector={}, payload={
            "kind": SYNTHESIS, "topic": topic, "reason": reason, "question": question, "doc_names": doc_names,
            "requested_at": time.time(), "attempts": 0})])

def job_key(job: Dict) -> str:
    return synthesis_job_id(job["topic"]) if job.get("kind") == SYNTHESIS else job_id(job["page_id"])

def pending_jobs() -> List[Dict]:
    jobs, offset = [], None
    while True:
        points, offset = clients.get_qdrant().scroll(collection_name=config.LIBRARIAN_JOB_COLLECTION, limit=256, offset=offset)
        jobs += [p.payload for p in points]
        if offset is None:
            return sorted(jobs, key=lambda j: j["requested_at"])

def finish_job(job: Dict, failed: bool) -> None:
    """정리하는 동안 같은 작업이 다시 요청됐으면(요청 시각이 바뀜) 작업을 남겨 다시 처리한다"""
    qdrant = clients.get_qdrant()
    key = job_key(job)
    current = qdrant.retrieve(collection_name=config.LIBRARIAN_JOB_COLLECTION, ids=[key])
    if current and current[0].payload["requested_at"] != job["requested_at"]:
        return
    attempts = job.get("attempts", 0) + 1
    if failed and attempts < JOB_MAX_ATTEMPTS:
        qdrant.set_payload(collection_name=config.LIBRARIAN_JOB_COLLECTION, payload={"attempts": attempts}, points=[key])
        return
    if failed:
        logger.error("🚨 [사서] %d번 실패해 작업을 버립니다 (%s)", attempts, job.get("page_id") or job.get("topic"))
    qdrant.delete(collection_name=config.LIBRARIAN_JOB_COLLECTION, points_selector=[key])


# --- [원본 읽기] ---
def original_chunks(page_id: str) -> List:
    return chunks(config.COLLECTION_NAME, page_id)

def chunks(collection: str, page_id: str) -> List:
    """문서 하나의 청크 (문서 안 순서대로)"""
    points, offset = [], None
    while True:
        batch, offset = clients.get_qdrant().scroll(
            collection_name=collection, limit=256, offset=offset, with_payload=True, with_vectors=False,
            scroll_filter=Filter(must=[FieldCondition(key="page_id", match=MatchValue(value=page_id))]))
        points += batch
        if offset is None:
            return sorted(points, key=lambda p: p.payload.get("chunk_idx", 0))

def chunk_body(payload) -> str:
    """청크 앞의 '문서: 제목 (위치: …)' 머리줄을 뗀 본문"""
    text = payload.get("text", "")
    first, _, rest = text.partition("\n")
    return rest if first.startswith("문서: ") else text

def source_hash(bodies: List[str]) -> str:
    return hashlib.sha256("\n".join(bodies).encode()).hexdigest()


# --- [정리] ---
def split_parts(bodies: List[str]) -> List[str]:
    """청크 경계를 넘지 않게 원본을 REFINE_PART_CHARS 안쪽으로 나눈다"""
    parts, current = [], []
    for body in bodies:
        if current and sum(map(len, current)) + len(body) > REFINE_PART_CHARS:
            parts.append("\n".join(current))
            current = []
        current.append(body)
    if current:
        parts.append("\n".join(current))
    return parts

def refine_text(title: str, path: str, bodies: List[str], items: List[str]) -> tuple:
    """(정리본, 입력 토큰, 출력 토큰)"""
    parts = split_parts(bodies)
    outputs, input_tokens, output_tokens = [], 0, 0
    for no, part in enumerate(parts, 1):
        part_items = [i for i in items if f"[{i}]" in part]
        note = "" if len(parts) == 1 else (f"\n(원본이 길어 {len(parts)}부분으로 나눠 준다. 지금은 {no}번째 부분이다. "
                                          + ("'## 개요'는 쓰지 말고 이어서 정리하라.)" if no > 1 else "개요는 이 부분에서만 쓴다.)"))
        response = clients.get_openai().responses.create(
            model=config.REFINE_MODEL, instructions=REFINE_PROMPT, reasoning={"effort": config.REFINE_REASONING_EFFORT},
            input=f"[문서 제목] {title}\n[노션 위치] {path or '(최상위)'}\n"
                  f"[DB 항목 목록] {', '.join(part_items) if part_items else '(없음)'}{note}\n\n[원본]\n{part}")
        outputs.append((response.output_text or "").strip())
        if response.usage:
            input_tokens += response.usage.input_tokens
            output_tokens += response.usage.output_tokens
    return "\n\n".join(outputs), input_tokens, output_tokens

def split_sections(refined: str, items: List[str]) -> List[tuple]:
    """정리본을 '## ' 섹션으로 나눠 [(항목 이름 또는 '', 섹션 글)]. 원본에 없는 항목 이름은 항목으로 보지 않는다"""
    sections = []
    for line in refined.split("\n"):
        if line.startswith("## ") or not sections:
            name = line[len(ITEM_HEADING):].strip() if line.startswith(ITEM_HEADING) else ""
            sections.append([name if name in items else "", []])
        sections[-1][1].append(line)
    return [(item, "\n".join(lines).strip()) for item, lines in sections if "\n".join(lines).strip()]

def split_long_section(text: str) -> List[str]:
    """긴 섹션은 줄 단위로 나누고, 이어지는 조각에도 섹션 제목을 붙여 어느 부분인지 알 수 있게 한다"""
    if len(text) <= REFINED_CHUNK_CHARS:
        return [text]
    heading, *lines = text.split("\n")
    pieces, current = [], [heading]
    for line in lines:
        if len(current) > 1 and sum(len(l) + 1 for l in current) + len(line) > REFINED_CHUNK_CHARS:
            pieces.append("\n".join(current))
            current = [f"{heading} (이어서)"]
        current.append(line)
    pieces.append("\n".join(current))
    return pieces

def refined_point_id(page_id: str, idx: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"refined:{page_id}:{idx}"))

def stored_hash(page_id: str) -> str:
    points = clients.get_qdrant().retrieve(collection_name=config.REFINED_COLLECTION, ids=[refined_point_id(page_id, 0)])
    return points[0].payload.get("source_hash", "") if points else ""

def refine_document(page_id: str, force: bool = False, dry_run: bool = False):
    """원본 문서 하나를 정리해 정리본 청크로 저장한다. 원본이 없으면 정리본도 지운다.
    원본이 지난번 정리 때와 같으면 건너뛴다 (force면 다시 정리). 저장한 청크 수를 돌려준다 (건너뛰면 None)."""
    originals = original_chunks(page_id)
    if not originals:
        delete_refined(page_id)
        return 0
    first = originals[0].payload
    title, path = first.get("title", "제목 없음"), first.get("path", "")
    bodies = [chunk_body(p.payload) for p in originals]
    digest = source_hash(bodies)
    if not force and not dry_run and stored_hash(page_id) == digest:
        logger.info("⏭️ [사서] '%s' 원본이 바뀌지 않아 건너뜁니다.", title)
        return None

    items = list(dict.fromkeys(p.payload["item"] for p in originals if p.payload.get("item")))
    started = time.time()
    refined, input_tokens, output_tokens = refine_text(title, path, bodies, items)
    logger.info("🧹 [사서] '%s' 정리 완료: 원본 %d자 → 정리본 %d자, 토큰 %d/%d, %.0f초",
                title, sum(map(len, bodies)), len(refined), input_tokens, output_tokens, time.time() - started)
    if dry_run:
        print(refined)
        return 0

    # 도판은 원본처럼 항목 청크에는 그 항목의 것만, 나머지 청크에는 문서 본문의 것을 단다
    images = {}
    for p in originals:
        for img in p.payload.get("images", []):
            images.setdefault(p.payload.get("item", ""), {})[img["block_id"]] = img
    header = indexing.chunk_header(title, path.split(" > ") if path else [])
    pieces = [(item, f"{header}\n{piece}") for item, text in split_sections(refined, items) for piece in split_long_section(text)]
    vectors = clients.get_embeddings([text for _, text in pieces])
    refined_at = datetime.now(backlog.KST).isoformat()
    source = {"page_id": page_id, "title": title, "url": first.get("url", "")}
    points = [PointStruct(id=refined_point_id(page_id, idx), vector=vector, payload={
        "page_id": page_id, "title": title, "path": path, "url": first.get("url", ""), "text": text, "chunk_idx": idx,
        "images": list(images.get(item, {}).values()), **({"item": item} if item else {}),
        "sources": [source], "source_hash": digest, "refined_at": refined_at})
        for idx, ((item, text), vector) in enumerate(zip(pieces, vectors))]

    qdrant = clients.get_qdrant()
    qdrant.upsert(collection_name=config.REFINED_COLLECTION, points=points)
    qdrant.delete(collection_name=config.REFINED_COLLECTION, points_selector=Filter(
        must=[FieldCondition(key="page_id", match=MatchValue(value=page_id))],
        must_not=[HasIdCondition(has_id=[p.id for p in points])]))
    import search
    search.invalidate_name_catalog()
    return len(points)

def delete_refined(page_id: str) -> None:
    """원본이 삭제되면 그 원본으로 만든 정리본을 지운다"""
    clients.get_qdrant().delete(collection_name=config.REFINED_COLLECTION, points_selector=Filter(
        must=[FieldCondition(key="sources[].page_id", match=MatchValue(value=page_id))]))
    import search
    search.invalidate_name_catalog()


# --- [검토] ---
def refined_head(page_id: str):
    """정리본 첫 청크의 payload (검토 여부 기록을 여기에 둔다). 정리본이 없으면 None"""
    points = clients.get_qdrant().retrieve(collection_name=config.REFINED_COLLECTION, ids=[refined_point_id(page_id, 0)])
    return points[0].payload if points else None

def review_and_notify(page_id: str, requested_by: str) -> List[Dict]:
    """정리본을 검토해 제안을 백로그로 저장하고 '완료'한 사람에게 슬랙으로 보낸다.
    같은 원본은 한 번만 검토한다 (정리본이 바뀌지 않았으면 건너뜀). 저장한 제안 목록을 돌려준다."""
    import proposal_slack
    import review
    head = refined_head(page_id)
    if head is None or head.get("reviewed_hash") == head.get("source_hash"):
        return []
    target = {"page_id": page_id, "title": head["title"], "url": head.get("url", "")}
    findings = review.review_document(page_id, head["title"])
    slack = proposal_slack.web_client()
    requester = proposal_slack.slack_user_for_notion(slack, requested_by) if slack else None
    # '완료'한 사람을 슬랙에서 찾지 못하면 대신 받는 사람(문서 진단 담당자)이 처리한다
    assignee_id = requester or config.DIAGNOSIS_REPORT_USER_ID
    assignee_name = proposal_slack.user_name(slack, assignee_id) if slack and assignee_id else None
    review_id = str(uuid.uuid4())
    records = [backlog.add_proposal(f, target, review.related_docs(f.get("related_docs", [])), assignee_id, assignee_name, review_id)
               for f in findings]
    clients.get_qdrant().set_payload(collection_name=config.REFINED_COLLECTION, payload={"reviewed_hash": head["source_hash"]},
                                     points=[refined_point_id(page_id, 0)])
    # AI가 읽지 못한 부분(DB 뷰 등)은 검토로 찾을 수 없으므로 문서 진단 기록에서 가져와 함께 알린다
    unreadable = doc_diagnosis.unreadable_notice(page_id)
    if (records or unreadable) and slack:
        proposal_slack.send_review(slack, assignee_id, target, records, requester_found=bool(requester), unreadable=unreadable)
    return records


# --- [실행] ---
def process_jobs() -> int:
    """쌓인 작업을 오래된 것부터 처리하고 처리한 수를 돌려준다.
    '완료'로 들어온 작업(requested_by)은 정리 후 검토까지 하고, 종합 요청은 초안을 만들어 보낸다."""
    import synthesis
    jobs = pending_jobs()
    for job in jobs:
        try:
            if job.get("kind") == SYNTHESIS:
                synthesis.handle_request(job)
            else:
                # 원본 내용이 바뀌었으면 그 원본으로 만든 지도 문서에 반영할 것이 있는지 본다
                if refine_document(job["page_id"]):
                    synthesis.check_dependents(job["page_id"])
                if job.get("requested_by"):
                    review_and_notify(job["page_id"], job["requested_by"])
            finish_job(job, failed=False)
        except Exception:
            logger.exception("🚨 [사서] 작업 실패 (%s)", job.get("page_id") or job.get("topic"))
            finish_job(job, failed=True)
    return len(jobs)

def run_forever() -> None:
    logger.info("🧹 [사서] 작업 목록 확인을 시작합니다. (%d초 간격)", JOB_POLL_SECONDS)
    while True:
        try:
            process_jobs()
        except Exception:
            # DB 연결이 잠깐 끊겨도 서비스는 계속 돈다
            logger.exception("🚨 [사서] 작업 목록 확인 실패")
        time.sleep(JOB_POLL_SECONDS)

def indexed_page_ids() -> List[str]:
    page_ids, offset = set(), None
    while True:
        points, offset = clients.get_qdrant().scroll(config.COLLECTION_NAME, limit=256, offset=offset, with_payload=["page_id"])
        page_ids.update(p.payload["page_id"] for p in points if p.payload.get("page_id"))
        if offset is None:
            return sorted(page_ids)

def main(args) -> int:
    parser = argparse.ArgumentParser(description="사서: 원본 기획서를 정리본으로")
    parser.add_argument("page_ids", nargs="*", help="지금 정리할 문서")
    parser.add_argument("--all", action="store_true", help="인덱스된 모든 문서를 지금 정리")
    parser.add_argument("--force", action="store_true", help="원본이 그대로여도 다시 정리")
    parser.add_argument("--dry-run", action="store_true", help="저장하지 않고 정리본만 출력")
    parser.add_argument("--workers", type=int, default=1, help="동시에 정리할 문서 수")
    opts = parser.parse_args(args)
    if not opts.page_ids and not opts.all:
        run_forever()
        return 0
    failed = []
    page_ids = indexed_page_ids() if opts.all else opts.page_ids

    def refine(numbered):
        no, page_id = numbered
        logger.info("진행 %d/%d", no, len(page_ids))
        try:
            refine_document(page_id, force=opts.force, dry_run=opts.dry_run)
        except Exception:
            logger.exception("🚨 [사서] 정리 실패 (page_id: %s)", page_id)
            failed.append(page_id)

    with ThreadPoolExecutor(max_workers=opts.workers) as pool:
        list(pool.map(refine, enumerate(page_ids, 1)))
    logger.info("정리 끝: %d개 중 실패 %d개 %s", len(page_ids), len(failed), " ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.exit(main(sys.argv[1:]))
    finally:
        clients.close_clients()
