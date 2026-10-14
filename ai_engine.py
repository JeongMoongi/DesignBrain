import logging
import threading
import uuid
from typing import List, Dict
import openai
from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.http import exceptions as qdrant_exceptions
from qdrant_client.models import VectorParams, Distance, PointStruct, Filter, FieldCondition, MatchValue, PayloadSchemaType, HasIdCondition
import config
import notion_parser
# backlog도 ai_engine을 import한다. 서로 함수 안에서만 참조하므로 순환 import 문제는 없다
import backlog

logger = logging.getLogger(__name__)

# --- [1. API 클라이언트 (import 시점이 아니라 처음 사용할 때 생성)] ---
COLLECTION_NAME = config.COLLECTION_NAME

_openai_client = None
_qdrant_client = None
# 웹훅 백그라운드 작업과 슬랙 스레드가 동시에 처음 호출해도 클라이언트는 한 번만 만든다 (로컬 DB는 중복 연결 불가)
_client_lock = threading.Lock()

def get_openai() -> OpenAI:
    global _openai_client
    with _client_lock:
        if _openai_client is None:
            _openai_client = OpenAI(api_key=config.OPENAI_API_KEY)
        return _openai_client

def get_qdrant() -> QdrantClient:
    global _qdrant_client
    with _client_lock:
        if _qdrant_client is None:
            _qdrant_client = _connect_qdrant()
        return _qdrant_client

def close_clients():
    """서버 종료 시 연결 정리 (로컬 DB는 닫아야 다음 실행에서 잠금 오류가 나지 않는다)"""
    global _openai_client, _qdrant_client
    with _client_lock:
        if _qdrant_client is not None:
            _qdrant_client.close()
            _qdrant_client = None
        if _openai_client is not None:
            _openai_client.close()
            _openai_client = None

def _connect_qdrant() -> QdrantClient:
    if config.QDRANT_URL and config.QDRANT_API_KEY:
        client = QdrantClient(url=config.QDRANT_URL, api_key=config.QDRANT_API_KEY, timeout=15.0)
        logger.info("🌐 [Qdrant] Qdrant Cloud 클러스터에 연결되었습니다.")
    else:
        client = QdrantClient(path=config.QDRANT_LOCAL_PATH)
        logger.info("💾 [Qdrant] 로컬 디스크 기반 DB를 사용합니다. (%s)", config.QDRANT_LOCAL_PATH)

    # --- [2. Qdrant 컬렉션 및 페이로드 인덱스 초기화] ---
    # Qdrant Cloud는 필터에 쓰는 필드에 인덱스가 필요하다
    for collection, indexed_fields in ((COLLECTION_NAME, ["page_id"]),
                                       (config.BACKLOG_COLLECTION, ["status", "assignee_id", "page_ids"])):
        if not client.collection_exists(collection):
            client.create_collection(
                collection_name=collection,
                vectors_config=VectorParams(size=config.EMBEDDING_DIM, distance=Distance.COSINE)
            )
            logger.info("✨ [Qdrant] '%s' 컬렉션이 새로 생성되었습니다.", collection)

        for field in indexed_fields:
            try:
                client.create_payload_index(collection_name=collection, field_name=field, field_schema=PayloadSchemaType.KEYWORD)
                logger.debug("🔑 [Qdrant] '%s.%s' 페이로드 인덱스 생성/확인 완료", collection, field)
            except Exception as e:
                # 이미 존재하는 경우 등
                logger.debug("[Qdrant] %s.%s 인덱스 생성 생략: %s", collection, field, e)
    return client


# --- [3. 텍스트 임베딩 생성 함수] ---
EMBEDDING_BATCH_SIZE = 100

def get_embeddings(texts: List[str]) -> List[List[float]]:
    """OpenAI 임베딩 모델로 여러 텍스트를 한 번에 1536차원 벡터로 변환 (요청 한도를 넘지 않도록 배치 분할)"""
    vectors = []
    for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        batch = [t.replace("\n", " ") for t in texts[start:start + EMBEDDING_BATCH_SIZE]]
        response = get_openai().embeddings.create(
            input=batch,
            model=config.EMBEDDING_MODEL
        )
        vectors.extend(d.embedding for d in sorted(response.data, key=lambda d: d.index))
    return vectors

def get_embedding(text: str) -> List[float]:
    return get_embeddings([text])[0]

def chunk_point_id(page_id: str, idx: int) -> str:
    """같은 문서의 같은 청크는 항상 같은 ID가 되도록 결정적 UUID 사용 (재동기화 시 덮어쓰기)"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"notion:{page_id}:{idx}"))




# --- [4-1. 청킹: 일반 텍스트는 줄 단위로 묶고, 표 행/DB 항목은 데이터 단위로 하나씩] ---
TEXT_CHUNK_CHARS = 250
# 임베딩 입력 한도를 넘지 않도록 아주 긴 데이터 단위만 나눈다 (현재 최장 DB 항목은 약 1,250자)
MAX_UNIT_CHARS = 2000

def split_long_unit(unit: str) -> List[str]:
    """긴 데이터 단위를 ' / ' 경계에서 나누고, 조각마다 항목명을 앞에 붙여 어느 항목인지 알 수 있게 한다."""
    if len(unit) <= MAX_UNIT_CHARS:
        return [unit]
    head = unit.split(" ➔ ", 1)[0] + " ➔ (이어서) " if " ➔ " in unit else ""
    pieces, current = [], ""
    for part in unit.split(" / "):
        candidate = f"{current} / {part}" if current else part
        if current and len(head) + len(candidate) > MAX_UNIT_CHARS:
            pieces.append(current)
            current = part
        else:
            current = candidate
    pieces.append(current)
    # 조각 하나가 그래도 길면 글자 수로 자른다
    limit = MAX_UNIT_CHARS - len(head)
    pieces = [p[i:i + limit] for p in pieces for i in range(0, len(p), limit)]
    return [pieces[0]] + [head + p for p in pieces[1:]]

def split_into_chunks(title: str, full_text: str) -> List[str]:
    doc_header = f"문서: {title}"
    chunks = []
    text_lines = []
    in_unit_section = False
    section_label = ""

    def flush_text():
        if text_lines:
            chunks.append("\n".join([doc_header] + text_lines))
            text_lines.clear()

    for raw in full_text.split("\n"):
        line = raw.strip()
        if not line:
            # 데이터 단위 섹션(표/DB)은 빈 줄에서 끝난다
            in_unit_section = False
            continue

        if line.startswith((notion_parser.TABLE_MARKER_PREFIX, notion_parser.DB_MARKER_PREFIX)):
            flush_text()
            in_unit_section = True
            # DB 이름 / 표 위치(제목 경로)를 각 데이터 단위 청크에 붙여 같은 내용의 행도 소속을 구분한다
            # 위치 정보가 없는 표 표시는 정보가 없으므로 생략
            section_label = "" if line == notion_parser.table_marker() else line.strip("#📊 ")
            continue

        if in_unit_section:
            flush_text()
            for piece in split_long_unit(line):
                chunks.append("\n".join(filter(None, [doc_header, section_label, piece])))
            continue

        if text_lines and len(doc_header) + sum(len(l) + 1 for l in text_lines) + len(line) > TEXT_CHUNK_CHARS:
            flush_text()
        text_lines.append(line)

    flush_text()
    return chunks


# --- [4-2. 노션 문서 동기화 함수] ---
# 같은 문서의 동기화/삭제는 하나씩만 실행한다. 동기화(수십 초) 도중 들어온 삭제가 먼저 끝나고
# 뒤늦게 동기화가 저장해 휴지통 문서가 되살아나는 것을 막기 위함
_page_locks = {}
_page_locks_guard = threading.Lock()

def _page_lock(page_id: str) -> threading.Lock:
    with _page_locks_guard:
        return _page_locks.setdefault(page_id, threading.Lock())

def sync_document_to_db(page_id: str, notion_token: str):
    """노션 문서를 파싱해 청크로 나눠 Qdrant에 저장. 휴지통 문서는 저장하지 않고 인덱스에서 제거한다."""
    with _page_lock(page_id):
        return _sync_document(page_id, notion_token)

def _sync_document(page_id: str, notion_token: str):
    doc_data = notion_parser.parse_notion_page_deep(page_id, notion_token)

    if doc_data and doc_data.get("trashed"):
        logger.info("🗑️ [동기화 생략] 휴지통에 있는 문서입니다. 인덱스에서 제거합니다. (page_id: %s)", page_id)
        _delete_page_points(page_id)
        return False

    if not doc_data or not doc_data.get("full_text"):
        logger.error("🚨 [동기화 실패] 노션 API에서 텍스트를 가져오지 못했습니다. (page_id: %s)", page_id)
        return False
        
    title = doc_data.get("title", "제목 없음")
    full_text = doc_data.get("full_text", "")
    
    logger.info("📄 [동기화] '%s' 파싱 완료 (%d자)", title, len(full_text))
    logger.debug("▼ [파싱 원문: %s]\n%s\n▲ [파싱 원문 끝]", title, full_text)

    chunks = split_into_chunks(title, full_text)

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("🧩 [청크 %d개: %s]\n%s", len(chunks), title,
                     "\n".join(f"--- [청크 #{idx+1}] ---\n{chk}" for idx, chk in enumerate(chunks)))

    # 임베딩을 모두 성공한 뒤에만 DB를 건드린다 (중간 실패 시 기존 데이터 보존)
    vectors = get_embeddings(chunks)

    points = []
    for idx, (chunk, vector) in enumerate(zip(chunks, vectors)):
        payload = {
            "page_id": page_id,
            "title": title,
            "text": chunk,
            "url": doc_data.get("url", ""),
            "chunk_idx": idx,
            # 이미지 block_id/캡션 캐시: 답변 시 페이지 전체를 다시 훑지 않고 이 블록들만 조회해 새 URL을 받는다
            "images": doc_data.get("images", [])
        }
        points.append(PointStruct(id=chunk_point_id(page_id, idx), vector=vector, payload=payload))

    # 파싱/임베딩 도중 휴지통으로 옮겨졌을 수 있으므로 저장 직전에 다시 확인 (확인 실패 시 예외로 저장 중단)
    if notion_parser.is_page_trashed(page_id, notion_token):
        logger.info("🗑️ [동기화 취소] 동기화 중 휴지통으로 옮겨진 문서입니다. 인덱스에서 제거합니다. (page_id: %s)", page_id)
        _delete_page_points(page_id)
        return False

    # 새 청크로 덮어쓴 뒤, 이번에 쓰지 않은 기존 청크(문서가 짧아져 남은 청크, 구버전 ID)만 삭제
    get_qdrant().upsert(collection_name=COLLECTION_NAME, points=points)
    try:
        get_qdrant().delete(
            collection_name=COLLECTION_NAME,
            points_selector=Filter(
                must=[FieldCondition(key="page_id", match=MatchValue(value=page_id))],
                must_not=[HasIdCondition(has_id=[p.id for p in points])]
            )
        )
    except Exception as e:
        logger.warning("⚠️ [Qdrant] 이전 청크 정리 실패 (새 청크는 저장됨, page_id: %s): %s", page_id, e)

    logger.info("🎉 [동기화 완료] '%s' (%d개 청크 저장)", title, len(points))
    return True


def delete_document_from_db(page_id: str):
    """노션에서 삭제된 문서의 모든 청크를 인덱스에서 제거 (진행 중인 같은 문서 동기화가 끝난 뒤 실행)"""
    with _page_lock(page_id):
        _delete_page_points(page_id)

def _delete_page_points(page_id: str):
    get_qdrant().delete(
        collection_name=COLLECTION_NAME,
        points_selector=Filter(must=[FieldCondition(key="page_id", match=MatchValue(value=page_id))])
    )
    logger.info("🗑️ [삭제 완료] page_id: %s 의 청크를 Qdrant에서 제거했습니다.", page_id)


# --- [6. AI Q&A 검색 및 답변 생성 함수] ---
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
    """Qdrant Cloud 벡터 검색 후 OpenAI GPT 답변 생성. 도판은 검색된 page_ids/page_images로 호출부에서 노션에서 직접 가져온다.
    실패하면 status=error 와 함께 실패 단계(stage), 원인(reason), 대처 안내(hint)를 돌려준다."""
    progress = {"stage": "질문 분석"}
    try:
        return _ask_ai(query, chat_history, progress)
    except Exception as e:
        logger.exception("🚨 [답변 실패] 단계: %s | 질문: %s", progress["stage"], query)
        return {"status": "error", "stage": progress["stage"], **describe_failure(e)}

REWRITE_PROMPT = (
    "너는 기획서 검색어 재작성기다. [대화 기록]과 [마지막 질문]을 보고, 마지막 질문을 대화 맥락 없이도 "
    "이해되는 완전한 한 문장 질문으로 다시 써라.\n"
    "- 생략된 대상(문서명, 캐릭터명, 챕터, 시스템명 등)을 대화 기록에서 찾아 채워 넣어라. "
    "마지막 질문에 무엇에 대한 질문인지(주어)가 없으면 반드시 대화의 주제를 넣어라. "
    "(예: '아이템 각인은 어떻게 해?' → '비용은 얼마야?' = '아이템 각인 비용은 얼마야?')\n"
    "- 'X는?'처럼 대상만 바꾼 질문이면, 직전 질문과 같은 내용을 X에 대해 묻는 질문으로 써라. "
    "X를 직전 대상의 일부로 붙이지 마라. (예: 'A 상점에 뭐가 있어?' → '로비는?' = 'A 로비에는 뭐가 있어?')\n"
    "- 마지막 질문에 나온 대상(이름, 챕터 번호 등)은 절대 빼거나 다른 것으로 바꾸지 마라. 대화 기록은 빠진 부분을 채우는 데만 써라.\n"
    "- 마지막 질문이 이미 독립적이면 그대로 출력하라.\n"
    "- 질문 한 문장만 출력하라."
)
# 재작성 입력이 길어지지 않도록 최근 대화만, 긴 답변은 앞부분만 넘긴다
REWRITE_HISTORY_TURNS = 4
REWRITE_MESSAGE_CHARS = 500

def rewrite_search_query(query: str, chat_history: List[Dict[str, str]]) -> str:
    """'그럼 챕터 2는?' 같은 후속 질문을 대화 맥락으로 채워 검색용 질문으로 바꾼다.
    스레드 첫 질문은 그대로 쓰고, 재작성에 실패해도 원래 질문으로 검색을 이어간다."""
    if not chat_history:
        return query
    history = "\n".join(f"{h.get('role', 'user')}: {h.get('content', '')[:REWRITE_MESSAGE_CHARS]}"
                        for h in chat_history[-REWRITE_HISTORY_TURNS:])
    try:
        completion = get_openai().chat.completions.create(
            model=config.CHAT_MODEL,
            messages=[{"role": "system", "content": REWRITE_PROMPT},
                      {"role": "user", "content": f"[대화 기록]\n{history}\n\n[마지막 질문]\n{query}"}],
            temperature=0
        )
        rewritten = (completion.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning("⚠️ [검색어 재작성] 실패, 원래 질문으로 검색합니다: %s", e)
        return query
    if not rewritten:
        return query
    logger.info("✏️ [검색어 재작성] %s → %s", query, rewritten)
    return rewritten

def _ask_ai(query: str, chat_history: List[Dict[str, str]], progress: Dict[str, str]) -> Dict:
    query_vector = get_embedding(rewrite_search_query(query, chat_history))

    # 1. 벡터 코사인 유사도 검색 (Threshold 0.25 적용 및 검색 후보 10개 확장)
    progress["stage"] = "기획서 검색"
    response = get_qdrant().query_points(
        collection_name=COLLECTION_NAME,
        query=query_vector,
        limit=10,
        score_threshold=0.25
    )
    search_results = response.points
    
    logger.info("🔍 [Qdrant 검색] %d개 결과", len(search_results))
    for idx, hit in enumerate(search_results):
        logger.debug("   └ [%d] Score: %.4f | Title: %s | Text: %s...", idx + 1, hit.score, hit.payload.get("title"), hit.payload.get("text", "")[:50])

    if not search_results:
        return {
            "status": "success",
            "AI_답변": "죄송하지만, 질문과 관련된 기획서 내용을 찾을 수 없습니다.",
            "page_ids": [],
            "page_images": {}
        }

    context_blocks = []
    page_ids = []
    page_images = {}
    doc_urls = set()

    for hit in search_results:
        payload = hit.payload
        text = payload.get("text", "")
        title = payload.get("title", "무제")
        url = payload.get("url", "")
        page_id = payload.get("page_id")

        context_blocks.append(f"[참고 문서: {title}]\n{text}")
        if url: doc_urls.add(f"[{title}]({url})")
        # 유사도 순서 유지하며 중복 제거
        if page_id and page_id not in page_ids: page_ids.append(page_id)
        # 이미지 캐시가 없는 청크(이전 버전으로 동기화됨)는 page_images에 넣지 않아 호출부가 전체 탐색으로 폴백한다
        if page_id and "images" in payload: page_images.setdefault(page_id, payload["images"])

    context_str = "\n\n".join(context_blocks)
    doc_urls_str = "\n- 참고 문서 : " + ", ".join(doc_urls) if doc_urls else ""

    # 2. GPT 프롬프트 구성
    system_prompt = (
        "너는 게임 기획팀의 친근하고 유능한 AI 어시스턴트 '@Brain'이다.\n"
        "제공된 기획서 [컨텍스트] 및 표(Table) 데이터에 기반하여 사용자의 질문에 정확하고 상세하게 답변하라.\n"
        "컨텍스트의 표나 텍스트에 있는 항목명과 수치는 원문 그대로 정확하게 인용하라."
    )
    
    messages = [{"role": "system", "content": system_prompt}]
    
    for h in chat_history[-5:]:
        messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
        
    messages.append({"role": "user", "content": f"[컨텍스트]\n{context_str}\n\n[질문]\n{query}"})

    # 3. GPT 답변 생성
    progress["stage"] = "답변 생성"
    completion = get_openai().chat.completions.create(
        model=config.CHAT_MODEL,
        messages=messages,
        temperature=0.1
    )
    ai_reply = completion.choices[0].message.content

    # 4. 관련 미해결 백로그 참고 문구 (실패해도 답변은 그대로 보낸다)
    try:
        notes = backlog.notes_for_answer(query_vector, page_ids)
    except Exception as e:
        logger.warning("⚠️ [백로그] 참고 문구 조회 실패, 답변만 보냅니다: %s", e)
        notes = []
    if notes:
        ai_reply += "\n\n" + "\n".join(f"📌 참고 : {note}" for note in notes)

    if doc_urls_str:
        ai_reply += f"\n{doc_urls_str}"

    return {
        "status": "success",
        "AI_답변": ai_reply,
        "page_ids": page_ids,
        "page_images": page_images
    }