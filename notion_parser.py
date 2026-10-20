import logging
import requests

logger = logging.getLogger(__name__)

NOTION_VERSION = "2022-06-28"

# rich_text 본문을 가진 블록 종류
TEXT_BLOCK_TYPES = ("paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item", "numbered_list_item", "to_do", "toggle", "quote", "callout")
# 하위 블록을 따로 처리(DB 쿼리/표 파싱)하거나 별도 문서라서 재귀 탐색하지 않는 블록 종류
NO_RECURSE_TYPES = ("child_database", "child_page", "table")

# 데이터 단위 섹션 표시. 섹션 안의 각 줄(표 행 / DB 항목)은 한 줄 = 한 데이터 단위이며, 섹션은 빈 줄에서 끝난다.
TABLE_MARKER_PREFIX = "### 📊 [상세 기획 표"
DB_MARKER_PREFIX = "### 📊 [데이터베이스:"

def table_marker(location=""):
    return f"{TABLE_MARKER_PREFIX}: {location}] ###" if location else f"{TABLE_MARKER_PREFIX}] ###"

def single_line(text):
    """여러 줄 텍스트를 ' / '로 이어 한 줄로 만든다 (데이터 단위가 줄바꿈으로 쪼개지지 않도록)"""
    return " / ".join(line.strip() for line in text.splitlines() if line.strip())

def get_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json"
    }

def fetch_all_pages(method, url, token, body=None):
    """노션 목록 API는 최대 100개씩만 반환하므로 has_more가 끝날 때까지 next_cursor를 따라가며 results를 모은다.
    첫 페이지 요청이 실패하면 None (호출부에서 폴백 판단용), 중간 페이지 실패는 문서 일부만 저장되는 것을 막기 위해 예외를 던진다."""
    results = []
    cursor = None

    while True:
        if method == "GET":
            params = {"page_size": 100}
            if cursor: params["start_cursor"] = cursor
            res = requests.get(url, headers=get_headers(token), params=params, timeout=15)
        else:
            payload = {**(body or {}), "page_size": 100}
            if cursor: payload["start_cursor"] = cursor
            res = requests.post(url, headers=get_headers(token), json=payload, timeout=15)

        if res.status_code != 200:
            if cursor is None:
                return None
            raise RuntimeError(f"노션 API 페이지네이션 중 실패 ({res.status_code}): {url}")

        data = res.json()
        results.extend(data.get("results", []))
        cursor = data.get("next_cursor")
        if not data.get("has_more") or not cursor:
            return results

def plain_text(rich_texts):
    return "".join(t.get("plain_text", "") for t in rich_texts).strip()

def fetch_block_children(block_id, token):
    return fetch_all_pages("GET", f"https://api.notion.com/v1/blocks/{block_id}/children", token)

def parse_property_value(prop):
    if not prop: return ""
    p_type = prop.get("type")
    
    if p_type in ("title", "rich_text"):
        return plain_text(prop.get(p_type, []))
    elif p_type == "select":
        sel = prop.get("select")
        return sel.get("name", "") if sel else ""
    elif p_type == "multi_select":
        sels = prop.get("multi_select", [])
        return ", ".join([s.get("name", "") for s in sels])
    elif p_type == "number":
        return str(prop.get("number", ""))
    elif p_type == "url":
        return prop.get("url", "")
    elif p_type == "checkbox":
        return "True" if prop.get("checkbox") else "False"
    return ""

def page_title(page_data):
    return next((plain_text(v.get("title", [])) for v in page_data.get("properties", {}).values() if v.get("type") == "title"), "")

def relation_titles(prop, token, cache):
    """관계형 속성이 가리키는 페이지들의 제목 (예: 아이템의 각인='협공'). 조회할 수 없거나 휴지통인 페이지는 뺀다.
    같은 DB의 여러 행이 같은 페이지를 가리키는 경우가 많아 cache(page_id → 제목)로 한 번만 조회한다."""
    titles = []
    for ref in prop.get("relation", []):
        ref_id = ref.get("id")
        if ref_id not in cache:
            data = fetch_page(ref_id, token)
            cache[ref_id] = page_title(data) if data and not is_trashed(data) else ""
        if cache[ref_id]:
            titles.append(cache[ref_id])
    return ", ".join(titles)

def query_notion_database(db_id, token):
    return fetch_all_pages("POST", f"https://api.notion.com/v1/databases/{db_id}/query", token)

def format_table_row(cell_texts, header_cells):
    """표 행 하나를 한 줄로 만든다. 헤더가 있으면 '헤더: 값' 형태로 붙여 행 단독 청크로도 의미가 통하게 한다."""
    if not header_cells:
        return " | ".join(cell_texts)
    labeled = []
    for i, value in enumerate(cell_texts):
        if not value: continue
        label = header_cells[i] if i < len(header_cells) else ""
        labeled.append(f"{label}: {value}" if label else value)
    return " | ".join(labeled)

def fetch_page_text_blocks(block_id, token, column_header=False):
    """block_id 직속 자식 블록의 텍스트. 표 블록이면 column_header에 따라 첫 행을 헤더로 쓴다."""
    blocks = fetch_block_children(block_id, token)
    if blocks is None:
        return ""

    texts = []
    header_cells = None
    
    for b in blocks:
        b_type = b.get("type")
        if not b_type: continue
        
        if b_type in TEXT_BLOCK_TYPES:
            text_content = plain_text(b.get(b_type, {}).get("rich_text", []))
            if text_content:
                prefix = "• " if "list" in b_type else ""
                texts.append(f"{prefix}{text_content}")
                
        elif b_type == "table_row":
            cells = b.get("table_row", {}).get("cells", [])
            # 셀 안의 줄바꿈이 남으면 행이 여러 줄로 쪼개져 청킹 시 서로 다른 청크로 흩어진다
            cell_texts = [" ".join(plain_text(cell).split()) for cell in cells]
            if not any(cell_texts):
                continue
            if column_header and header_cells is None:
                header_cells = cell_texts
                continue
            texts.append(format_table_row(cell_texts, header_cells))
                
    # 헤더 행만 있는 표는 헤더라도 남긴다
    if header_cells and not texts:
        texts.append(" | ".join(header_cells))
    return "\n".join(texts)

def fetch_database_rows(db_id, db_title, token, images, outline=None, location=""):
    """뷰/DB 타겟 파싱: 엉뚱한 전체 DB 매칭을 방지하고 실제 연결된 DB 쿼리.
    각 행 페이지의 이미지는 항목 이름(item)과 함께 images에 모아, 그 항목 청크에만 연결되게 한다.
    outline이 있으면 읽을 수 없는 DB(뷰)와 공유되지 않은 관계형 참조를 문서 진단용으로 기록한다."""
    db_contents = []
    
    # 1. Direct DB 쿼리
    results = query_notion_database(db_id, token)
    if results is None and outline is not None:
        outline.append(("linked_view", " > ".join(filter(None, [location, db_title or "Untitled"]))))

    if results:
        display_title = db_title if db_title else "연관 데이터베이스"
        logger.debug("📊 [DB 파싱] %s (%d행)", display_title, len(results))
        db_contents.append(f"\n\n{DB_MARKER_PREFIX} {display_title}] ###")
        
        relation_cache = {}
        for row in results:
            row_id = row.get("id")
            row_props = row.get("properties", {})
            row_title = ""
            row_details = []
            
            for k, v in row_props.items():
                if v.get("type") == "relation":
                    parsed_val = relation_titles(v, token, relation_cache)
                else:
                    parsed_val = parse_property_value(v)
                if parsed_val:
                    if v.get("type") == "title":
                        row_title = parsed_val
                    else:
                        row_details.append(f"{k}='{parsed_val}'")
            
            if not row_title and row_details:
                row_title = row_details[0].split("=")[-1].strip("'")
            elif not row_title:
                row_title = "항목"
                
            detail_str = ", ".join(row_details) if row_details else ""
            
            # 각 행의 하위 페이지 본문 딥파싱
            if row_id:
                inner_text = fetch_page_text_blocks(row_id, token)
                if inner_text:
                    detail_str = f"{detail_str} | 내용: {inner_text}".strip(" |")
                for img in fetch_page_images(row_id, token):
                    images.append({"block_id": img["block_id"], "caption": img["caption"],
                                   "section": img["section"], "item": row_title})
            
            if not detail_str:
                detail_str = "세부 정보 없음"
                
            # 속성값/하위 페이지 본문의 줄바꿈을 없애 DB 항목 하나가 한 줄(= 청크 하나)이 되게 한다
            parsed_line = single_line(f"- 항목: [{row_title}] ➔ {detail_str}")
            logger.debug("   %s", parsed_line[:120] + "..." if len(parsed_line) > 120 else parsed_line)
            db_contents.append(parsed_line)

        unresolved = sum(1 for title in relation_cache.values() if not title)
        if unresolved and outline is not None:
            outline.append(("unshared_relation", display_title, unresolved))
            
        return "\n".join(db_contents)

    # Fallback: 일반 블록 파싱
    fallback_text = fetch_page_text_blocks(db_id, token)
    if fallback_text:
        return f"\n\n### 🔗 [연결된 문서/뷰: {db_title or '상세 데이터'}] ###\n" + fallback_text
        
    return "\n".join(db_contents)

# 다른 문서를 가리키는 링크 표시. 답변에서는 필요할 때만 '참고 자료'로 가볍게 언급한다
LINK_REFERENCE_PREFIX = "🔗 참고 링크:"

def link_reference(link, token):
    """link_to_page 블록이 가리키는 페이지/DB의 제목과 주소. 연동에 공유되지 않아 조회할 수 없으면 None"""
    kind = link.get("type")
    if kind not in ("page_id", "database_id"):
        return None
    endpoint = "pages" if kind == "page_id" else "databases"
    res = requests.get(f"https://api.notion.com/v1/{endpoint}/{link[kind]}", headers=get_headers(token), timeout=15)
    if res.status_code != 200:
        return None
    data = res.json()
    if is_trashed(data):
        return None
    if kind == "page_id":
        title = page_title(data)
    else:
        title = plain_text(data.get("title", []))
    return f"{LINK_REFERENCE_PREFIX} [{title or '제목 없음'}]({data.get('url', '')})"

def image_info(b):
    """이미지 블록에서 block_id, 캡션, (만료되는) 현재 URL 추출"""
    img_obj = b.get("image", {})
    return {
        # block_id: 만료되는 URL과 달리 고정값이라 캐싱과 슬랙 스레드 내 중복 첨부 판별에 사용
        "block_id": b.get("id"),
        "url": img_obj.get("file", {}).get("url") or img_obj.get("external", {}).get("url"),
        # 노션 캡션 (없으면 빈 문자열. 표시용 캡션은 동기화 때 이미지가 놓인 제목 경로로 채운다)
        "caption": plain_text(img_obj.get("caption", [])),
    }

HEADING_LEVELS = {"heading_1": 0, "heading_2": 1, "heading_3": 2}
# 표 바로 앞의 짧은 문단과 표를 감싼 짧은 부모 블록(토글 등)은 표 제목(예: 'attack', 'Intro')으로 보고 표 위치 정보에 포함한다.
# 긴 문장은 제목이 아니라 본문이므로 제외
TABLE_CAPTION_MAX_CHARS = 40

def table_location(headings, parent_path, caption):
    """표가 문서 어디에 있는지: 제목 경로 > 부모 블록 > 바로 앞 짧은 문단 (예: 'Ripper > VFX 적용 가이드 > attack')"""
    parts = []
    for part in [h for h in headings if h] + parent_path + [caption]:
        if part and (not parts or parts[-1] != part):
            parts.append(part)
    return " > ".join(parts)

def extract_blocks_recursive(block_id, token, full_text_list, images, depth=0, headings=None, parent_path=(), outline=None):
    """중첩된 블록 재귀적 파싱. 이미지 블록은 images에 block_id/캡션만 모은다 (URL은 만료되므로 저장하지 않음).
    headings: 문서 순서대로 갱신되는 현재 [제목1, 제목2, 제목3] (재귀 전체가 공유), parent_path: 감싸고 있는 토글 등 부모 블록 텍스트
    outline: 문서 진단용으로 블록 순서대로 ("heading", 단계, 제목) / ("content",) / 읽지 못한 것들을 기록한다 (재귀 전체가 공유)"""
    if depth > 5: return
    if headings is None:
        headings = [None, None, None]
    if outline is None:
        outline = []

    blocks = fetch_block_children(block_id, token)
    if blocks is None:
        return

    prev_sibling_text = None
    for b in blocks:
        b_type = b.get("type")
        has_children = b.get("has_children", False)
        if not b_type: continue
        text_content = ""

        # 1. 텍스트 계열 블록
        if b_type in TEXT_BLOCK_TYPES:
            text_content = plain_text(b.get(b_type, {}).get("rich_text", []))
            if text_content:
                full_text_list.append(text_content)
                if b_type in HEADING_LEVELS:
                    level = HEADING_LEVELS[b_type]
                    headings[level:] = [text_content] + [None] * (2 - level)
                    prev_sibling_text = None
                    outline.append(("heading", level, text_content))
                else:
                    prev_sibling_text = text_content
                    outline.append(("content",))
            # 빈 문단(간격용)은 표 제목 판단에 영향을 주지 않는다

        # 2. 문서 안의 DB (원본 DB면 행을 항목으로 펼친다. 뷰는 API가 원본을 알려주지 않아 읽을 수 없다)
        elif b_type == "child_database":
            db_text = fetch_database_rows(b.get("id"), b.get("child_database", {}).get("title", ""), token, images,
                                          outline, table_location(headings, list(parent_path), None))
            if db_text:
                full_text_list.append(db_text)
                outline.append(("content",))

        # 2-1. 다른 페이지/DB를 가리키는 링크: 내용은 가져오지 않고 참고 링크로만 남긴다
        elif b_type == "link_to_page":
            reference = link_reference(b.get("link_to_page", {}), token)
            if reference:
                full_text_list.append(reference)
                outline.append(("content",))
            else:
                outline.append(("unshared_link",))

        # 3. 테이블(표) 블록 파싱
        elif b_type == "table":
            column_header = b.get("table", {}).get("has_column_header", False)
            table_text = fetch_page_text_blocks(b.get("id"), token, column_header)
            if table_text:
                caption = prev_sibling_text if prev_sibling_text and len(prev_sibling_text) <= TABLE_CAPTION_MAX_CHARS else None
                location = table_location(headings, list(parent_path), caption)
                full_text_list.append(f"\n{table_marker(location)}\n" + table_text)
                outline.append(("content",))

        # 4. 이미지 블록
        elif b_type == "image":
            img = image_info(b)
            if img["url"]:
                images.append({"block_id": img["block_id"], "caption": img["caption"],
                               "section": table_location(headings, list(parent_path), None)})
                outline.append(("content",))

        if b_type not in TEXT_BLOCK_TYPES:
            prev_sibling_text = None

        # 5. 기타 자식 블록 탐색 (제목이 아닌 부모 블록의 텍스트는 하위 표의 위치 정보가 된다)
        if has_children and b_type not in NO_RECURSE_TYPES:
            is_label = text_content and b_type not in HEADING_LEVELS and len(text_content) <= TABLE_CAPTION_MAX_CHARS
            child_path = parent_path + (text_content,) if is_label else parent_path
            extract_blocks_recursive(b.get("id"), token, full_text_list, images, depth + 1, headings, child_path, outline)

def fetch_page_images(block_id, token, depth=0, headings=None):
    """페이지 내 이미지 블록 전체 탐색 (DB 행 페이지의 이미지 수집용). 이미지마다 놓인 위치의 제목 경로(section)를 붙인다."""
    if depth > 5: return []
    if headings is None:
        headings = [None, None, None]

    blocks = fetch_block_children(block_id, token)
    if blocks is None:
        return []

    images = []
    for b in blocks:
        b_type = b.get("type")
        if not b_type: continue

        if b_type in HEADING_LEVELS:
            text_content = plain_text(b.get(b_type, {}).get("rich_text", []))
            if text_content:
                level = HEADING_LEVELS[b_type]
                headings[level:] = [text_content] + [None] * (2 - level)

        if b_type == "image":
            img = image_info(b)
            if img["url"]:
                images.append({**img, "section": table_location(headings, [], None)})

        if b.get("has_children", False) and b_type not in NO_RECURSE_TYPES:
            images.extend(fetch_page_images(b.get("id"), token, depth + 1, headings))

    return images

def fetch_image_by_block_id(block_id, token):
    """캐싱해 둔 이미지 block_id로 블록 하나만 조회해 새 URL을 받는다. 삭제됐거나 이미지가 아니면 None."""
    res = requests.get(f"https://api.notion.com/v1/blocks/{block_id}", headers=get_headers(token), timeout=15)
    if res.status_code != 200:
        return None
    b = res.json()
    if b.get("type") != "image" or b.get("archived") or b.get("in_trash"):
        return None
    img = image_info(b)
    return img if img["url"] else None

def is_trashed(page_data):
    return bool(page_data.get("in_trash") or page_data.get("archived"))

def fetch_page(page_id, token):
    """페이지 객체 조회. 실패하면 None"""
    res = requests.get(f"https://api.notion.com/v1/pages/{page_id}", headers=get_headers(token), timeout=15)
    return res.json() if res.status_code == 200 else None

def is_page_trashed(page_id, token):
    """저장 직전 재확인용. 확인 자체가 실패하면 저장하지 않도록 예외를 던진다."""
    page_data = fetch_page(page_id, token)
    if page_data is None:
        raise RuntimeError(f"노션 페이지 상태 확인 실패 (page_id: {page_id})")
    return is_trashed(page_data)

# 경로를 찾을 때 거슬러 올라가는 최대 단계 (워크스페이스 최상위에 닿으면 그 전에 멈춘다)
PATH_MAX_DEPTH = 10

def fetch_object(kind, obj_id, token):
    """상위 객체(page/database/block) 조회. 연동에 공유되지 않았거나 실패하면 None"""
    endpoint = {"page_id": "pages", "database_id": "databases", "block_id": "blocks"}[kind]
    res = requests.get(f"https://api.notion.com/v1/{endpoint}/{obj_id}", headers=get_headers(token), timeout=15)
    return res.json() if res.status_code == 200 else None

def page_path(page_data, token):
    """페이지가 노션 어디에 있는지: 상위 페이지/DB 제목 목록 (먼 쪽부터). 예: ['영웅 목록', '디고리'].
    '전투 요소'처럼 여러 곳에 같은 제목이 있는 페이지를 구분하는 데 쓴다. 공유되지 않은 상위를 만나면 거기서 멈춘다.
    DB 행의 DB 이름이 그 DB를 담은 페이지 제목과 같으면 한 번만 넣는다."""
    titles = []
    parent = page_data.get("parent", {})
    for _ in range(PATH_MAX_DEPTH):
        kind = parent.get("type")
        if kind not in ("page_id", "database_id", "block_id"):
            break
        data = fetch_object(kind, parent[kind], token)
        if data is None:
            break
        title = page_title(data) if kind == "page_id" else plain_text(data.get("title", [])) if kind == "database_id" else ""
        if title and (not titles or titles[0] != title):
            titles.insert(0, title)
        parent = data.get("parent", {})
    return titles

def parse_notion_page_deep(page_id, token):
    """페이지 전체 파싱. 조회 실패 시 None, 휴지통 문서는 본문을 읽지 않고 {"trashed": True}를 반환한다."""
    page_data = fetch_page(page_id, token)
    if page_data is None:
        return None
    if is_trashed(page_data):
        return {"page_id": page_id, "trashed": True}

    props = page_data.get("properties", {})
    
    title = "제목 없음"
    for v in props.values():
        if v.get("type") == "title":
            title = parse_property_value(v)
            break
            
    doc_url = page_data.get("url", "")
    full_text_list = [f"# {title}"]

    images = []
    outline = []
    extract_blocks_recursive(page_id, token, full_text_list, images, outline=outline)

    return {
        "page_id": page_id,
        "title": title,
        "path": page_path(page_data, token),
        # 문서 진단용 구조 기록 (저장하지 않는다)
        "outline": outline,
        "full_text": "\n\n".join(full_text_list),
        "url": doc_url,
        "images": images
    }