"""사서 검토: 기획자가 '완료'한 문서를 다른 기획서와 비교해 기획자가 처리할 것(충돌, 누락, 새로 정리할 주제)을 찾는다.
찾은 것은 백로그 제안으로 저장되어 '완료'한 사람에게 슬랙으로 간다 (proposal_slack.py).
다른 문서는 정리본을 해석자와 같은 도구(agent.py의 검색/목록/문서 읽기)로 찾아 읽는다."""
import json
import logging
from datetime import datetime
from typing import Dict, List
import agent
import backlog
import clients
import librarian
import librarian_report
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
STATUS_LABELS = {backlog.OPEN: "처리 대기", backlog.RESOLVED: "승인됨", backlog.HELD: "보류됨", backlog.REJECTED: "반려됨",
                 backlog.APPLIED: "문서 수정으로 반영됨", backlog.OBSOLETE: "문서 변경으로 무효"}

RECHECK_PROMPT = (
    "너는 게임 기획팀의 기획서 정리 담당 '사서'다. [갱신된 문서]가 방금 바뀌었다. 이 문서에 대해 기획자 처리를 기다리던 [제안]들이 "
    "갱신된 문서에서 어떻게 됐는지 판단하라.\n"
    "- applied: 문서 수정으로 제안이 다룬 문제가 해결됐다 (제안과 다른 내용으로 정했어도 해결됐으면 applied)\n"
    "- obsolete: 제안이 가리키던 부분이 사라지거나 크게 바뀌어 제안이 더 이상 의미 없다\n"
    "- keep: 아직 그대로다. 확실하지 않으면 keep\n"
    "JSON만 출력한다: {\"results\": [{\"no\": 제안 번호, \"status\": \"applied|obsolete|keep\", \"reason\": \"근거 한 문장\"}]}"
)
RECHECK_STATUS = {"applied": backlog.APPLIED, "obsolete": backlog.OBSOLETE}
RECHECK_LABELS = {backlog.APPLIED: "✅ 문서가 수정돼 반영된 것으로 보고 닫았어요", backlog.OBSOLETE: "🗑️ 문서가 바뀌어 더 이상 필요 없는 제안이라 닫았어요"}
RECHECK_PROPOSAL_CHARS = 600


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


def parse_json(text: str) -> Dict:
    """모델 출력에서 JSON 객체를 꺼낸다 (앞뒤에 설명이 붙어도). 읽지 못하면 빈 dict"""
    start, end = text.find("{"), text.rfind("}")
    if start == -1:
        return {}
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        logger.warning("⚠️ [사서] 결과 JSON을 읽지 못했습니다: %s", text[:300])
        return {}


def parse_findings(text: str) -> List[Dict]:
    """모델 출력의 JSON에서 형식이 맞는 항목만 고른다"""
    data = parse_json(text)
    findings = []
    for f in data.get("findings", []):
        if f.get("type") in (backlog.DECISION, backlog.WRITING) and f.get("title") and f.get("proposal"):
            if f["type"] == backlog.WRITING and not f.get("new_doc_title"):
                f["new_doc_title"] = f["title"].split(":", 1)[-1].strip()
            findings.append(f)
    return findings


def run_with_tools(instructions: str, request: str, label: str, task: str) -> str:
    """사서가 해석자와 같은 도구(정리본 검색/목록/읽기, 원본 읽기)로 자료를 찾아 가며 일하고, 마지막 출력 글을 돌려준다.
    task는 토큰 기록에 남길 작업 이름 (librarian_report)"""
    state = agent.AgentState(None, config.REFINED_COLLECTION)
    options = {"model": config.REFINE_MODEL, "instructions": instructions, "tools": agent.TOOLS + [agent.READ_ORIGINAL_TOOL],
               "reasoning": {"effort": config.REVIEW_REASONING_EFFORT}}
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
    input_tokens, output_tokens = librarian_report.usage_tokens(usage)
    logger.info("🔎 [사서] %s: 도구 %d회, 토큰 %d/%d", label, len(state.calls), input_tokens, output_tokens)
    librarian_report.record_usage(task, label, input_tokens, output_tokens)
    return response.output_text or ""


def review_document(page_id: str, title: str) -> List[Dict]:
    """문서를 검토해 제안 목록을 돌려준다. 다른 문서는 도구로 정리본을 찾아 읽는다."""
    request = f"[검토할 문서] {title}\n\n{refined_text(page_id)}\n\n[이미 낸 제안]\n{earlier_proposals(page_id)}"
    findings = parse_findings(run_with_tools(REVIEW_PROMPT, request, f"'{title}' 검토", librarian_report.REVIEW))
    logger.info("🔎 [사서 검토] '%s': 제안 %d건", title, len(findings))
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


def recheck_open(page_id: str) -> List[Dict]:
    """문서가 갱신되면 그 문서의 미해결 백로그를 새 버전과 대조한다.
    사서 제안은 반영됐거나 무효가 된 것을 닫고 원래 메세지에 알린다. 사람이 등록한 백로그는 닫지 않고 갱신됐다는 표시만 남긴다.
    닫은 제안을 돌려준다."""
    import proposal_slack
    records = backlog.open_for_page(page_id)
    now = datetime.now(backlog.KST).isoformat()
    for r in records:
        if r.get("kind") != backlog.PROPOSAL:
            backlog.update(r["backlog_id"], doc_updated_at=now)
    proposals = [r for r in records if r.get("kind") == backlog.PROPOSAL]
    text = refined_text(page_id)
    if not proposals or not text:
        return []
    listing = "\n\n".join(f"{no}. {r['content']}\n문제: {r.get('problem', '')}\n제안: {r['proposal'][:RECHECK_PROPOSAL_CHARS]}"
                          for no, r in enumerate(proposals, 1))
    response = clients.get_openai().responses.create(
        model=config.REFINE_MODEL, instructions=RECHECK_PROMPT, reasoning={"effort": config.REFINE_REASONING_EFFORT},
        input=f"[갱신된 문서]\n{text}\n\n[제안]\n{listing}")
    title = next((d["title"] for d in proposals[0].get("docs", []) if d.get("page_id") == page_id), page_id)
    librarian_report.record_usage(librarian_report.RECHECK, title, *librarian_report.usage_tokens([response.usage]))
    closed = []
    for result in parse_json(response.output_text or "").get("results", []):
        status = RECHECK_STATUS.get(result.get("status"))
        no = result.get("no")
        if not status or not isinstance(no, int) or not 1 <= no <= len(proposals):
            continue
        record = proposals[no - 1]
        reason = result.get("reason", "")
        backlog.close(record["backlog_id"], "사서", status, close_reason=reason)
        closed.append(record)
        logger.info("🧹 [사서] 문서 갱신으로 제안 닫음 (%s): %s — %s", status, record["content"], reason)
        if record.get("slack_ts"):
            try:
                client = proposal_slack.web_client()
                import backlog_slack
                client.chat_update(channel=record["slack_channel"], ts=record["slack_ts"], text=record["content"],
                                   blocks=backlog_slack.replace_item_actions(proposal_slack.item_blocks(record), record["backlog_id"],
                                                                             f"{RECHECK_LABELS[status]} ({reason})"))
            except Exception as e:
                logger.warning("⚠️ [사서] 닫은 제안 메세지 갱신 실패: %s", e)
    return closed
