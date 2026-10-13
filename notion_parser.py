import os
import requests

NOTION_VERSION = "2022-06-28"

def get_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json"
    }

def parse_property_value(prop):
    if not prop: return ""
    p_type = prop.get("type")
    
    if p_type == "title":
        titles = prop.get("title", [])
        return "".join([t.get("plain_text", "") for t in titles]).strip()
    elif p_type == "rich_text":
        texts = prop.get("rich_text", [])
        return "".join([t.get("plain_text", "") for t in texts]).strip()
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
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    return requests.post(url, headers=get_headers(token), json={"page_size": 100}, timeout=15)

def fetch_all_accessible_databases(token):
    """토큰이 접근 가능한 모든 데이터베이스 리스트 pre-fetch"""
    url = "https://api.notion.com/v1/search"
    payload = {
        "filter": {"value": "database", "property": "object"},
        "page_size": 100
    }
    try:
        res = requests.post(url, headers=get_headers(token), json=payload, timeout=15)
        if res.status_code == 200:
            return res.json().get("results", [])
    except Exception as e:
        print(f"⚠️ 전체 DB 목록 조회 실패: {e}")
    return []

def fetch_page_text_blocks(block_id, token):
    url = f"https://api.notion.com/v1/blocks/{block_id}/children?page_size=100"
    res = requests.get(url, headers=get_headers(token), timeout=15)
    
    if res.status_code != 200:
        return ""
        
    blocks = res.json().get("results", [])
    texts = []
    
    for b in blocks:
        b_type = b.get("type")
        if not b_type: continue
        
        if b_type in ["paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item", "numbered_list_item", "to_do", "toggle", "quote", "callout"]:
            rich_texts = b.get(b_type, {}).get("rich_text", [])
            text_content = "".join([t.get("plain_text", "") for t in rich_texts]).strip()
            if text_content:
                prefix = "• " if "list" in b_type else ""
                texts.append(f"{prefix}{text_content}")
                
        elif b_type == "table_row":
            cells = b.get("table_row", {}).get("cells", [])
            cell_texts = []
            for cell in cells:
                c_text = "".join([t.get("plain_text", "") for t in cell]).strip()
                cell_texts.append(c_text)
            if any(cell_texts):
                texts.append(" | ".join(cell_texts))
                
    return "\n".join(texts)

def fetch_database_rows(db_id, db_title, token, accessible_dbs=None):
    """뷰/DB 타겟 파싱: 엉뚱한 전체 DB 매칭을 방지하고 실제 연결된 DB 쿼리"""
    db_contents = []
    
    # 1. Direct DB 쿼리
    db_res = query_notion_database(db_id, token)
    
    if db_res.status_code == 200:
        results = db_res.json().get("results", [])
        if results:
            display_title = db_title if db_title else "연관 데이터베이스"
            print(f"\n================ [Direct DB 파싱 성공: {display_title}] ================")
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
                print(parsed_line[:120] + "..." if len(parsed_line) > 120 else parsed_line)
                db_contents.append(parsed_line)
                
            print("===============================================================\n")
            return "\n".join(db_contents)

    # Fallback: 일반 블록 파싱
    fallback_text = fetch_page_text_blocks(db_id, token)
    if fallback_text:
        return f"\n\n### 🔗 [연결된 문서/뷰: {db_title or '상세 데이터'}] ###\n" + fallback_text
        
    return "\n".join(db_contents)

def extract_blocks_recursive(block_id, token, accessible_dbs, full_text_list, depth=0):
    """중첩된 블록 재귀적 파싱"""
    if depth > 5: return
    
    headers = get_headers(token)
    blocks_url = f"https://api.notion.com/v1/blocks/{block_id}/children?page_size=100"
    b_res = requests.get(blocks_url, headers=headers, timeout=15)
    
    if b_res.status_code != 200:
        return

    blocks = b_res.json().get("results", [])

    for b in blocks:
        b_type = b.get("type")
        has_children = b.get("has_children", False)
        if not b_type: continue

        # 1. 텍스트 계열 블록
        if b_type in ["paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item", "numbered_list_item", "to_do", "toggle", "quote", "callout"]:
            rich_texts = b.get(b_type, {}).get("rich_text", [])
            text_content = "".join([t.get("plain_text", "") for t in rich_texts]).strip()
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
                db_text = fetch_database_rows(target_id, db_title, token, accessible_dbs)
                if db_text:
                    full_text_list.append(db_text)

        # 3. 테이블(표) 블록 파싱
        elif b_type == "table":
            table_text = fetch_page_text_blocks(b.get("id"), token)
            if table_text:
                full_text_list.append(f"\n### 📊 [상세 기획 표] ###\n" + table_text)

        # 4. 기타 자식 블록 탐색
        if has_children and b_type not in ["child_database", "child_page", "table"]:
            extract_blocks_recursive(b.get("id"), token, accessible_dbs, full_text_list, depth + 1)

def fetch_page_images(block_id, token, depth=0):
    """페이지 내 이미지 블록 조회. 노션 파일 URL은 약 1시간 뒤 만료되므로 DB에 저장하지 않고 필요할 때마다 호출한다."""
    if depth > 5: return []

    blocks_url = f"https://api.notion.com/v1/blocks/{block_id}/children?page_size=100"
    b_res = requests.get(blocks_url, headers=get_headers(token), timeout=15)

    if b_res.status_code != 200:
        return []

    images = []
    for b in b_res.json().get("results", []):
        b_type = b.get("type")
        if not b_type: continue

        if b_type == "image":
            img_obj = b.get("image", {})
            img_url = img_obj.get("file", {}).get("url") or img_obj.get("external", {}).get("url")
            caption_list = img_obj.get("caption", [])
            caption = "".join([c.get("plain_text", "") for c in caption_list]).strip() or "도판 이미지"
            if img_url:
                # block_id: 만료되는 URL과 달리 고정값이라 슬랙 스레드 내 중복 첨부 판별에 사용
                images.append({"block_id": b.get("id"), "url": img_url, "caption": caption})

        if b.get("has_children", False) and b_type not in ["child_database", "child_page", "table"]:
            images.extend(fetch_page_images(b.get("id"), token, depth + 1))

    return images

def parse_notion_page_deep(page_id, token):
    headers = get_headers(token)
    accessible_dbs = fetch_all_accessible_databases(token)
    
    page_url = f"https://api.notion.com/v1/pages/{page_id}"
    res = requests.get(page_url, headers=headers, timeout=15)
    if res.status_code != 200:
        return None
        
    page_data = res.json()
    props = page_data.get("properties", {})
    
    title = "제목 없음"
    for k, v in props.items():
        if v.get("type") == "title":
            title = parse_property_value(v)
            break
            
    doc_url = page_data.get("url", "")
    full_text_list = [f"# {title}"]

    extract_blocks_recursive(page_id, token, accessible_dbs, full_text_list)

    return {
        "page_id": page_id,
        "title": title,
        "full_text": "\n\n".join(full_text_list),
        "url": doc_url
    }