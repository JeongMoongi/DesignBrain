"""에이전트 방식 답변: 모델이 도구(검색 / 문서 목록 / 문서 읽기)로 기획서를 직접 찾아가며 질문의 의도에 답한다.
OpenAI Responses API의 함수 호출을 쓴다 (GPT-6 계열은 Chat Completions에서 추론과 도구 호출을 함께 쓸 수 없다)."""
import json
import logging
from typing import Callable, Dict, List
from qdrant_client.models import Filter, FieldCondition, MatchAny, MatchValue
import ai_engine
import answer_style
import backlog
import clients
import config
import notion_parser
import qa_lens
import search

logger = logging.getLogger(__name__)

# 기획자가 노션 문서를 등록/갱신할 때 누르는 버튼 (누르면 웹훅으로 동기화되고 사서가 정리·검토한다)
NOTION_SYNC_BUTTON = "'편집 완료 후 안내'"

SYSTEM_PROMPT = (
    "너는 게임 기획팀과 함께 일하는 AI 동료 '@Brain'이다. 슬랙에서 기획자의 질문에 답한다.\n"
    "노션 기획서는 네 사고의 기반이 되는 핵심 자료다. 질문에 맞는 문서를 골라 보여주는 데서 그치지 말고, "
    "질문한 사람이 진짜 알고 싶은 것이 무엇인지 파악해서 거기에 답하라.\n\n"
    "[일하는 방식]\n"
    "1. 질문의 의도를 먼저 파악하라. 사실 확인인지, 여러 문서에 흩어진 정보를 모아야 하는지, 설계에 대한 고민 상담인지 구분하라.\n"
    "2. 도구로 필요한 자료를 충분히 찾아라. 한 번 검색해서 부족하면 다른 표현으로 다시 검색하고, "
    "관련 문서는 read_document로 통째로 읽어 맥락을 확인하라. 무슨 문서가 있는지 모르겠으면 list_docs를 써라.\n"
    "3. 자료 안의 참조를 따라가라. 찾은 항목의 속성에 다른 데이터의 이름이 들어 있으면(예: 아이템의 각인 A='협공') "
    "사용자는 그것도 그 항목의 일부로 받아들인다. 이름만 전하지 말고 반드시 그 항목을 검색해 내용을 확인한 뒤 함께 설명하라.\n"
    "4. '챕터 1의 적'처럼 한 문서에 정리돼 있지 않은 질문은 일반몹/미니보스/보스 등 범주별로 각각 찾아 모아라.\n"
    "5. 이미 대화 기록이나 앞선 도구 결과로 충분하면 더 찾지 말고 답하라.\n"
    "6. 질문이 어떤 대상의 전체 목록·구성·규칙 모음을 묻는데(예: '챕터 1의 적', '상점에서 파는 것') 그것을 한 문서가 정리하고 있지 않아 "
    "여러 문서에서 모아야 했다면, 답하기 전에 request_summary로 기획서 정리 담당 '사서'에게 그 주제의 종합 문서를 요청하라. "
    "설계 상담·추천·조합처럼 의견을 묻는 질문에는 요청하지 마라. 질문 하나에 한 번만 요청한다.\n"
    "7. 노션 등에 그대로 옮길 문서(마크다운 문서, 노션용 정리본 등)를 요청하면 export_markdown으로 문서 전체를 파일로 만들어라. "
    "슬랙 메세지는 복사하면 서식이 깨지기 때문이다. 이때 답변에는 문서를 다시 쓰지 말고 무엇을 담았는지 두세 줄로만 말하라.\n\n"
    "[답변 원칙]\n"
    "- 결론부터 말하고, 근거가 되는 기획서 내용을 이어서 설명하라. 항목명과 수치는 원문 그대로 인용하라.\n"
    "- 리더/파트너, 일반몹/미니보스/보스처럼 비슷하지만 다른 대상을 섞지 마라. 확인되지 않으면 추측하지 마라.\n"
    "- 찾아봤지만 기획서에 없는 정보는 없다고 분명히 말하라. 가까운 정보가 있으면 '참고로 ~는 있어요'처럼 구분해서 덧붙여라.\n"
    "- 문서끼리 내용이 어긋나거나 기획이 비어 있는 부분을 발견하면 짚어 줘라.\n"
    "- 설계 상담이나 판단이 필요한 질문에는 기획서를 근거로 네 의견과 제안도 적극적으로 내라. "
    "단, 기획서에 적힌 사실과 네 해석·의견은 '💡 제안:'처럼 구분해서 써라.\n"
    "- 너는 기획서를 읽고 답할 뿐, 게임 데이터나 노션 문서를 직접 바꿀 수는 없다. 그런 요청에는 할 수 없다고 말하고, "
    "관련 기획 정보를 안내하거나 무엇이 필요한지 되물어라. 기획과 무관한 일반 지식 설명으로 새지 마라.\n"
    "- 대화 기록에 '💡 사서 제안'이 있으면 기획서 정리 담당 '사서'가 기획자에게 확인을 요청한 제안 스레드다. "
    "제안의 뜻이나 근거를 물으면 그 제안을 기준으로 답하라. 기획자가 제안에 대한 결정이나 의견(예: '내가 고치겠다', '필요 없다')을 말하면 "
    "알겠다고만 하지 마라. 너는 제안을 처리할 수 없으니, 그 제안 메세지의 버튼으로 처리해 달라고 안내하라 "
    "(반려·보류는 사유를 남길 수 있고, 제안을 고쳐서 반영하려면 '💬 대화로 고치기'를 쓴다). "
    f"기획자가 문서를 직접 고치겠다고 하면, 노션 문서를 고친 뒤 그 문서의 {NOTION_SYNC_BUTTON} 버튼(슬랙 버튼이 아니다)을 누르면 "
    "사서가 새 문서와 대조해 반영된 제안을 스스로 닫는다고 안내하라.\n"
    "- 슬랙 메세지이므로 핵심 위주로 간결하게 써라. 긴 목록은 묶어서 정리하라.\n"
    f"- 자료의 '{notion_parser.LINK_REFERENCE_PREFIX}' 줄은 다른 문서를 가리키는 링크일 뿐 그 문서의 내용이 아니다. "
    "질문과 관련 있을 때만 짧게 언급하고, 링크 너머의 내용을 추측하지 마라.\n"
    f"- 답변의 맨 마지막 줄에는 '{ai_engine.USED_DOCS_LABEL} 제목1 | 제목2' 형식으로, 답변에 실제로 사용한 문서의 제목만 적어라. "
    f"사용한 문서가 없으면 '{ai_engine.USED_DOCS_LABEL} 없음'이라고 적어라."
)
# 슬랙에서 훑어 읽기 쉽게 쓰는 표현 기준 (answer_style.py). 위의 내용 규칙을 바꾸지 않는다
SYSTEM_PROMPT += answer_style.STYLE_PROMPT
# QA 담당자의 'QA 할건데 뭘 볼까?' 같은 질문에 가볍게 답하는 규칙 (qa_lens.py)
SYSTEM_PROMPT += qa_lens.QA_PROMPT
# 정리본을 읽을 때 덧붙이는 안내 (config.AGENT_SOURCE == "refined")
REFINED_PROMPT = (
    "\n\n[자료에 대해]\n"
    "- search_docs/list_docs/read_document가 보여주는 자료는 사서가 노션 원본 기획서를 읽기 쉽게 다시 쓴 정리본이다. "
    "문서 제목과 항목 이름은 원본과 같다.\n"
    "- 정리본만으로 애매하거나, 원문 표현·수치를 그대로 확인해야 하면 read_original로 같은 문서의 원본을 읽어라."
)

TOOLS = [
    {"type": "function", "name": "search_docs",
     "description": "기획서 전체에서 질문과 관련된 부분(청크)을 찾는다. 문서 제목이나 DB 항목 이름(몹, 아이템, 각인 등)이 "
                    "검색어에 그대로 들어 있으면 그 항목을 우선 찾는다. 결과가 부족하면 다른 표현으로 다시 검색하라.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": "검색할 내용. 대화 맥락 없이도 이해되는 완전한 문장이나 키워드"}},
         "required": ["query"]}},
    {"type": "function", "name": "list_docs",
     "description": "인덱스된 기획서 제목과 각 문서의 DB 항목 이름 목록을 본다. 어떤 문서가 있는지 모르거나, "
                    "범주별로 문서를 찾아야 할 때(예: 챕터별 몹/보스 문서) 쓴다.",
     "parameters": {"type": "object", "properties": {
         "keyword": {"type": "string", "description": "제목이나 항목 이름에 들어 있어야 할 단어. 비우면 전체 목록"}}}},
    {"type": "function", "name": "read_document",
     "description": "기획서 한 문서를 처음부터 순서대로 읽는다. item을 주면 그 문서의 DB 항목 하나만 읽는다. "
                    "길면 여러 부분으로 나뉘며 part로 다음 부분을 읽는다.",
     "parameters": {"type": "object", "properties": {
         "title": {"type": "string", "description": "문서 제목 (list_docs나 검색 결과에 나온 제목)"},
         "item": {"type": "string", "description": "읽을 DB 항목 이름 (선택)"},
         "part": {"type": "integer", "description": "읽을 부분 번호, 1부터 (선택)"}},
         "required": ["title"]}},
]
REQUEST_SUMMARY_TOOL = {
    "type": "function", "name": "request_summary",
    "description": "여러 기획서에 흩어져 있어 모으기 어려웠던 주제의 종합 문서를 사서에게 요청한다. 사서가 초안을 써서 기획자에게 승인받는다. "
                   "답변에 필요한 자료를 다 찾은 뒤에 부른다.",
    "parameters": {"type": "object", "properties": {
        "topic": {"type": "string", "description": "종합 문서의 주제. 문서 제목처럼 짧게 (예: '챕터 1 적 구성')"},
        "reason": {"type": "string", "description": "어떤 정보가 어느 문서들에 흩어져 있었는지 한두 문장"},
        "docs": {"type": "array", "items": {"type": "string"}, "description": "정보를 모은 문서 이름들"}},
        "required": ["topic", "reason", "docs"]}}
EXPORT_MARKDOWN_TOOL = {
    "type": "function", "name": "export_markdown",
    "description": "노션에 붙여넣거나 가져올 수 있는 마크다운 파일을 만들어 답변 스레드에 첨부한다. "
                   "제목(#), 표, 코드 블록 등 일반 마크다운 문법으로 문서 전체를 쓴다. 슬랙 서식(*굵게*, <url|이름>)은 쓰지 않는다.",
    "parameters": {"type": "object", "properties": {
        "title": {"type": "string", "description": "파일 이름으로 쓸 문서 제목 (확장자 없이)"},
        "content": {"type": "string", "description": "문서 전체 마크다운"}},
        "required": ["title", "content"]}}
# 마크다운 파일을 첨부했을 때 답변 끝에 붙이는 안내
EXPORTED_NOTE = "📎 마크다운 파일로 첨부했어요. 내용을 복사해 노션에 붙여넣거나, 노션의 가져오기로 파일을 올리면 서식이 유지돼요."
# 사서에게 종합을 요청했을 때 답변 끝에 붙이는 안내
SUMMARY_REQUESTED_NOTE = "📚 여러 문서에 흩어진 정보라 사서에게 종합 문서 정리를 요청해 뒀어요."
READ_ORIGINAL_TOOL = {
    "type": "function", "name": "read_original",
    "description": "정리본이 아닌 노션 원본 기획서를 읽는다. 사용법은 read_document와 같다.",
    "parameters": TOOLS[2]["parameters"]}

# 도구 결과가 모델 입력을 지나치게 키우지 않도록 자르는 기준
SEARCH_RESULT_CHARS = 1000
READ_PART_CHARS = 12000
LIST_ITEMS_SHOWN = 30


class AgentState:
    """한 질문 동안 도구로 읽은 청크(사용한 문서/도판 선택용)와 도구 호출 기록"""
    def __init__(self, on_progress, collection: str = None):
        # 검색/목록/문서 읽기에 쓰는 컬렉션 (원본 또는 정리본)
        self.collection = collection or config.COLLECTION_NAME
        self.query = ""
        # 사서에게 요청한 종합 문서 주제
        self.summary_requests = []
        # 답변 스레드에 첨부할 마크다운 파일 ({"title", "content"})
        self.exports = []
        self.hits = {}
        self.calls = []
        self.query_vector = None
        self.on_progress = on_progress

    def add_hits(self, points):
        for p in points:
            self.hits.setdefault(p.id, p)

    def progress(self, text):
        if not self.on_progress:
            return
        try:
            self.on_progress(text)
        except Exception as e:
            # 진행 표시는 부가 기능이므로 실패해도 답변을 계속 만든다
            logger.warning("⚠️ [진행 표시] 실패: %s", e)


def chunk_body(payload) -> str:
    """청크 앞의 '문서: 제목 (위치: …)' 머리줄은 도구 결과에 문서 이름을 따로 붙이므로 뗀다"""
    text = payload.get("text", "")
    first, _, rest = text.partition("\n")
    return rest if first.startswith("문서: ") else text


def document_names(collection: str = None) -> Dict[str, str]:
    """page_id → 문서 이름 (list_docs/read_document에서 쓰는 이름. 같은 제목이 여러 곳에 있으면 상위 이름이 붙는다)"""
    return {page_id: name for name, doc in search.get_documents(collection).items() for page_id in doc["page_ids"]}


def linked_documents_note(query: str, collection: str = None) -> str:
    """질문에 붙은 노션 링크가 어느 기획서인지 알려 주는 안내 (모델은 링크의 ID만 보고는 문서를 알 수 없다). 링크가 없으면 빈 문자열"""
    page_ids = backlog.page_ids_in_text(query)
    if not page_ids:
        return ""
    names = document_names(collection)
    lines = [f"- {names[p]}: read_document로 읽어라" if p in names else
             f"- 기획서 목록에 없는 문서 ({p}): 아직 등록되지 않아 읽을 수 없다. "
             f"노션 문서의 {NOTION_SYNC_BUTTON} 버튼을 눌러 등록하면 읽을 수 있다고 안내하라"
             for p in page_ids]
    return "\n\n[질문에 붙은 노션 링크]\n" + "\n".join(lines)


def tool_search_docs(state: AgentState, query: str) -> str:
    state.progress(f"🔍 '{query}' 검색 중…")
    vector = clients.get_embedding(query)
    if state.query_vector is None:
        state.query_vector = vector
    results, _ = search.search_chunks(query, vector, state.collection)
    state.add_hits(results)
    if not results:
        return "검색 결과 없음"
    names = document_names(state.collection)
    lines = []
    for hit in results:
        p = hit.payload
        where = names.get(p.get("page_id"), p.get("title", "무제")) + (f" > {p['item']}" if p.get("item") else "")
        lines.append(f"[{where}]\n{chunk_body(p)[:SEARCH_RESULT_CHARS]}")
    return "\n\n".join(lines)


def tool_list_docs(state: AgentState, keyword: str = "") -> str:
    state.progress("🗂️ 기획서 목록 확인 중…")
    documents = search.get_documents(state.collection)
    key = search.normalize_name(keyword or "")
    lines = []
    for title in sorted(documents):
        items = documents[title]["items"]
        if key and key not in search.normalize_name(title):
            items = [i for i in items if key in search.normalize_name(i)]
            if not items:
                continue
        line = f"- {title}"
        if items:
            shown = ", ".join(items[:LIST_ITEMS_SHOWN])
            more = f" 외 {len(items) - LIST_ITEMS_SHOWN}개" if len(items) > LIST_ITEMS_SHOWN else ""
            line += f" (항목: {shown}{more})"
        lines.append(line)
    return "\n".join(lines) or f"'{keyword}'에 해당하는 문서 없음"


def find_title(title: str, documents: Dict[str, dict]):
    """정확한 제목 → 공백/대소문자 무시 → 부분 일치 순으로 문서를 찾는다"""
    if title in documents:
        return title
    key = search.normalize_name(title)
    normalized = {search.normalize_name(t): t for t in documents}
    if key in normalized:
        return normalized[key]
    partial = [t for n, t in normalized.items() if key and (key in n or n in key)]
    return partial[0] if len(partial) == 1 else None


def tool_read_document(state: AgentState, title: str, item: str = "", part: int = 1) -> str:
    return read_document(state, state.collection, title, item, part)


def tool_read_original(state: AgentState, title: str, item: str = "", part: int = 1) -> str:
    return read_document(state, config.COLLECTION_NAME, title, item, part, label="원본")


def read_document(state: AgentState, collection: str, title: str, item: str = "", part: int = 1, label: str = "문서") -> str:
    documents = search.get_documents(collection)
    found = find_title(title, documents)
    if not found:
        return f"'{title}' 문서를 찾을 수 없음. list_docs로 정확한 제목을 확인하라."
    state.progress(f"📖 '{found}' {'원본 ' if label == '원본' else ''}읽는 중…")
    page_ids = documents[found]["page_ids"]
    conditions = [FieldCondition(key="page_id", match=MatchAny(any=page_ids))]
    if item:
        items = documents[found]["items"]
        matched = item if item in items else next((i for i in items if search.normalize_name(item) in search.normalize_name(i)), None)
        if not matched:
            return f"'{found}' 문서에 '{item}' 항목이 없음. 항목 목록: {', '.join(items[:LIST_ITEMS_SHOWN])}"
        conditions.append(FieldCondition(key="item", match=MatchValue(value=matched)))
    points, offset = [], None
    while True:
        batch, offset = clients.get_qdrant().scroll(collection_name=collection, scroll_filter=Filter(must=conditions),
                                                    limit=256, offset=offset, with_payload=True, with_vectors=False)
        points += batch
        if offset is None:
            break
    points.sort(key=lambda p: (page_ids.index(p.payload["page_id"]), p.payload.get("chunk_idx", 0)))

    # 청크 경계를 넘지 않게 부분을 나눈다
    parts, current, size = [], [], 0
    for p in points:
        body = chunk_body(p.payload)
        if current and size + len(body) > READ_PART_CHARS:
            parts.append(current)
            current, size = [], 0
        current.append(p)
        size += len(body)
    if current:
        parts.append(current)
    if not parts:
        return f"'{found}' 문서에 내용이 없음"
    part = min(max(part or 1, 1), len(parts))
    state.add_hits(parts[part - 1])
    text = "\n".join(chunk_body(p.payload) for p in parts[part - 1])
    header = f"[{label}: {found}" + (f" > {item}" if item else "") + (f" | {part}/{len(parts)}부분]" if len(parts) > 1 else "]")
    return f"{header}\n{text}"


def tool_request_summary(state: AgentState, topic: str, reason: str = "", docs: List[str] = ()) -> str:
    if state.summary_requests:
        return "이 질문으로는 이미 요청했다."
    state.summary_requests.append(topic)
    # 평가 실행 등에서는 요청을 기록만 하고 실제로 올리지 않는다
    if config.SYNTHESIS_REQUESTS:
        import librarian
        librarian.enqueue_synthesis(topic, reason, state.query, list(docs))
    logger.info("📚 [종합 요청] %s (%s)", topic, reason)
    return "요청했다. 사서가 초안을 만들어 기획자에게 승인받는다. 이제 답하라."


def tool_export_markdown(state: AgentState, title: str, content: str) -> str:
    state.progress("📝 마크다운 파일 만드는 중…")
    state.exports.append({"title": title.strip() or "문서", "content": content})
    return "파일을 첨부했다. 답변에는 문서를 다시 쓰지 말고 무엇을 담았는지 짧게 말하라."


TOOL_FUNCTIONS = {"search_docs": tool_search_docs, "list_docs": tool_list_docs, "read_document": tool_read_document,
                  "read_original": tool_read_original, "request_summary": tool_request_summary,
                  "export_markdown": tool_export_markdown}


def run_tool(state: AgentState, name: str, arguments: str) -> str:
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return "도구 인자를 해석할 수 없음 (JSON 형식 오류)"
    func = TOOL_FUNCTIONS.get(name)
    if not func:
        return f"알 수 없는 도구: {name}"
    state.calls.append(f"{name}({', '.join(str(v) for v in args.values())})")
    logger.info("🛠️ [에이전트 도구] %s %s", name, args)
    try:
        return func(state, **args)
    except TypeError as e:
        return f"도구 인자가 잘못됨: {e}"


def ask_agent(query: str, chat_history: List[Dict[str, str]], progress: Dict[str, str],
              on_progress: Callable[[str], None] = None) -> Dict:
    refined = config.AGENT_SOURCE == "refined"
    state = AgentState(on_progress, config.REFINED_COLLECTION if refined else config.COLLECTION_NAME)
    state.query = query
    progress["stage"] = "자료 탐색"
    openai_client = clients.get_openai()
    options = {"model": config.AGENT_MODEL, "instructions": SYSTEM_PROMPT + (REFINED_PROMPT if refined else ""),
               "tools": TOOLS + ([READ_ORIGINAL_TOOL] if refined else []) + [REQUEST_SUMMARY_TOOL, EXPORT_MARKDOWN_TOOL],
               "reasoning": {"effort": config.AGENT_REASONING_EFFORT}}
    history = [{"role": h.get("role", "user"), "content": h.get("content", "")}
               for h in chat_history[-ai_engine.ANSWER_HISTORY_TURNS:]]
    question = query + linked_documents_note(query, state.collection)
    response = openai_client.responses.create(input=history + [{"role": "user", "content": question}], **options)
    usage = [response.usage]

    for round_no in range(1, config.AGENT_MAX_TOOL_ROUNDS + 1):
        calls = [item for item in response.output if item.type == "function_call"]
        if not calls:
            break
        outputs = [{"type": "function_call_output", "call_id": c.call_id, "output": run_tool(state, c.name, c.arguments)}
                   for c in calls]
        # 마지막 차례에는 도구를 더 쓰지 못하게 해 모은 자료로 답하게 한다
        final_round = round_no == config.AGENT_MAX_TOOL_ROUNDS
        response = openai_client.responses.create(previous_response_id=response.id, input=outputs,
                                                  **options, **({"tool_choice": "none"} if final_round else {}))
        usage.append(response.usage)

    input_tokens = sum(u.input_tokens for u in usage if u)
    output_tokens = sum(u.output_tokens for u in usage if u)
    logger.info("🤖 [에이전트] 호출 %d회, 도구 %d회, 토큰 입력 %d / 출력 %d", len(usage), len(state.calls), input_tokens, output_tokens)

    progress["stage"] = "답변 정리"
    reply = response.output_text or ""
    hits = list(state.hits.values())
    # DB 항목 청크는 답변에 그 항목 이름이 나올 때만 도판 후보로 쓴다 (문서 전체를 읽으면 다른 항목 도판까지 섞이므로)
    normalized_reply = search.normalize_name(reply)
    image_hits = [h for h in hits if not h.payload.get("item") or search.normalize_name(h.payload["item"]) in normalized_reply]
    result = ai_engine.finish_answer(query, reply, hits, image_hits, " | ".join(state.calls), state.query_vector)
    if state.summary_requests:
        result["AI_답변"] += f"\n\n{SUMMARY_REQUESTED_NOTE}"
    if state.exports:
        result["AI_답변"] += f"\n\n{EXPORTED_NOTE}"
    result["usage"] = {"input_tokens": input_tokens, "output_tokens": output_tokens, "tool_calls": len(state.calls)}
    result["summary_requests"] = state.summary_requests
    result["exports"] = state.exports
    return result
