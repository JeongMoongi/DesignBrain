"""사서 검토: 기획자가 '완료'한 문서를 다른 기획서와 비교해 기획자가 처리할 것(충돌, 누락, 새로 정리할 주제)을 찾는다.
찾은 것은 백로그 제안으로 저장되어 '완료'한 사람에게 슬랙으로 간다 (proposal_slack.py).
다른 문서는 정리본을 해석자와 같은 도구(agent.py의 검색/목록/문서 읽기)로 찾아 읽는다."""
import json
import logging
from typing import Dict, List
import agent
import backlog
import clients
import librarian
import config

logger = logging.getLogger(__name__)

REVIEW_PROMPT = (
    "너는 게임 기획팀의 기획서 정리 담당 '사서'다. 기획자가 방금 [검토할 문서]를 작성하고 '완료'했다. "
    "이 문서를 기존 기획서와 비교해, 기획자가 결정하거나 작성해야 할 것을 찾아 제안하라. 기획자는 네 제안을 버튼 하나로 승인한다.\n\n"
    "[찾을 것]\n"
    "1. 충돌: 이 문서의 내용(이름, 수치, 규칙, 구성)이 다른 기획서와 어긋나는 것. 도구로 관련 문서를 찾아 실제로 확인한 것만.\n"
    "2. 누락: 이 문서 안에서 정의가 비어 있거나('내용 없음', 미정, 논의 필요, 고민 메모), 다른 부분이 전제하는데 정의되지 않은 것. "
    "같은 성격의 빈칸은 한 건으로 묶어라 (예: 여러 항목의 연출 칸이 비어 있음 → 한 건).\n"
    "3. 새 주제: 여러 문서에 흩어져 있어 한 문서로 정리하면 좋을 주제.\n\n"
    "[항목 형식]\n"
    "- type: 1·2는 \"decision\"(이 문서에 결정을 덧붙이면 해결), 3은 \"writing\"(새 문서가 필요)\n"
    "- title: decision은 '결정이 필요함: …', writing은 '작성이 필요함: …'. 무엇이 필요한지만 쓰고 결론을 단정하지 마라.\n"
    "- problem: 무엇이 문제인지와 근거(어느 문서의 어떤 내용인지 원문 인용). 3문장 이내.\n"
    "- proposal: 사서의 제안. decision이면 이 문서의 '📌 결정 기록'에 그대로 붙일 결정 내용을 기획서 문체로 쓴다. "
    "근거 문서가 있으면 그 내용을 따르는 안을, 근거가 없으면 합리적인 안을 낸다. "
    "writing이면 새 문서 초안 전체를 '## 소제목'과 '- 목록'으로 쓰고, 각 내용 끝에 출처 문서를 '(참고: 문서 이름)'으로 단다. "
    "2500자 이내.\n"
    "- assumptions: proposal에서 근거 문서 없이 네가 새로 정한 값이나 규칙을 한 줄로 요약 (없으면 빈 문자열). "
    "기획자가 승인 전에 꼭 확인해야 할 부분이다.\n"
    "- related_docs: 근거로 읽은 문서 이름 목록 (도구 결과에 나온 이름 그대로)\n"
    "- new_doc_title: writing일 때 새 문서 제목 (decision이면 빈 문자열)\n\n"
    "[원칙]\n"
    "- 근거 없는 지적, 표현·서식에 대한 지적은 하지 마라.\n"
    "- [이미 낸 제안]과 같은 내용은 다시 내지 마라. 반려된 제안은 사유를 존중하라.\n"
    "- 문서의 '📌 결정 기록'에 이미 결정된 것은 본문과 달라도 해결된 것으로 본다.\n"
    "- 찾은 것이 없으면 빈 목록을 낸다.\n"
    '- 마지막에는 JSON만 출력한다: {"findings": [{"type": "...", "title": "...", "problem": "...", "proposal": "...", '
    '"assumptions": "...", "related_docs": ["..."], "new_doc_title": "..."}]}'
)
REVIEW_MAX_TOOL_ROUNDS = 8
STATUS_LABELS = {backlog.OPEN: "처리 대기", backlog.RESOLVED: "승인됨", backlog.HELD: "보류됨", backlog.REJECTED: "반려됨"}


def refined_text(page_id: str) -> str:
    """검토할 문서의 정리본 전체 (정리본이 없으면 원본)"""
    for collection in (config.REFINED_COLLECTION, config.COLLECTION_NAME):
        points = librarian.chunks(collection, page_id)
        if points:
            return "\n".join(librarian.chunk_body(p.payload) for p in points)
    return ""


def earlier_proposals(page_id: str) -> str:
    lines = []
    for r in backlog.proposals_for_page(page_id):
        reason = f" (사유: {r['reject_reason']})" if r.get("reject_reason") else ""
        lines.append(f"- [{STATUS_LABELS.get(r['status'], r['status'])}] {r['content']}{reason}")
    return "\n".join(lines) or "(없음)"


def parse_findings(text: str) -> List[Dict]:
    """모델 출력의 JSON에서 형식이 맞는 항목만 고른다"""
    start, end = text.find("{"), text.rfind("}")
    if start == -1:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        logger.warning("⚠️ [사서 검토] 결과 JSON을 읽지 못했습니다: %s", text[:300])
        return []
    findings = []
    for f in data.get("findings", []):
        if f.get("type") in (backlog.DECISION, backlog.WRITING) and f.get("title") and f.get("proposal"):
            if f["type"] == backlog.WRITING and not f.get("new_doc_title"):
                f["new_doc_title"] = f["title"].split(":", 1)[-1].strip()
            findings.append(f)
    return findings


def review_document(page_id: str, title: str) -> List[Dict]:
    """문서를 검토해 제안 목록을 돌려준다. 다른 문서는 도구로 정리본을 찾아 읽는다."""
    state = agent.AgentState(None, config.REFINED_COLLECTION)
    tools = agent.TOOLS + [agent.READ_ORIGINAL_TOOL]
    options = {"model": config.REFINE_MODEL, "instructions": REVIEW_PROMPT, "tools": tools,
               "reasoning": {"effort": config.REVIEW_REASONING_EFFORT}}
    request = (f"[검토할 문서] {title}\n\n{refined_text(page_id)}\n\n[이미 낸 제안]\n{earlier_proposals(page_id)}")
    openai_client = clients.get_openai()
    response = openai_client.responses.create(input=[{"role": "user", "content": request}], **options)
    usage = [response.usage]
    for round_no in range(1, REVIEW_MAX_TOOL_ROUNDS + 1):
        calls = [item for item in response.output if item.type == "function_call"]
        if not calls:
            break
        outputs = [{"type": "function_call_output", "call_id": c.call_id, "output": agent.run_tool(state, c.name, c.arguments)}
                   for c in calls]
        final_round = round_no == REVIEW_MAX_TOOL_ROUNDS
        response = openai_client.responses.create(previous_response_id=response.id, input=outputs,
                                                  **options, **({"tool_choice": "none"} if final_round else {}))
        usage.append(response.usage)
    findings = parse_findings(response.output_text or "")
    logger.info("🔎 [사서 검토] '%s': 제안 %d건, 도구 %d회, 토큰 %d/%d", title, len(findings), len(state.calls),
                sum(u.input_tokens for u in usage if u), sum(u.output_tokens for u in usage if u))
    return findings


def related_docs(names: List[str]) -> List[Dict[str, str]]:
    """제안의 근거 문서 이름을 {"page_id", "title", "url"}로 (찾을 수 없는 이름은 뺀다). 슬랙의 참고 문서 링크로 쓴다"""
    import search
    documents = search.get_documents()
    docs = []
    for name in names:
        found = agent.find_title(name, documents)
        if found:
            docs += backlog.linked_docs(documents[found]["page_ids"][:1])
    return list({d["page_id"]: d for d in docs}.values())
