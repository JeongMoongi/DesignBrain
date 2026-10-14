"""노션 페이지/DB 링크를 주면 그 아래의 페이지를 모두 찾아 인덱스에 등록한다.

    python import_notion.py <페이지 또는 DB 링크> [...]
    python import_notion.py --dry-run <링크> [...]   # 등록하지 않고 무엇이 등록될지만 출력

- 페이지: 그 페이지와 하위 페이지(child page)를 모두 등록한다. 페이지 안의 원본 DB 행은
  그 페이지 문서에 항목으로 들어가므로 따로 등록하지 않고, 행 안의 하위 페이지만 따라간다.
- DB 링크: 각 행을 문서 하나로 등록하고, 행 안의 하위 페이지도 따라간다.
- 페이지 링크(link_to_page)는 다른 곳을 가리키므로 따라가지 않는다 (문서에 참고 링크로만 남는다).
- DB 뷰(linked view)는 노션 API가 원본 DB를 알려주지 않아 읽을 수 없다. 원본 DB 링크를 따로 넣어야 한다.
"""
import logging
import re
import sys
from collections import deque
import config
import indexing
import notion_parser

logger = logging.getLogger("import_notion")

NOTION_ID = re.compile(r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}")
SIDE_PEEK_PARAM = re.compile(r"[?&]p=([0-9a-f]{32})")
# 블록 재귀 깊이 제한 (notion_parser와 같은 기준)
MAX_DEPTH = 5


def notion_id(link: str) -> str:
    """링크나 ID에서 노션 ID(하이픈 형식)를 뽑는다. DB 뷰 링크의 ?v=는 무시하고, 사이드 피크(?p=)는 그 페이지로 본다."""
    peek = SIDE_PEEK_PARAM.search(link)
    path = peek.group(1) if peek else link.split("?", 1)[0]
    found = NOTION_ID.findall(path.lower())
    if not found:
        raise ValueError(f"노션 ID를 찾을 수 없습니다: {link}")
    raw = found[-1].replace("-", "")
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def page_title(page) -> str:
    return next((notion_parser.plain_text(v.get("title", [])) for v in page.get("properties", {}).values()
                 if v.get("type") == "title"), "") or "제목 없음"


def database_title(db) -> str:
    return notion_parser.plain_text(db.get("title", [])) or "제목 없음"


def fetch_database(db_id, token):
    res = notion_parser.requests.get(f"https://api.notion.com/v1/databases/{db_id}",
                                     headers=notion_parser.get_headers(token), timeout=15)
    return res.json() if res.status_code == 200 else None


class Discovery:
    """등록할 페이지와 읽을 수 없는 뷰를 찾는다"""

    def __init__(self, token):
        self.token = token
        self.pages = {}            # 등록할 page_id → 제목 (찾은 순서 유지)
        self.unreadable_views = []  # (뷰가 있는 페이지 제목, 뷰 위치: 바로 위 제목 > 뷰 제목)
        self.missing = []          # 조회할 수 없는 링크 (연동에 공유되지 않았거나 잘못된 링크)
        self._seen = set()
        self._queue = deque()       # (page_id, 등록 여부)

    def run(self, root_id):
        page = notion_parser.fetch_page(root_id, self.token)
        if page is not None:
            self._queue.append((root_id, True))
        else:
            db = fetch_database(root_id, self.token)
            if db is None:
                self.missing.append(root_id)
                return
            rows = notion_parser.query_notion_database(root_id, self.token)
            if rows is None:
                self.unreadable_views.append(("(링크로 준 DB)", database_title(db)))
                return
            self._queue.extend((row["id"], True) for row in rows)

        while self._queue:
            page_id, index = self._queue.popleft()
            if page_id in self._seen:
                continue
            self._seen.add(page_id)
            page = notion_parser.fetch_page(page_id, self.token)
            if page is None or notion_parser.is_trashed(page):
                continue
            title = page_title(page)
            if index:
                self.pages[page_id] = title
            self._walk(page_id, title)

    def _walk(self, block_id, title, depth=0, heading=None):
        """heading: 지금까지 지나온 마지막 제목 (뷰는 대개 제목이 'Untitled'라 어느 뷰인지 구분하는 데 쓴다)"""
        if depth > MAX_DEPTH:
            return heading
        for b in notion_parser.fetch_block_children(block_id, self.token) or []:
            b_type = b.get("type")
            if b_type in ("heading_1", "heading_2", "heading_3"):
                heading = notion_parser.plain_text(b.get(b_type, {}).get("rich_text", [])) or heading
            if b_type == "child_page":
                self._queue.append((b["id"], True))
            elif b_type == "child_database":
                rows = notion_parser.query_notion_database(b["id"], self.token)
                if rows is None:
                    view = b.get("child_database", {}).get("title") or "Untitled"
                    self.unreadable_views.append((title, f"{heading} > {view}" if heading else view))
                else:
                    # 행은 이 페이지 문서에 항목으로 들어가므로 등록하지 않고, 행 안의 하위 페이지만 찾는다
                    self._queue.extend((row["id"], False) for row in rows)
            elif b.get("has_children") and b_type not in notion_parser.NO_RECURSE_TYPES:
                heading = self._walk(b["id"], title, depth + 1, heading)
        return heading


def main(args):
    dry_run = "--dry-run" in args
    links = [a for a in args if not a.startswith("--")]
    if not links:
        print(__doc__)
        return 1
    token = config.NOTION_INTEGRATION_TOKEN
    discovery = Discovery(token)
    for link in links:
        discovery.run(notion_id(link))

    logger.info("등록할 페이지 %d개를 찾았습니다.", len(discovery.pages))
    for page_id, title in discovery.pages.items():
        logger.info("  - %s (%s)", title, page_id)
    for where, view in discovery.unreadable_views:
        logger.warning("원본 링크가 필요한 뷰: '%s' 안의 '%s'", where, view)
    for page_id in discovery.missing:
        logger.warning("조회할 수 없는 링크 (연동에 공유되지 않았거나 잘못된 링크): %s", page_id)
    if dry_run:
        return 0

    failed = []
    for i, (page_id, title) in enumerate(discovery.pages.items(), 1):
        try:
            if not indexing.sync_document_to_db(page_id, token):
                failed.append((title, "저장 안 함 (휴지통/빈 문서)"))
        except Exception as e:
            logger.exception("등록 실패: %s", title)
            failed.append((title, e))
        logger.info("진행 %d/%d", i, len(discovery.pages))
    logger.info("등록 완료: %d개 중 %d개 저장", len(discovery.pages), len(discovery.pages) - len(failed))
    for title, reason in failed:
        logger.error("  저장하지 못함: %s (%s)", title, reason)
    return 1 if failed else 0


if __name__ == "__main__":
    import clients
    try:
        sys.exit(main(sys.argv[1:]))
    finally:
        clients.close_clients()
