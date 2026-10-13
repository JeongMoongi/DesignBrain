import logging
import requests

logger = logging.getLogger(__name__)

NOTION_VERSION = "2022-06-28"

# rich_text 본문을 가진 블록 종류
TEXT_BLOCK_TYPES = ("paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item", "numbered_list_item", "to_do", "toggle", "quote", "callout")
# 하위 블록을 따로 처리(DB 쿼리/표 파싱)하거나 별도 문서라서 재귀 탐색하지 않는 블록 종류
NO_RECURSE_TYPES = ("child_database", "child_page", "table")

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

def fetch_database_rows(db_id, db_title, token):
    """뷰/DB 타겟 파싱: 엉뚱한 전체 DB 매칭을 방지하고 실제 연결된 DB 쿼리"""
    db_contents = []
    
    # 1. Direct DB 쿼리
    results = query_notion_database(db_id, token)

    if results:
        display_title = db_title if db_title else "연관 데이터베이스"
        logger.debug("📊 [DB 파싱] %s (%d행)", display_title, len(results))
        db_contents.append(f"\n\n### 📊 [데이터베이스: {display_title}] ###")
        
        for row in results:
            row_id = row.get("id")
            row_props = row.get("properties", {})
            row_title = ""
            row_details = []
            
            for k, v in row_props.items():
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
            
            if not detail_str:
                detail_str = "세부 정보 없음"
                
            parsed_line = f"- 항목: [{row_title}] ➔ {detail_str}"
            logger.debug("   %s", parsed_line[:120] + "..." if len(parsed_line) > 120 else parsed_line)
            db_contents.append(parsed_line)
            
        return "\n".join(db_contents)

    # Fallback: 일반 블록 파싱
    fallback_text = fetch_page_text_blocks(db_id, token)
    if fallback_text:
        return f"\n\n### 🔗 [연결된 문서/뷰: {db_title or '상세 데이터'}] ###\n" + fallback_text
        
    return "\n".join(db_contents)

def image_info(b):
    """이미지 블록에서 block_id, 캡션, (만료되는) 현재 URL 추출"""
    img_obj = b.get("image", {})
    return {
        # block_id: 만료되는 URL과 달리 고정값이라 캐싱과 슬랙 스레드 내 중복 첨부 판별에 사용
        "block_id": b.get("id"),
        "url": img_obj.get("file", {}).get("url") or img_obj.get("external", {}).get("url"),
        "caption": plain_text(img_obj.get("caption", [])) or "도판 이미지",
    }

def extract_blocks_recursive(block_id, token, full_text_list, images, depth=0):
    """중첩된 블록 재귀적 파싱. 이미지 블록은 images에 block_id/캡션만 모은다 (URL은 만료되므로 저장하지 않음)."""
    if depth > 5: return

    blocks = fetch_block_children(block_id, token)
    if blocks is None:
        return

    for b in blocks:
        b_type = b.get("type")
        has_children = b.get("has_children", False)
        if not b_type: continue

        # 1. 텍스트 계열 블록
        if b_type in TEXT_BLOCK_TYPES:
            text_content = plain_text(b.get(b_type, {}).get("rich_text", []))
            if text_content:
                full_text_list.append(text_content)

        # 2. 데이터베이스 및 뷰 참조 블록 (모든 종류 강제 감지)
        elif b_type in ["child_database", "link_to_page", "linked_database", "database"]:
            target_id = b.get("id")
            db_title = b.get("child_database", {}).get("title", "") if b_type == "child_database" else ""

            if b_type in ["link_to_page", "linked_database"]:
                link_obj = b.get(b_type, {})
                link_type = link_obj.get("type")
                if link_type == "database_id":
                    target_id = link_obj.get("database_id")
                elif link_type == "page_id":
                    target_id = link_obj.get("page_id")

            if target_id:
                db_text = fetch_database_rows(target_id, db_title, token)
                if db_text:
                    full_text_list.append(db_text)

        # 3. 테이블(표) 블록 파싱
        elif b_type == "table":
            column_header = b.get("table", {}).get("has_column_header", False)
            table_text = fetch_page_text_blocks(b.get("id"), token, column_header)
            if table_text:
                full_text_list.append(f"\n### 📊 [상세 기획 표] ###\n" + table_text)

        # 4. 이미지 블록
        elif b_type == "image":
            img = image_info(b)
            if img["url"]:
                images.append({"block_id": img["block_id"], "caption": img["caption"]})

        # 5. 기타 자식 블록 탐색
        if has_children and b_type not in NO_RECURSE_TYPES:
            extract_blocks_recursive(b.get("id"), token, full_text_list, images, depth + 1)

def fetch_page_images(block_id, token, depth=0):
    """페이지 내 이미지 블록 전체 탐색. 이미지 목록이 캐싱되지 않은 (이전 버전으로 동기화된) 문서용 폴백."""
    if depth > 5: return []

    blocks = fetch_block_children(block_id, token)
    if blocks is None:
        return []

    images = []
    for b in blocks:
        b_type = b.get("type")
        if not b_type: continue

        if b_type == "image":
            img = image_info(b)
            if img["url"]:
                images.append(img)

        if b.get("has_children", False) and b_type not in NO_RECURSE_TYPES:
            images.extend(fetch_page_images(b.get("id"), token, depth + 1))

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

def parse_notion_page_deep(page_id, token):
    page_url = f"https://api.notion.com/v1/pages/{page_id}"
    res = requests.get(page_url, headers=get_headers(token), timeout=15)
    if res.status_code != 200:
        return None
        
    page_data = res.json()
    props = page_data.get("properties", {})
    
    title = "제목 없음"
    for v in props.values():
        if v.get("type") == "title":
            title = parse_property_value(v)
            break
            
    doc_url = page_data.get("url", "")
    full_text_list = [f"# {title}"]

    images = []
    extract_blocks_recursive(page_id, token, full_text_list, images)

    return {
        "page_id": page_id,
        "title": title,
        "full_text": "\n\n".join(full_text_list),
        "url": doc_url,
        "images": images
    }