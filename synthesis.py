"""사서 종합: @Brain(해석자)이 질문에 답하다가 '정보가 여러 문서에 흩어져 있다'고 요청한 주제를 한 문서 초안으로 모은다.
초안은 '작성이 필요함' 제안으로 관련 원본 문서를 만든 사람에게 슬랙으로 가고, 승인되면 자동 작성 기획 DB에 문서가 생긴다.
(요청은 agent.py의 request_summary 도구 → librarian.enqueue_synthesis → 사서 프로세스가 handle_request로 처리)"""
import logging
from typing import Dict, List, Optional
import backlog
import clients
import config
import librarian_report
import notion_parser
import review

logger = logging.getLogger(__name__)

SYNTHESIS_PROMPT = (
    "너는 게임 기획팀의 기획서 정리 담당 '사서'다. 팀의 AI 동료 @Brain이 기획자의 질문에 답하다가, "
    "필요한 정보가 한 문서에 정리돼 있지 않고 여러 기획서에 흩어져 있다며 [요청 주제]의 종합 문서를 요청했다. "
    "도구로 관련 기획서를 빠짐없이 찾아 읽고, 흩어진 정보를 찾아갈 수 있게 하는 '지도' 문서 초안을 써라. "
    "기획자가 검토해 승인하면 노션에 새 문서로 등록된다.\n\n"
    "[지도 문서란]\n"
    "- 무엇이 있는지(빠짐없는 목록), 어떻게 나뉘는지(분류), 자세한 내용은 어느 문서에 있는지만 담는다.\n"
    "- 항목마다 한 줄: '이름 — 분류와 그것을 알아볼 수 있는 핵심 특징 한 구절 (참고: 문서 이름)'.\n"
    "- 수치, 패턴, 규칙 같은 상세 내용은 옮기지 마라. 상세는 원본 문서가 맡는다 (원본이 바뀌어도 지도가 낡지 않도록).\n\n"
    "[반드시 지킬 것]\n"
    "- 기획서에 있는 내용만 쓴다. 이름은 원문 그대로 쓴다. 근거 없는 내용을 더하지 마라.\n"
    "- 모든 항목 끝에 출처를 '(참고: 문서 이름)'으로 단다. 문서 이름은 도구 결과에 나온 이름 그대로 쓴다.\n"
    "- 문서끼리 어긋나면 한쪽을 고르지 말고 양쪽을 모두 적고 '문서 간 차이'라고 표시한다.\n"
    "- 폐기·Legacy·미정으로 적힌 것도 빼지 말고 그렇게 표시해서 남긴다.\n"
    "- 전용 기획서가 없는 항목은 '전용 기획서 없음'이라고 쓴다.\n"
    "- 요청 주제의 범위까지만 담는다.\n\n"
    "[출력]\n"
    "JSON만 출력한다: {\"new_doc_title\": \"새 문서 제목\", "
    "\"summary\": \"기획자가 한 줄만 읽고 알 수 있는 요약, 40자 안팎 (예: '챕터 1 적 구성이 여러 문서에 흩어져 있어요')\", \"problem\": \"왜 이 문서가 필요한지 기획자가 바로 납득하도록 두 문장으로. "
    "'[정보]가 [문서들]에 흩어져 있어 [누가 어떤 곤란을 겪는] 문제가 생긴다. 한 문서로 모으면 [무엇이 나아진다].'\", "
    "\"proposal\": \"지도 문서 초안 전체. '## 분류'와 '- 항목' 한 줄씩, 1500자 이내\", "
    "\"assumptions\": \"초안에서 근거 없이 정한 부분 요약 (없으면 빈 문자열)\", \"related_docs\": [\"근거로 읽은 문서 이름\"]}\n"
    "모을 정보가 실제로는 한 문서에 이미 정리돼 있으면 {\"skip\": \"이유\"}만 출력한다."
)
# 이미 낸 '작성이 필요함' 제안(상태 무관: 처리 대기/승인/보류/반려)과 같은 주제면 다시 만들지 않는다.
# 임베딩은 '챕터 1 적'과 '챕터 2 적'을 구분하지 못하므로(실측 0.46 vs 0.48) 유사도로 후보만 고르고 같은 주제인지는 모델이 판단한다
CANDIDATE_MIN_SCORE = 0.35
CANDIDATES_SHOWN = 5
SAME_TOPIC_PROMPT = (
    "새 종합 문서 주제가 [기존 제안] 중 하나와 사실상 같은 문서를 만들게 되는지 판단하라. 대상(챕터 번호, 캐릭터, 시스템)이 다르면 다른 주제다. "
    "같은 것이 있으면 그 번호만, 없으면 0만 출력하라."
)


def similar_proposal(topic: str, topic_vector: List[float]) -> Optional[Dict]:
    hits = clients.get_qdrant().query_points(collection_name=config.BACKLOG_COLLECTION, query=topic_vector, limit=20,
                                             with_payload=True).points
    candidates = [h.payload for h in hits if h.score >= CANDIDATE_MIN_SCORE and h.payload.get("kind") == backlog.PROPOSAL
                  and h.payload.get("proposal_type") == backlog.WRITING][:CANDIDATES_SHOWN]
    if not candidates:
        return None
    listing = "\n".join(f"{no}. {c['new_doc_title'] or c['content']}" for no, c in enumerate(candidates, 1))
    completion = clients.get_openai().chat.completions.create(model=config.CHAT_MODEL, temperature=0, messages=[
        {"role": "system", "content": SAME_TOPIC_PROMPT},
        {"role": "user", "content": f"[새 주제] {topic}\n[기존 제안]\n{listing}"}])
    answer = (completion.choices[0].message.content or "").strip()
    return candidates[int(answer) - 1] if answer.isdigit() and 1 <= int(answer) <= len(candidates) else None


def draft(job: Dict) -> Optional[Dict]:
    """종합 문서 초안을 써서 백로그 제안 형식(finding)으로 돌려준다. 모을 필요가 없으면 None"""
    request = (f"[요청 주제] {job['topic']}\n[요청 이유] {job.get('reason', '')}\n[기획자의 질문] {job.get('question', '')}\n"
               f"[@Brain이 참고한 문서] {', '.join(job.get('doc_names', [])) or '(없음)'}")
    data = review.parse_json(review.run_with_tools(SYNTHESIS_PROMPT, request, f"'{job['topic']}' 종합", librarian_report.SYNTHESIS))
    if data.get("skip") or not data.get("proposal") or not data.get("new_doc_title"):
        logger.info("📚 [사서 종합] '%s' 초안을 만들지 않습니다: %s", job["topic"], data.get("skip", "형식 오류"))
        return None
    return {"type": backlog.WRITING, "title": f"작성이 필요함: {data['new_doc_title']}", "new_doc_title": data["new_doc_title"],
            "category": backlog.TOPIC, "summary": review.one_line(data.get("summary")),
            "problem": data.get("problem", ""), "proposal": data["proposal"], "assumptions": data.get("assumptions", ""),
            "related_docs": data.get("related_docs", [])}


UPDATE_PROMPT = (
    "너는 게임 기획팀의 기획서 정리 담당 '사서'다. [지도 문서]는 여러 원본 기획서를 모아 만든 지도로, "
    "무엇이 있는지·어떻게 나뉘는지·상세는 어느 문서에 있는지만 담는다. 원본 중 [바뀐 문서]가 방금 갱신됐다.\n"
    "지도의 항목(목록, 분류, 한 줄 특징, 출처)이 바뀐 문서와 어긋나거나, 바뀐 문서에 새로 생겨 지도에 빠진 항목이 있는지 확인하라. "
    "지도에 없는 상세(수치, 패턴)의 변경은 무시한다. [이미 낸 제안]과 같은 것은 다시 내지 마라.\n"
    "고칠 것이 있으면 JSON만 출력한다: {\"needs_update\": true, \"title\": \"결정이 필요함: …\", "
    "\"summary\": \"기획자가 한 줄만 읽고 알 수 있는 요약, 40자 안팎 (예: '지도에 모드레드가 빠져 있어요: 챕터 2 보스에 새로 생김')\", "
    "\"problem\": \"왜 고쳐야 하는지 기획자가 바로 납득하도록 두 문장으로. "
    "'[바뀐 문서]는 [이렇게] 바뀌었으나 지도는 [그대로]라 [누가 어떤 곤란을 겪는] 문제가 생긴다. 지도를 고치면 [무엇이 나아진다].'\", "
    "\"proposal\": \"지도에 반영할 항목 줄만 지도 형식으로 (참고: 문서 이름) 포함\", "
    "\"assumptions\": \"\"}\n"
    "고칠 것이 없으면 {\"needs_update\": false}만 출력한다."
)


def summaries_built_from(page_id: str) -> List[Dict]:
    """이 원본으로 만들어져 승인된 지도(종합) 문서 제안들"""
    records, offset = [], None
    while True:
        points, offset = clients.get_qdrant().scroll(collection_name=config.BACKLOG_COLLECTION, limit=256, offset=offset,
                                                     with_payload=True)
        records += [p.payload for p in points
                    if p.payload.get("proposal_type") == backlog.WRITING and p.payload.get("status") == backlog.RESOLVED
                    and p.payload.get("notion_page_id")
                    and page_id in (p.payload.get("source_page_ids") or [d["page_id"] for d in p.payload.get("docs", [])])]
        if offset is None:
            return records


def check_dependents(page_id: str) -> List[Dict]:
    """원본이 바뀌었을 때, 그 원본으로 만든 지도 문서에 반영할 것이 있으면 지도를 승인한 사람에게 갱신 제안을 보낸다.
    갱신은 지도 문서의 결정 기록에 덧붙는 결정 제안이다 (정리할 때 결정 기록이 본문보다 우선한다). 보낸 제안을 돌려준다."""
    import proposal_slack
    sent = []
    for summary in summaries_built_from(page_id):
        summary_page = summary["notion_page_id"]
        summary_text, source_text = review.refined_text(summary_page), review.refined_text(page_id)
        if not summary_text or not source_text:
            continue
        response = clients.get_openai().responses.create(
            model=config.REFINE_MODEL, instructions=UPDATE_PROMPT, reasoning={"effort": config.REFINE_REASONING_EFFORT},
            input=f"[지도 문서] {summary['new_doc_title']}\n{summary_text}\n\n[바뀐 문서]\n{source_text}\n\n"
                  f"[이미 낸 제안]\n{review.earlier_proposals(summary_page)}")
        librarian_report.record_usage(librarian_report.MAP_UPDATE, summary["new_doc_title"],
                                      *librarian_report.usage_tokens([response.usage]))
        data = review.parse_json(response.output_text or "")
        if not data.get("needs_update") or not data.get("title") or not data.get("proposal"):
            continue
        target = {"page_id": summary_page, "title": summary["new_doc_title"], "url": summary.get("notion_url", "")}
        source = next(iter(backlog.linked_docs([page_id])), None)
        finding = {"type": backlog.DECISION, "title": data["title"],
                   "summary": review.one_line(data.get("summary")), "problem": data.get("problem", ""),
                   "proposal": data["proposal"], "assumptions": data.get("assumptions", "")}
        slack = proposal_slack.web_client()
        assignee_id = summary.get("resolved_by") or config.DIAGNOSIS_REPORT_USER_ID
        assignee_name = proposal_slack.user_name(slack, assignee_id) if slack and assignee_id else None
        record = backlog.add_proposal(finding, target, [source] if source else [], assignee_id, assignee_name,
                                      review_id=f"summary-update:{page_id}")
        if slack:
            changed = f"<{source['url']}|{source['title']}>" if source else "원본 문서"
            intro = (f"🗺️ {changed} 이(가) 바뀌어, 그 문서로 만든 지도 <{target['url']}|{target['title']}> 에 반영할 내용이 있어요.\n"
                     "통과하면 지도 문서의 결정 기록에 추가돼요.")
            proposal_slack.send_review(slack, assignee_id, target, [record], intro=intro)
        logger.info("🗺️ [사서] '%s' 변경으로 지도 '%s' 갱신 제안", page_id, summary["new_doc_title"])
        sent.append(record)
    return sent


def page_creator(page_id: str) -> Optional[str]:
    """노션 페이지를 만든 사람의 노션 ID"""
    page = notion_parser.fetch_page(page_id, config.NOTION_INTEGRATION_TOKEN)
    return (page or {}).get("created_by", {}).get("id")


def handle_request(job: Dict, check_existing: bool = True) -> Optional[Dict]:
    """종합 요청 하나를 처리한다: 비슷한 제안이 있으면 건너뛰고, 아니면 초안을 써서 관련 원본 문서를 만든 사람에게 보낸다.
    저장한 제안을 돌려준다. check_existing=False면 이미 낸 제안이 있어도 다시 만든다 (형식을 바꿔 다시 낼 때)."""
    import proposal_slack
    topic = job["topic"]
    existing = similar_proposal(topic, clients.get_embedding(topic)) if check_existing else None
    if existing:
        logger.info("📚 [사서 종합] '%s'은(는) 이미 낸 제안 '%s'(%s)과 비슷해 건너뜁니다.", topic, existing["content"], existing["status"])
        return None
    finding = draft(job)
    if not finding:
        return None
    related = review.related_docs(finding["related_docs"]) or review.related_docs(job.get("doc_names", []))
    if not related:
        logger.warning("📚 [사서 종합] '%s' 초안의 근거 문서를 찾지 못해 보내지 않습니다.", topic)
        return None
    target = related[0]
    slack = proposal_slack.web_client()
    author = proposal_slack.slack_user_for_notion(slack, page_creator(target["page_id"])) if slack else None
    assignee_id = author or config.DIAGNOSIS_REPORT_USER_ID
    assignee_name = proposal_slack.user_name(slack, assignee_id) if slack and assignee_id else None
    record = backlog.add_proposal(finding, target, related[1:], assignee_id, assignee_name, review_id=f"synthesis:{topic}")
    if slack:
        question = job.get("question", "")
        intro = (f"📚 @Brain이 질문에 답하면서 *{topic}* 정보가 여러 문서에 흩어져 있는 것을 발견해, 사서가 종합 문서 초안을 만들었어요."
                 + (f"\n> {question}" if question else "")
                 + f"\n관련 문서 <{target['url']}|{target['title']}> 을(를) 만드신 분께 보내요. 통과하면 노션에 새 문서로 등록돼요.")
        proposal_slack.send_review(slack, assignee_id, target, [record], requester_found=bool(author), intro=intro)
    return record
