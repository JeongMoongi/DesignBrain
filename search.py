"""질문으로 기획서 청크를 찾는다: 후속 질문 재작성 → 이름 일치(문서 제목/DB 항목명) → 벡터 유사도 검색"""
import logging
import re
import threading
from itertools import zip_longest
from typing import List, Dict
from qdrant_client.models import Filter, FieldCondition, MatchValue
import clients
import config

logger = logging.getLogger(__name__)

# --- [후속 질문 재작성] ---
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
        completion = clients.get_openai().chat.completions.create(
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


# --- [이름 일치 검색: 문서 제목/DB 항목명이 질문에 그대로 있으면 해당 청크를 먼저 쓴다] ---
# 임베딩은 '석화체' 같은 고유 이름을 잘 잡지 못하고, 긴 항목 청크에서는 이름 신호가 묻혀 검색에서 밀린다
# '패턴 1'처럼 여러 곳에 있는 이름은 어느 것인지 알 수 없으므로 이름 일치로 쓰지 않는다
MAX_TARGETS_PER_ALIAS = 3
NAME_MATCH_TOTAL = 12
SEARCH_LIMIT = 10

def normalize_name(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()

def name_aliases(name: str) -> set:
    """전체 이름과 함께, 괄호/따옴표로 구분된 부분도 별칭으로 쓴다.
    (예: '덩굴 추적자 (VineStalker)' → 덩굴 추적자, VineStalker / '챕터 1 미니보스 ‘변종 분쇄자’' → 변종 분쇄자)
    ' - ' 앞부분은 '패턴 1 - SporeLaunch'의 '패턴 1'처럼 분류명인 경우가 많아 뒷부분만 쓴다."""
    last = re.split(r"\s+-\s+", name)[-1]
    parts = [name, last] + re.split(r"[()（）‘’“”'\"]", last)
    return {alias for alias in map(normalize_name, parts) if len(alias) >= 2}

# (만들 때 쓴 Qdrant 클라이언트, 별칭 목록, 문서 목록). 연결이 바뀌면 (테스트의 임시 DB 등) 다른 DB이므로 다시 만든다
_name_catalog = None
_name_catalog_lock = threading.Lock()

def invalidate_name_catalog():
    global _name_catalog
    with _name_catalog_lock:
        _name_catalog = None

def _get_catalogs() -> tuple:
    """(별칭 → {(page_id, item)}, 제목 → {"page_ids", "items"}). item이 빈 문자열이면 문서 전체.
    제목이 같은 서로 다른 페이지('아이템' DB 문서와 아이콘 경로만 있는 '아이템' 페이지 등)는 한 문서로 묶는다.
    동기화/삭제 후 다음 질문에서 다시 만든다."""
    global _name_catalog
    with _name_catalog_lock:
        qdrant = clients.get_qdrant()
        if _name_catalog is not None and _name_catalog[0] is qdrant:
            return _name_catalog[1], _name_catalog[2]
        targets = set()
        documents = {}
        offset = None
        while True:
            points, offset = qdrant.scroll(collection_name=config.COLLECTION_NAME, limit=256, offset=offset,
                                                 with_payload=["page_id", "title", "item", "chunk_idx"], with_vectors=False)
            for p in points:
                page_id = p.payload.get("page_id")
                if not page_id:
                    continue
                title = p.payload.get("title", "")
                targets.add((page_id, "", title))
                doc = documents.setdefault(title, {"page_ids": [], "items": {}})
                if page_id not in doc["page_ids"]:
                    doc["page_ids"].append(page_id)
                if p.payload.get("item"):
                    targets.add((page_id, p.payload["item"], p.payload["item"]))
                    doc["items"].setdefault(p.payload["item"], p.payload.get("chunk_idx", 0))
            if offset is None:
                break
        catalog = {}
        for page_id, item, name in targets:
            for alias in name_aliases(name):
                catalog.setdefault(alias, set()).add((page_id, item))
        catalog = {alias: t for alias, t in catalog.items() if len(t) <= MAX_TARGETS_PER_ALIAS}
        # 항목은 문서 안의 순서대로
        for doc in documents.values():
            doc["items"] = sorted(doc["items"], key=doc["items"].get)
        _name_catalog = (qdrant, catalog, documents)
        logger.info("📇 [이름 목록] 별칭 %d개, 문서 %d개 준비", len(catalog), len(documents))
        return catalog, documents

def _get_name_catalog() -> Dict[str, set]:
    return _get_catalogs()[0]

def get_documents() -> Dict[str, dict]:
    """인덱스된 문서 목록: 제목 → {"page_ids", "items"(DB 항목 이름, 문서 안 순서)}"""
    return _get_catalogs()[1]

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
        per_target.append(clients.get_qdrant().query_points(
            collection_name=config.COLLECTION_NAME, query=query_vector,
            query_filter=Filter(must=conditions), limit=NAME_MATCH_TOTAL
        ).points)
    # 이름이 여러 개 일치하면 대상별로 번갈아 채워 한 문서가 자리를 독차지하지 않게 한다
    seen = set()
    named = [p for rank in zip_longest(*per_target) for p in rank
             if p is not None and not (p.id in seen or seen.add(p.id))][:NAME_MATCH_TOTAL]

    similar = clients.get_qdrant().query_points(
        collection_name=config.COLLECTION_NAME,
        query=query_vector,
        limit=SEARCH_LIMIT,
        score_threshold=0.25
    ).points
    if named:
        logger.info("📇 [이름 일치] %d개 청크를 먼저 사용", len(named))
    combined = named + [p for p in similar if p.id not in seen]
    return combined[:max(SEARCH_LIMIT, len(named))], len(named)
