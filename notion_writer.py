"""기획자가 승인한 수집가 제안을 노션에 쓴다.
- 결정: 원본 문서 맨 아래 '📌 결정 기록' 제목 밑에 날짜·승인자와 함께 덧붙인다 (본문은 건드리지 않는다)
- 새 문서: 자동 작성 기획 DB(config.PROPOSAL_DATABASE_ID)에 페이지로 만들고 '예상 담당자'를 지정한다
노션 연동에 '콘텐츠 삽입' 권한이 있어야 한다."""
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional
import requests
import backlog
import config
import notion_parser

logger = logging.getLogger(__name__)

DECISION_HEADING = "📌 결정 기록"
# 노션 rich_text 하나에 넣을 수 있는 최대 글자 수, 한 번에 덧붙일 수 있는 최대 블록 수
RICH_TEXT_CHARS = 2000
BLOCKS_PER_REQUEST = 100
API = "https://api.notion.com/v1"


def _request(method: str, path: str, body: Dict = None) -> Dict:
    res = requests.request(method, f"{API}/{path}", headers=notion_parser.get_headers(config.NOTION_INTEGRATION_TOKEN),
                           json=body, timeout=15)
    if res.status_code != 200:
        raise RuntimeError(f"노션 API 실패 ({res.status_code}) {method} {path}: {res.text[:300]}")
    return res.json()


def rich_text(text: str, bold: bool = False, color: str = "default") -> List[Dict]:
    return [{"type": "text", "text": {"content": text[i:i + RICH_TEXT_CHARS]},
             "annotations": {"bold": bold, "color": color}}
            for i in range(0, len(text), RICH_TEXT_CHARS)] or [{"type": "text", "text": {"content": ""}}]


def block(kind: str, text: str, **annotations) -> Dict:
    return {"object": "block", "type": kind, kind: {"rich_text": rich_text(text, **annotations)}}


def text_to_blocks(text: str) -> List[Dict]:
    """제안 글을 노션 블록으로: '## ' 제목2, '### ' 제목3, '- ' 글머리 목록, '1. ' 번호 목록, 나머지는 문단. 빈 줄은 버린다."""
    blocks = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("### "):
            blocks.append(block("heading_3", stripped[4:]))
        elif stripped.startswith("## "):
            blocks.append(block("heading_2", stripped[3:]))
        elif stripped.startswith(("- ", "* ", "• ")):
            blocks.append(block("bulleted_list_item", stripped[2:]))
        elif re.match(r"^\d+\.\s", stripped):
            blocks.append(block("numbered_list_item", stripped.split(" ", 1)[1]))
        else:
            blocks.append(block("paragraph", stripped))
    return blocks


def append_blocks(parent_id: str, blocks: List[Dict]) -> None:
    for start in range(0, len(blocks), BLOCKS_PER_REQUEST):
        _request("PATCH", f"blocks/{parent_id}/children", {"children": blocks[start:start + BLOCKS_PER_REQUEST]})


def has_decision_heading(page_id: str) -> bool:
    children = notion_parser.fetch_all_pages("GET", f"{API}/blocks/{page_id}/children", config.NOTION_INTEGRATION_TOKEN) or []
    return any(b.get("type") == "heading_2" and notion_parser.plain_text(b["heading_2"].get("rich_text", [])) == DECISION_HEADING
               for b in children)


def append_decision(page_id: str, title: str, text: str, approver_name: Optional[str]) -> None:
    """원본 문서 맨 아래 결정 기록에 승인된 결정을 덧붙인다. 결정 기록 제목이 없으면 먼저 만든다."""
    blocks = [] if has_decision_heading(page_id) else [block("heading_2", DECISION_HEADING)]
    stamp = f"{datetime.now(backlog.KST):%Y-%m-%d} · 승인: {approver_name or '기획자'}"
    blocks.append({"object": "block", "type": "paragraph", "paragraph": {
        "rich_text": rich_text(title, bold=True) + rich_text(f"  ({stamp})", color="gray")}})
    blocks += text_to_blocks(text)
    append_blocks(page_id, blocks)
    logger.info("📝 [노션 쓰기] 결정 기록 추가: %s (page_id: %s)", title, page_id)


def create_proposal_page(title: str, text: str, assignee_notion_id: Optional[str]) -> Dict:
    """자동 작성 기획 DB에 새 문서를 만든다. {"page_id", "url"}"""
    properties = {"이름": {"title": rich_text(title)}}
    if assignee_notion_id:
        properties["예상 담당자"] = {"people": [{"id": assignee_notion_id}]}
    blocks = text_to_blocks(text)
    page = _request("POST", "pages", {"parent": {"database_id": config.PROPOSAL_DATABASE_ID}, "properties": properties,
                                      "children": blocks[:BLOCKS_PER_REQUEST]})
    append_blocks(page["id"], blocks[BLOCKS_PER_REQUEST:])
    logger.info("📝 [노션 쓰기] 새 문서 생성: %s (%s)", title, page.get("url"))
    return {"page_id": page["id"], "url": page.get("url", "")}


# --- [노션 사용자 ↔ 이메일] ---
def user_email(notion_user_id: str) -> Optional[str]:
    """노션 사용자 이메일 (연동에 '이메일을 포함한 사용자 정보 읽기' 권한 필요). 실패하면 None"""
    try:
        return _request("GET", f"users/{notion_user_id}").get("person", {}).get("email")
    except Exception as e:
        logger.warning("⚠️ [노션] 사용자 이메일 조회 실패 (%s): %s", notion_user_id, e)
        return None


def user_id_by_email(email: Optional[str]) -> Optional[str]:
    if not email:
        return None
    try:
        users = notion_parser.fetch_all_pages("GET", f"{API}/users", config.NOTION_INTEGRATION_TOKEN) or []
    except Exception as e:
        logger.warning("⚠️ [노션] 사용자 목록 조회 실패: %s", e)
        return None
    return next((u["id"] for u in users if u.get("person", {}).get("email", "").lower() == email.lower()), None)
