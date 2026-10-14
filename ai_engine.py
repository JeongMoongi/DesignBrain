"""기획서 Q&A 답변 생성: 검색(search) 결과로 GPT 답변을 만들고, 사용한 문서/도판/백로그 참고 문구를 붙인다"""
import logging
import re
from typing import List, Dict
import openai
from qdrant_client.http import exceptions as qdrant_exceptions
import backlog
import clients
import config
import notion_parser
import search

logger = logging.getLogger(__name__)

# --- [실패 안내] ---
RETRY_HINT = "잠시 후 다시 질문해 주세요."
ADMIN_HINT = "계속 실패하면 관리자에게 알려주세요."

def describe_failure(e: Exception) -> Dict[str, str]:
    """예외를 슬랙 사용자에게 보여줄 원인/대처 안내로 바꾼다"""
    if isinstance(e, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return {"reason": "OpenAI API 키가 유효하지 않거나 권한이 없습니다.", "hint": "관리자에게 알려주세요."}
    if isinstance(e, openai.RateLimitError):
        if getattr(e, "code", None) == "insufficient_quota":
            return {"reason": "OpenAI 크레딧(사용 한도)이 소진되었습니다.", "hint": "관리자에게 알려주세요."}
        return {"reason": "OpenAI에 요청이 너무 많이 몰렸습니다.", "hint": RETRY_HINT}
    if isinstance(e, openai.APITimeoutError):
        return {"reason": "OpenAI 응답이 너무 오래 걸려 중단되었습니다.", "hint": RETRY_HINT}
    if isinstance(e, openai.APIConnectionError):
        return {"reason": "OpenAI 서버에 연결하지 못했습니다.", "hint": RETRY_HINT}
    if isinstance(e, openai.InternalServerError):
        return {"reason": "OpenAI 서버에 일시적인 문제가 있습니다.", "hint": RETRY_HINT}
    if isinstance(e, openai.BadRequestError):
        return {"reason": "OpenAI가 요청을 거부했습니다. (대화나 질문이 너무 길 수 있습니다)", "hint": "새 스레드에서 질문을 짧게 다시 해 주세요."}
    if isinstance(e, openai.APIStatusError):
        return {"reason": f"OpenAI 오류가 발생했습니다. (HTTP {e.status_code})", "hint": ADMIN_HINT}
    if isinstance(e, qdrant_exceptions.UnexpectedResponse):
        if e.status_code in (401, 403):
            return {"reason": "기획서 DB(Qdrant) 인증에 실패했습니다.", "hint": "관리자에게 알려주세요."}
        return {"reason": f"기획서 DB(Qdrant)가 오류를 반환했습니다. (HTTP {e.status_code})", "hint": ADMIN_HINT}
    if isinstance(e, qdrant_exceptions.ResponseHandlingException):
        return {"reason": "기획서 DB(Qdrant)에 연결하지 못했습니다.", "hint": RETRY_HINT}
    return {"reason": f"예상하지 못한 오류가 발생했습니다. ({type(e).__name__})", "hint": ADMIN_HINT}

def ask_ai_with_history(query: str, chat_history: List[Dict[str, str]]) -> Dict:
    """Qdrant Cloud 벡터 검색 후 OpenAI GPT 답변 생성. 도판은 검색된 images(block_id 목록)로 호출부에서 노션에서 직접 가져온다.
    실패하면 status=error 와 함께 실패 단계(stage), 원인(reason), 대처 안내(hint)를 돌려준다."""
    progress = {"stage": "질문 분석"}
    try:
        return _ask_ai(query, chat_history, progress)
    except Exception as e:
        logger.exception("🚨 [답변 실패] 단계: %s | 질문: %s", progress["stage"], query)
        return {"status": "error", "stage": progress["stage"], **describe_failure(e)}

# 답변 생성 시 함께 넘기는 최근 대화 수 (질문/답변 각각 1개로 센다)
ANSWER_HISTORY_TURNS = 5

USED_DOCS_LABEL = "사용한 문서:"
# GPT가 '**사용한 문서:** …'처럼 마크다운으로 꾸미는 경우가 있다. 제목 끝의 따옴표(’) 등은 지우면 안 되므로 마크다운 기호만 뗀다
MARKDOWN_MARKS = " *_`"
USED_DOCS_PATTERN = re.compile(rf"^[\s*_`>-]*{USED_DOCS_LABEL}(.*)$")

def split_used_docs(reply: str, search_results: list) -> tuple:
    """답변 끝의 '사용한 문서:' 줄을 떼어 내고, 그 문서들의 검색 결과만 돌려준다 (참고 문서 링크/이미지/백로그용).
    줄이 없으면 검색 1위 문서를, '없음'이면 빈 목록을 쓴다. 검색 결과에 없는 제목은 무시한다."""
    lines = reply.rstrip().split("\n")
    match = USED_DOCS_PATTERN.match(lines[-1].strip()) if lines else None
    if not match:
        logger.info("📎 [사용한 문서] 표시가 없어 검색 1위 문서만 참고 문서로 씁니다.")
        top_page = search_results[0].payload.get("page_id")
        return reply, [hit for hit in search_results if hit.payload.get("page_id") == top_page]
    cited = {search.normalize_name(title.strip(MARKDOWN_MARKS)) for title in match.group(1).split("|")}
    used = [hit for hit in search_results if search.normalize_name(hit.payload.get("title", "")) in cited]
    logger.info("📎 [사용한 문서] %s", match.group(1).strip(MARKDOWN_MARKS))
    return "\n".join(lines[:-1]).rstrip(), used

def _ask_ai(query: str, chat_history: List[Dict[str, str]], progress: Dict[str, str]) -> Dict:
    search_query = search.rewrite_search_query(query, chat_history)
    query_vector = clients.get_embedding(search_query)

    # 1. 이름 일치 + 벡터 코사인 유사도 검색
    progress["stage"] = "기획서 검색"
    search_results, named_count = search.search_chunks(search_query, query_vector)

    logger.info("🔍 [Qdrant 검색] %d개 결과", len(search_results))
    for idx, hit in enumerate(search_results):
        logger.debug("   └ [%d] Score: %.4f | Title: %s | Text: %s...", idx + 1, hit.score, hit.payload.get("title"), hit.payload.get("text", "")[:50])

    if not search_results:
        return {
            "status": "success",
            "AI_답변": "죄송하지만, 질문과 관련된 기획서 내용을 찾을 수 없습니다.",
            "page_ids": [],
            "images": []
        }

    context_str = "\n\n".join(f"[참고 문서: {hit.payload.get('title', '무제')}]\n{hit.payload.get('text', '')}"
                              for hit in search_results)

    # 2. GPT 프롬프트 구성
    system_prompt = (
        "너는 게임 기획팀의 친근하고 유능한 AI 어시스턴트 '@Brain'이다.\n"
        "제공된 기획서 [컨텍스트] 및 표(Table) 데이터에 기반하여 사용자의 질문에 정확하고 상세하게 답변하라.\n"
        "컨텍스트의 표나 텍스트에 있는 항목명과 수치는 원문 그대로 정확하게 인용하라.\n"
        f"컨텍스트의 '{notion_parser.LINK_REFERENCE_PREFIX}' 줄은 다른 문서를 가리키는 링크일 뿐 그 문서의 내용이 아니다. "
        "질문과 관련 있을 때만 '참고 자료로 [제목](주소)도 있어요'처럼 짧게 언급하고, 링크 너머의 내용을 추측하지 마라.\n"
        f"답변의 맨 마지막 줄에는 '{USED_DOCS_LABEL} 제목1 | 제목2' 형식으로, 답변에 실제로 사용한 [참고 문서]의 제목만 적어라. "
        f"사용한 문서가 없으면 '{USED_DOCS_LABEL} 없음'이라고 적어라."
    )
    
    messages = [{"role": "system", "content": system_prompt}]
    
    for h in chat_history[-ANSWER_HISTORY_TURNS:]:
        messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
        
    messages.append({"role": "user", "content": f"[컨텍스트]\n{context_str}\n\n[질문]\n{query}"})

    # 3. GPT 답변 생성
    progress["stage"] = "답변 생성"
    completion = clients.get_openai().chat.completions.create(
        model=config.CHAT_MODEL,
        messages=messages,
        temperature=0.1
    )
    ai_reply, used_hits = split_used_docs(completion.choices[0].message.content or "", search_results)
    used_page_ids = list(dict.fromkeys(hit.payload["page_id"] for hit in used_hits if hit.payload.get("page_id")))

    # 이름이 일치한 문서/항목이 있으면 그 이미지만, 그중에서도 답변에 사용한 문서의 이미지만 쓴다
    image_hits = search_results[:named_count] if named_count else search_results
    used_ids = {hit.id for hit in used_hits}
    image_hits = [hit for hit in image_hits if hit.id in used_ids]
    images, seen_block_ids = [], set()
    for hit in image_hits:
        for img in hit.payload.get("images", []):
            if img.get("block_id") and img["block_id"] not in seen_block_ids:
                seen_block_ids.add(img["block_id"])
                images.append(img)

    # 4. 관련 미해결 백로그 참고 문구 (실패해도 답변은 그대로 보낸다)
    try:
        notes = backlog.notes_for_answer(query_vector, used_page_ids)
    except Exception as e:
        logger.warning("⚠️ [백로그] 참고 문구 조회 실패, 답변만 보냅니다: %s", e)
        notes = []
    if notes:
        ai_reply += "\n\n" + "\n".join(f"📌 참고 : {note}" for note in notes)

    # 검색 순서를 유지하며 문서당 링크 하나
    doc_urls = list(dict.fromkeys(f"[{hit.payload.get('title', '무제')}]({hit.payload['url']})"
                                  for hit in used_hits if hit.payload.get("url")))
    if doc_urls:
        ai_reply += "\n\n- 참고 문서 : " + ", ".join(doc_urls)

    return {
        "status": "success",
        "AI_답변": ai_reply,
        "page_ids": used_page_ids,
        "images": images
    }
