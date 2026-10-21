"""노션 문서를 청크로 나눠 Qdrant에 저장/삭제한다 (웹훅, resync_all.py에서 호출)"""
import logging
import re
import threading
import uuid
from typing import List, Dict
from qdrant_client.models import PointStruct, Filter, FieldCondition, MatchValue, HasIdCondition
import clients
import collector
import doc_diagnosis
import config
import notion_parser
import search

logger = logging.getLogger(__name__)

ITEM_NAME_PATTERN = re.compile(r"^- 항목: \[(.+?)\] ➔", re.MULTILINE)
def chunk_item_name(chunk: str) -> str:
    """DB 항목 청크면 항목 이름을 돌려준다 (긴 항목을 나눈 조각에도 항목명이 붙어 있다)"""
    match = ITEM_NAME_PATTERN.search(chunk)
    return match.group(1) if match else ""

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




# --- [청킹: 일반 텍스트는 줄 단위로 묶고, 표 행/DB 항목은 데이터 단위로 하나씩] ---
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

# 청크 머리줄에 넣는 경로 단계 수 (가까운 상위부터). '전투 요소'가 어느 영웅의 것인지 같은 소속이 검색에도 반영되게 한다
PATH_IN_HEADER = 3

def chunk_header(title: str, path: List[str] = ()) -> str:
    location = f" (위치: {' > '.join(path[-PATH_IN_HEADER:])})" if path else ""
    return f"문서: {title}{location}"

def split_into_chunks(title: str, full_text: str, path: List[str] = ()) -> List[str]:
    doc_header = chunk_header(title, path)
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


# --- [노션 문서 동기화] ---
# 같은 문서의 동기화/삭제는 하나씩만 실행한다. 동기화(수십 초) 도중 들어온 삭제가 먼저 끝나고
# 뒤늦게 동기화가 저장해 휴지통 문서가 되살아나는 것을 막기 위함
_page_locks = {}
_page_locks_guard = threading.Lock()

def _page_lock(page_id: str) -> threading.Lock:
    with _page_locks_guard:
        return _page_locks.setdefault(page_id, threading.Lock())

def sync_document_to_db(page_id: str, notion_token: str, notify: bool = False):
    """노션 문서를 파싱해 청크로 나눠 Qdrant에 저장. 휴지통 문서는 저장하지 않고 인덱스에서 제거한다.
    저장한 뒤 문서 진단을 남기고, notify면(웹훅 동기화) 진단할 것이 있을 때 담당자에게 알린다.
    수집가 작업 목록에도 올려 정리본을 새로 만들게 한다."""
    with _page_lock(page_id):
        return _sync_document(page_id, notion_token, notify)

def _sync_document(page_id: str, notion_token: str, notify: bool = False):
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

    path = doc_data.get("path", [])
    chunks = split_into_chunks(title, full_text, path)

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("🧩 [청크 %d개: %s]\n%s", len(chunks), title,
                     "\n".join(f"--- [청크 #{idx+1}] ---\n{chk}" for idx, chk in enumerate(chunks)))

    # 임베딩을 모두 성공한 뒤에만 DB를 건드린다 (중간 실패 시 기존 데이터 보존)
    vectors = clients.get_embeddings(chunks)

    points = []
    for idx, (chunk, vector) in enumerate(zip(chunks, vectors)):
        item = chunk_item_name(chunk)
        payload = {
            "page_id": page_id,
            "title": title,
            # 노션 안의 위치 (상위 페이지/DB 제목, 먼 쪽부터). 같은 제목의 다른 문서를 구분하는 데 쓴다
            "path": " > ".join(path),
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
    clients.get_qdrant().upsert(collection_name=config.COLLECTION_NAME, points=points)
    try:
        clients.get_qdrant().delete(
            collection_name=config.COLLECTION_NAME,
            points_selector=Filter(
                must=[FieldCondition(key="page_id", match=MatchValue(value=page_id))],
                must_not=[HasIdCondition(has_id=[p.id for p in points])]
            )
        )
    except Exception as e:
        logger.warning("⚠️ [Qdrant] 이전 청크 정리 실패 (새 청크는 저장됨, page_id: %s): %s", page_id, e)
    search.invalidate_name_catalog()

    logger.info("🎉 [동기화 완료] '%s' (%d개 청크 저장)", title, len(points))
    doc_diagnosis.run(doc_data, notify_owner=notify)
    try:
        collector.enqueue(page_id)
    except Exception as e:
        logger.warning("⚠️ [수집가] 작업 목록에 올리지 못했습니다 (원본은 저장됨, page_id: %s): %s", page_id, e)
    return True


def delete_document_from_db(page_id: str):
    """노션에서 삭제된 문서의 모든 청크를 인덱스에서 제거 (진행 중인 같은 문서 동기화가 끝난 뒤 실행)"""
    with _page_lock(page_id):
        _delete_page_points(page_id)

def _delete_page_points(page_id: str):
    clients.get_qdrant().delete(
        collection_name=config.COLLECTION_NAME,
        points_selector=Filter(must=[FieldCondition(key="page_id", match=MatchValue(value=page_id))])
    )
    search.invalidate_name_catalog()
    try:
        doc_diagnosis.delete(page_id)
    except Exception as e:
        logger.warning("⚠️ [문서 진단] 삭제된 문서의 진단 기록 정리 실패: %s", e)
    try:
        collector.delete_refined(page_id)
    except Exception as e:
        logger.warning("⚠️ [수집가] 삭제된 문서의 정리본 정리 실패: %s", e)
    logger.info("🗑️ [삭제 완료] page_id: %s 의 청크를 Qdrant에서 제거했습니다.", page_id)
