"""OpenAI/Qdrant 클라이언트와 임베딩. 클라이언트는 import 시점이 아니라 처음 사용할 때 만든다.
다른 모듈은 clients.get_qdrant()처럼 모듈 이름을 붙여 호출한다 (테스트에서 한 곳만 바꿔치기하면 되도록)."""
import logging
import threading
from typing import List
from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance, PayloadSchemaType
import config

logger = logging.getLogger(__name__)

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

    # Qdrant 컬렉션 및 페이로드 인덱스 초기화
    # Qdrant Cloud는 필터에 쓰는 필드에 인덱스가 필요하다
    text_vectors = VectorParams(size=config.EMBEDDING_DIM, distance=Distance.COSINE)
    for collection, vectors, indexed_fields in ((config.COLLECTION_NAME, text_vectors, ["page_id", "item"]),
                                                (config.BACKLOG_COLLECTION, text_vectors, ["status", "assignee_id", "page_ids", "chat_ts"]),
                                                # 질문 기록은 검색하지 않으므로 벡터 없이 저장한다
                                                (config.QA_LOG_COLLECTION, {}, ["answer_ts"]),
                                                (config.DIAGNOSIS_COLLECTION, {}, []),
                                                (config.PROPERTY_CHECK_COLLECTION, {}, []),
                                                (config.REFINED_COLLECTION, text_vectors, ["page_id", "item", "sources[].page_id"]),
                                                (config.LIBRARIAN_JOB_COLLECTION, {}, []),
                                                (config.LIBRARIAN_USAGE_COLLECTION, {}, [])):
        if not client.collection_exists(collection):
            client.create_collection(collection_name=collection, vectors_config=vectors)
            logger.info("✨ [Qdrant] '%s' 컬렉션이 새로 생성되었습니다.", collection)

        for field in indexed_fields:
            try:
                client.create_payload_index(collection_name=collection, field_name=field, field_schema=PayloadSchemaType.KEYWORD)
                logger.debug("🔑 [Qdrant] '%s.%s' 페이로드 인덱스 생성/확인 완료", collection, field)
            except Exception as e:
                # 이미 존재하는 경우 등
                logger.debug("[Qdrant] %s.%s 인덱스 생성 생략: %s", collection, field, e)
    return client


# --- 텍스트 임베딩 ---
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
