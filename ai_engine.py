import logging
import re
import threading
import uuid
from itertools import zip_longest
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
    # 다음 연결은 다른 DB일 수 있으므로 (테스트의 임시 DB 등) 이름 목록도 다시 만든다
    invalidate_name_catalog()

def _connect_qdrant() -> QdrantClient:
    if config.QDRANT_URL and config.QDRANT_API_KEY:
        client = QdrantClient(url=config.QDRANT_URL, api_key=config.QDRANT_API_KEY, timeout=15.0)
        logger.info("🌐 [Qdrant] Qdrant Cloud 클러스터에 연결되었습니다.")
    else:
        client = QdrantClient(path=config.QDRANT_LOCAL_PATH)
        logger.info("💾 [Qdrant] 로컬 디스크 기반 DB를 사용합니다. (%s)", config.QDRANT_LOCAL_PATH)

    # --- [2. Qdrant 컬렉션 및 페이로드 인덱스 초기화] ---
    # Qdrant Cloud는 필터에 쓰는 필드에 인덱스가 필요하다
    for collection, indexed_fields in ((COLLECTION_NAME, ["page_id", "item"]),
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

IMAGE_CAPTION_PATH_DEPTH = 2
IMAGE_CAPTION_MAX_CHARS = 80

def image_caption(img: Dict, name: str) -> str:
    """슬랙에 보여줄 도판 설명: '문서/항목 이름 · 노션 캡션(없으면 이미지가 놓인 제목 경로)' (예: '석화체 · 패턴 > Powerbomb')
    깊은 제목 경로는 이미지에 가까운 마지막 두 단계만 쓰고, 너무 길면 자른다."""
    detail = img.get("caption") or " > ".join((img.get("section") or "").split(" > ")[-IMAGE_CAPTION_PATH_DEPTH:])
    caption = " ".join(f"{name} · {detail}".split()) if detail else name
    return caption if len(caption) <= IMAGE_CAPTION_MAX_CHARS else caption[:IMAGE_CAPTION_MAX_CHARS - 1] + "…"

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
        item = chunk_item_name(chunk)
        payload = {
            "page_id": page_id,
            "title": title,
            "text": chunk,
            "url": doc_data.get("url", ""),
            "chunk_idx": idx,
            # 이미지 block_id/캡션 캐시: 답변 시 이 블록들만 조회해 새 URL을 받는다.
            # DB 항목 청크에는 그 항목 페이지의 이미지만, 나머지 청크에는 문서 본문의 이미지를 단다
            "images": [{"block_id": img["block_id"], "caption": image_caption(img, item or title)}
                       for img in doc_data.get("images", []) if img.get("item", "") == item]
        }
        if item:
            payload["item"] = item
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
    invalidate_name_catalog()

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
    invalidate_name_catalog()
    logger.info("🗑️ [삭제 완료] page_id: %s 의 청크를 Qdrant에서 제거했습니다.", page_id)


# --- [5. 이름 일치 검색: 문서 제목/DB 항목명이 질문에 그대로 있으면 해당 청크를 먼저 쓴다] ---
# 임베딩은 '석화체' 같은 고유 이름을 잘 잡지 못하고, 긴 항목 청크에서는 이름 신호가 묻혀 검색에서 밀린다
ITEM_NAME_PATTERN = re.compile(r"^- 항목: \[(.+?)\] ➔", re.MULTILINE)
# '패턴 1'처럼 여러 곳에 있는 이름은 어느 것인지 알 수 없으므로 이름 일치로 쓰지 않는다
MAX_TARGETS_PER_ALIAS = 3
NAME_MATCH_TOTAL = 12
SEARCH_LIMIT = 10

def chunk_item_name(chunk: str) -> str:
    """DB 항목 청크면 항목 이름을 돌려준다 (긴 항목을 나눈 조각에도 항목명이 붙어 있다)"""
    match = ITEM_NAME_PATTERN.search(chunk)
    return match.group(1) if match else ""

def normalize_name(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()

def name_aliases(name: str) -> set:
    """전체 이름과 함께, 괄호/따옴표로 구분된 부분도 별칭으로 쓴다.
    (예: '덩굴 추적자 (VineStalker)' → 덩굴 추적자, VineStalker / '챕터 1 미니보스 ‘변종 분쇄자’' → 변종 분쇄자)
    ' - ' 앞부분은 '패턴 1 - SporeLaunch'의 '패턴 1'처럼 분류명인 경우가 많아 뒷부분만 쓴다."""
    last = re.split(r"\s+-\s+", name)[-1]
    parts = [name, last] + re.split(r"[()（）‘’“”'\"]", last)
    return {alias for alias in map(normalize_name, parts) if len(alias) >= 2}

_name_catalog = None
_name_catalog_lock = threading.Lock()

def invalidate_name_catalog():
    global _name_catalog
    with _name_catalog_lock:
        _name_catalog = None

def _get_name_catalog() -> Dict[str, set]:
    """별칭 → {(page_id, item)}. item이 빈 문자열이면 문서 전체. 동기화/삭제 후 다음 질문에서 다시 만든다."""
    global _name_catalog
    with _name_catalog_lock:
        if _name_catalog is not None:
            return _name_catalog
        targets = set()
        offset = None
        while True:
            points, offset = get_qdrant().scroll(collection_name=COLLECTION_NAME, limit=256, offset=offset,
                                                 with_payload=["page_id", "title", "item"], with_vectors=False)
            for p in points:
                page_id = p.payload.get("page_id")
                if not page_id:
                    continue
                targets.add((page_id, "", p.payload.get("title", "")))
                if p.payload.get("item"):
                    targets.add((page_id, p.payload["item"], p.payload["item"]))
            if offset is None:
                break
        catalog = {}
        for page_id, item, name in targets:
            for alias in name_aliases(name):
                catalog.setdefault(alias, set()).add((page_id, item))
        _name_catalog = {alias: t for alias, t in catalog.items() if len(t) <= MAX_TARGETS_PER_ALIAS}
        logger.info("📇 [이름 목록] 별칭 %d개 준비", len(_name_catalog))
        return _name_catalog

def match_names(query: str, catalog: Dict[str, set]) -> List[tuple]:
    """질문에 들어 있는 이름의 대상 목록. 더 긴 이름에 포함된 짧은 이름('변종 분쇄자' 안의 '분쇄자')은 뺀다."""
    normalized = normalize_name(query)
    spans = []
    for alias in catalog:
        start = normalized.find(alias)
        while start != -1:
            spans.append((start, start + len(alias), alias))
            start = normalized.find(alias, start + 1)
    kept = [s for s in spans
            if not any(o[0] <= s[0] and s[1] <= o[1] and o[1] - o[0] > s[1] - s[0] for o in spans)]
    targets = []
    for _, _, alias in sorted(kept):
        for target in sorted(catalog[alias]):
            if target not in targets:
                targets.append(target)
    return targets

def search_chunks(query: str, query_vector: List[float]) -> tuple:
    """이름이 일치한 문서/항목 안에서 먼저 찾고, 나머지는 벡터 유사도 검색으로 채운다.
    (검색 결과, 앞쪽의 이름 일치 결과 개수)를 돌려준다."""
    try:
        catalog = _get_name_catalog()
    except Exception as e:
        # 이름 일치는 보조 수단이므로 목록을 못 만들어도 벡터 검색으로 답변한다 (다음 질문에서 다시 시도)
        logger.warning("⚠️ [이름 목록] 준비 실패, 벡터 검색만 사용합니다: %s", e)
        catalog = {}
    per_target = []
    for page_id, item in match_names(query, catalog):
        conditions = [FieldCondition(key="page_id", match=MatchValue(value=page_id))]
        if item:
            conditions.append(FieldCondition(key="item", match=MatchValue(value=item)))
        per_target.append(get_qdrant().query_points(
            collection_name=COLLECTION_NAME, query=query_vector,
            query_filter=Filter(must=conditions), limit=NAME_MATCH_TOTAL
        ).points)
    # 이름이 여러 개 일치하면 대상별로 번갈아 채워 한 문서가 자리를 독차지하지 않게 한다
    seen = set()
    named = [p for rank in zip_longest(*per_target) for p in rank
             if p is not None and not (p.id in seen or seen.add(p.id))][:NAME_MATCH_TOTAL]

    similar = get_qdrant().query_points(
        collection_name=COLLECTION_NAME,
        query=query_vector,
        limit=SEARCH_LIMIT,
        score_threshold=0.25
    ).points
    if named:
        logger.info("📇 [이름 일치] %d개 청크를 먼저 사용", len(named))
    combined = named + [p for p in similar if p.id not in seen]
    return combined[:max(SEARCH_LIMIT, len(named))], len(named)


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
    """Qdrant Cloud 벡터 검색 후 OpenAI GPT 답변 생성. 도판은 검색된 images(block_id 목록)로 호출부에서 노션에서 직접 가져온다.
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
    cited = {normalize_name(title.strip(MARKDOWN_MARKS)) for title in match.group(1).split("|")}
    used = [hit for hit in search_results if normalize_name(hit.payload.get("title", "")) in cited]
    logger.info("📎 [사용한 문서] %s", match.group(1).strip(MARKDOWN_MARKS))
    return "\n".join(lines[:-1]).rstrip(), used

def _ask_ai(query: str, chat_history: List[Dict[str, str]], progress: Dict[str, str]) -> Dict:
    search_query = rewrite_search_query(query, chat_history)
    query_vector = get_embedding(search_query)

    # 1. 이름 일치 + 벡터 코사인 유사도 검색
    progress["stage"] = "기획서 검색"
    search_results, named_count = search_chunks(search_query, query_vector)

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
        f"답변의 맨 마지막 줄에는 '{USED_DOCS_LABEL} 제목1 | 제목2' 형식으로, 답변에 실제로 사용한 [참고 문서]의 제목만 적어라. "
        f"사용한 문서가 없으면 '{USED_DOCS_LABEL} 없음'이라고 적어라."
    )
    
    messages = [{"role": "system", "content": system_prompt}]
    
    for h in chat_history[-ANSWER_HISTORY_TURNS:]:
        messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
        
    messages.append({"role": "user", "content": f"[컨텍스트]\n{context_str}\n\n[질문]\n{query}"})

    # 3. GPT 답변 생성
    progress["stage"] = "답변 생성"
    completion = get_openai().chat.completions.create(
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