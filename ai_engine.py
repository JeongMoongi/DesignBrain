import logging
import threading
import uuid
from typing import List, Dict
from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance, PointStruct, Filter, FieldCondition, MatchValue, PayloadSchemaType, HasIdCondition
import config
import notion_parser

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
    if not client.collection_exists(COLLECTION_NAME):
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=config.EMBEDDING_DIM, distance=Distance.COSINE)
        )
        logger.info("✨ [Qdrant] '%s' 컬렉션이 새로 생성되었습니다.", COLLECTION_NAME)

    # Qdrant Cloud 필수: page_id 인덱스 생성
    try:
        client.create_payload_index(
            collection_name=COLLECTION_NAME,
            field_name="page_id",
            field_schema=PayloadSchemaType.KEYWORD,
        )
        logger.debug("🔑 [Qdrant] 'page_id' 페이로드 인덱스 생성/확인 완료")
    except Exception as e:
        # 이미 존재하는 경우 등
        logger.debug("[Qdrant] page_id 인덱스 생성 생략: %s", e)
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




# --- [5. 백로그 등록 함수] ---
def add_backlog(assignee: str, content: str) -> str:
    """백로그 등록 핸들러"""
    try:
        scroll_res = get_qdrant().scroll(
            collection_name=COLLECTION_NAME,
            limit=1
        )
        points = scroll_res[0]
        target_title = points[0].payload.get("title", "공통 기획서") if points else "일반 기획서"
    except Exception:
        target_title = "일반 기획서"

    logger.info("📝 [백로그 등록] 대상: %s | 담당자: %s | 내용: %s", target_title, assignee, content)
    return target_title


# --- [4. 노션 문서 동기화 함수 (라인 단위 청킹 최적화)] ---
def sync_document_to_db(page_id: str, notion_token: str):
    """노션 문서를 파싱해 청크로 나눠 Qdrant에 저장. 파싱 원문과 청크 전체는 DEBUG 로그로 확인할 수 있다."""
    doc_data = notion_parser.parse_notion_page_deep(page_id, notion_token)
    
    if not doc_data or not doc_data.get("full_text"):
        logger.error("🚨 [동기화 실패] 노션 API에서 텍스트를 가져오지 못했습니다. (page_id: %s)", page_id)
        return False
        
    title = doc_data.get("title", "제목 없음")
    full_text = doc_data.get("full_text", "")
    
    logger.info("📄 [동기화] '%s' 파싱 완료 (%d자)", title, len(full_text))
    logger.debug("▼ [파싱 원문: %s]\n%s\n▲ [파싱 원문 끝]", title, full_text)

    # 라인 분할 및 청크 형태 확인
    lines = [line.strip() for line in full_text.split("\n") if line.strip()]
    chunks = []
    current_chunk = f"문서: {title}\n"

    for line in lines:
        if len(current_chunk) + len(line) > 250 or "|" in line:
            if len(current_chunk.strip()) > len(f"문서: {title}"):
                chunks.append(current_chunk.strip())
            current_chunk = f"문서: {title}\n{line}\n"
        else:
            current_chunk += f"{line}\n"

    if len(current_chunk.strip()) > len(f"문서: {title}"):
        chunks.append(current_chunk.strip())

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
    """노션에서 삭제된 문서의 모든 청크를 인덱스에서 제거"""
    get_qdrant().delete(
        collection_name=COLLECTION_NAME,
        points_selector=Filter(must=[FieldCondition(key="page_id", match=MatchValue(value=page_id))])
    )
    logger.info("🗑️ [삭제 완료] page_id: %s 의 청크를 Qdrant에서 제거했습니다.", page_id)


# --- [6. AI Q&A 검색 및 답변 생성 함수] ---
def ask_ai_with_history(query: str, chat_history: List[Dict[str, str]]) -> Dict:
    """Qdrant Cloud 벡터 검색 후 OpenAI GPT 답변 생성. 도판은 검색된 page_ids/page_images로 호출부에서 노션에서 직접 가져온다."""
    query_vector = get_embedding(query)
    
    # 1. 벡터 코사인 유사도 검색 (Threshold 0.25 적용 및 검색 후보 10개 확장)
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
    try:
        completion = get_openai().chat.completions.create(
            model=config.CHAT_MODEL,
            messages=messages,
            temperature=0.1
        )
        ai_reply = completion.choices[0].message.content
        if doc_urls_str:
            ai_reply += f"\n{doc_urls_str}"
    except Exception as e:
        return {"status": "error", "message": f"GPT 답변 생성 실패: {e}"}

    return {
        "status": "success",
        "AI_답변": ai_reply,
        "page_ids": page_ids,
        "page_images": page_images
    }