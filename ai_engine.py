import os
import re
import uuid
from typing import List, Dict
from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance, PointStruct, Filter, FieldCondition, MatchValue, PayloadSchemaType, HasIdCondition
import notion_parser

# --- [1. 환경변수 및 API 클라이언트 세팅] ---
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
QDRANT_URL = os.environ.get("QDRANT_URL")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY")

openai_client = OpenAI(api_key=OPENAI_API_KEY)

# Qdrant Cloud 연결
if QDRANT_URL and QDRANT_API_KEY:
    client = QdrantClient(
        url=QDRANT_URL,
        api_key=QDRANT_API_KEY,
        timeout=15.0
    )
    print("🌐 [Qdrant] Qdrant Cloud 클러스터에 성공적으로 연결되었습니다.")
else:
    DB_PATH = os.path.abspath("./qdrant_db")
    client = QdrantClient(path=DB_PATH)
    print(f"💾 [Qdrant] 로컬 디스크 기반 DB를 사용합니다. ({DB_PATH})")

COLLECTION_NAME = "game_docs"

# --- [2. Qdrant 컬렉션 및 페이로드 인덱스 초기화] ---
if not client.collection_exists(COLLECTION_NAME):
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=1536, distance=Distance.COSINE)
    )
    print(f"✨ [Qdrant] '{COLLECTION_NAME}' 컬렉션이 새로 생성되었습니다.")

# Qdrant Cloud 필수: page_id 인덱스 생성
try:
    client.create_payload_index(
        collection_name=COLLECTION_NAME,
        field_name="page_id",
        field_schema=PayloadSchemaType.KEYWORD,
    )
    print("🔑 [Qdrant] 'page_id' 페이로드 인덱스 생성/확인 완료!")
except Exception:
    pass


# --- [3. 텍스트 임베딩 생성 함수] ---
EMBEDDING_BATCH_SIZE = 100

def get_embeddings(texts: List[str]) -> List[List[float]]:
    """OpenAI text-embedding-3-small 모델로 여러 텍스트를 한 번에 1536차원 벡터로 변환 (요청 한도를 넘지 않도록 배치 분할)"""
    vectors = []
    for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        batch = [t.replace("\n", " ") for t in texts[start:start + EMBEDDING_BATCH_SIZE]]
        response = openai_client.embeddings.create(
            input=batch,
            model="text-embedding-3-small"
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
        scroll_res = client.scroll(
            collection_name=COLLECTION_NAME,
            limit=1
        )
        points = scroll_res[0]
        target_title = points[0].payload.get("title", "공통 기획서") if points else "일반 기획서"
    except Exception:
        target_title = "일반 기획서"

    print(f"📝 [백로그 등록] 대상: {target_title} | 담당자: {assignee} | 내용: {content}")
    return target_title


# --- [4. 노션 문서 동기화 함수 (라인 단위 청킹 최적화)] ---
def sync_document_to_db(page_id: str, notion_token: str):
    """추측 배제! 파싱된 원시 데이터와 Qdrant 저장 청크 형태를 직접 검증하는 진단 로직"""
    print(f"\n==================== [1. 노션 원시 파싱 데이터 검증] ====================")
    doc_data = notion_parser.parse_notion_page_deep(page_id, notion_token)
    
    if not doc_data or not doc_data.get("full_text"):
        print(f"🚨 [실패] 노션 API에서 텍스트를 전혀 가져오지 못했습니다. (page_id: {page_id})")
        return False
        
    title = doc_data.get("title", "제목 없음")
    full_text = doc_data.get("full_text", "")
    
    print(f"📄 문서 제목: '{title}'")
    print(f"📏 총 글자 수: {len(full_text)} 자")
    print(f"----------------------------------------------------------------------")
    print(f"▼ [노션에서 파싱된 전체 원문 내용 출력 시작]")
    print(full_text)
    print(f"▲ [노션에서 파싱된 전체 원문 내용 출력 끝]")
    print(f"======================================================================\n")

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

    print(f"==================== [2. Qdrant에 저장될 청크 데이터 검증] ====================")
    print(f"🧩 생성된 총 청크 개수: {len(chunks)}개")
    for idx, chk in enumerate(chunks):
        print(f"\n--- [청크 #{idx+1}] ---")
        print(chk)
    print(f"======================================================================\n")

    # 임베딩을 모두 성공한 뒤에만 DB를 건드린다 (중간 실패 시 기존 데이터 보존)
    vectors = get_embeddings(chunks)

    points = []
    for idx, (chunk, vector) in enumerate(zip(chunks, vectors)):
        payload = {
            "page_id": page_id,
            "title": title,
            "text": chunk,
            "url": doc_data.get("url", ""),
            "chunk_idx": idx
        }
        points.append(PointStruct(id=chunk_point_id(page_id, idx), vector=vector, payload=payload))

    # 새 청크로 덮어쓴 뒤, 이번에 쓰지 않은 기존 청크(문서가 짧아져 남은 청크, 구버전 ID)만 삭제
    client.upsert(collection_name=COLLECTION_NAME, points=points)
    try:
        client.delete(
            collection_name=COLLECTION_NAME,
            points_selector=Filter(
                must=[FieldCondition(key="page_id", match=MatchValue(value=page_id))],
                must_not=[HasIdCondition(has_id=[p.id for p in points])]
            )
        )
    except Exception as e:
        print(f"⚠️ [Qdrant] 이전 청크 정리 실패 (새 청크는 저장됨, page_id: {page_id}): {e}")

    print(f"🎉 [동기화 완료] '{title}' ({len(points)}개 청크 Qdrant 저장 완료)\n")
    return True


# --- [6. AI Q&A 검색 및 답변 생성 함수] ---
def ask_ai_with_history(query: str, chat_history: List[Dict[str, str]]) -> Dict:
    """Qdrant Cloud 벡터 검색 후 OpenAI GPT 답변 생성. 도판은 검색된 page_ids로 호출부에서 노션에서 직접 가져온다."""
    query_vector = get_embedding(query)
    
    # 1. 벡터 코사인 유사도 검색 (Threshold 0.25 적용 및 검색 후보 10개 확장)
    response = client.query_points(
        collection_name=COLLECTION_NAME,
        query=query_vector,
        limit=10,
        score_threshold=0.25
    )
    search_results = response.points
    
    print(f"\n🔍 [Qdrant 검색 결과 개수]: {len(search_results)}개")
    for idx, hit in enumerate(search_results):
        print(f"   └ [{idx+1}] Score: {hit.score:.4f} | Title: {hit.payload.get('title')} | Text: {hit.payload.get('text')[:50]}...")

    if not search_results:
        return {
            "status": "success",
            "AI_답변": "죄송하지만, 질문과 관련된 기획서 내용을 찾을 수 없습니다.",
            "page_ids": []
        }

    context_blocks = []
    page_ids = []
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
        completion = openai_client.chat.completions.create(
            model="gpt-4o-mini",
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
        "page_ids": page_ids
    }