import pytest
import import_notion
import notion_parser


@pytest.mark.parametrize("link,expected", [
    ("https://www.notion.so/team/1-337107d6982c803bbe17ce907056d08e", "337107d6-982c-803b-be17-ce907056d08e"),
    # DB 뷰 링크의 ?v= 는 DB 자신이 아니다
    ("https://www.notion.so/2f1107d6982c80a1b2c3d4e5f6a7b8c9?v=11111111111111111111111111111111",
     "2f1107d6-982c-80a1-b2c3-d4e5f6a7b8c9"),
    # 사이드 피크로 연 페이지는 p= 가 실제 페이지
    ("https://www.notion.so/team/2f1107d6982c80a1b2c3d4e5f6a7b8c9?v=1111&p=337107d6982c803bbe17ce907056d08e&pm=s",
     "337107d6-982c-803b-be17-ce907056d08e"),
    ("337107d6-982c-803b-be17-ce907056d08e", "337107d6-982c-803b-be17-ce907056d08e"),
])
def test_notion_id(link, expected):
    assert import_notion.notion_id(link) == expected


def test_notion_id_rejects_links_without_id():
    with pytest.raises(ValueError):
        import_notion.notion_id("https://www.notion.so/team")


def block(b_type, block_id, has_children=False, **extra):
    return {"id": block_id, "type": b_type, "has_children": has_children, **extra}


@pytest.fixture
def workspace(monkeypatch, no_network):
    """페이지/DB/블록으로 이루어진 가짜 노션"""
    ws = {"pages": {}, "children": {}, "dbs": {}, "rows": {}}

    def fetch_page(page_id, token):
        return ws["pages"].get(page_id)

    monkeypatch.setattr(notion_parser, "fetch_page", fetch_page)
    monkeypatch.setattr(notion_parser, "fetch_block_children", lambda block_id, token: ws["children"].get(block_id, []))
    monkeypatch.setattr(notion_parser, "query_notion_database", lambda db_id, token: ws["rows"].get(db_id))
    monkeypatch.setattr(import_notion, "fetch_database", lambda db_id, token: ws["dbs"].get(db_id))
    return ws


def page(title, trashed=False):
    return {"in_trash": trashed, "properties": {"n": {"type": "title", "title": [{"plain_text": title}]}}}


def test_page_discovers_child_pages_but_not_links_or_db_rows(workspace):
    ws = workspace
    ws["pages"].update({"root": page("기획"), "child": page("하위"), "grand": page("손자"), "row1": page("석화체"),
                        "rowchild": page("석화체 상세"), "linked": page("링크 대상"), "trash": page("지운 문서", trashed=True)})
    ws["children"].update({
        # 토글 안의 하위 페이지도 찾는다
        "root": [block("toggle", "t1", has_children=True), block("child_database", "db1", child_database={"title": "몹"}),
                 block("link_to_page", "l1", link_to_page={"type": "page_id", "page_id": "linked"}),
                 {**block("heading_2", "h"), "heading_2": {"rich_text": [{"plain_text": "개인 물품"}]}},
                 block("child_database", "view1", child_database={"title": "Untitled"}), block("child_page", "trash")],
        "t1": [block("child_page", "child")],
        "child": [block("child_page", "grand")],
        "row1": [block("child_page", "rowchild")],
    })
    ws["rows"]["db1"] = [{"id": "row1"}]  # view1은 뷰라서 쿼리 실패 (None)

    found = import_notion.Discovery("tok")
    found.run("root")
    # DB 행(석화체)은 '기획' 문서에 들어가므로 따로 등록하지 않고, 행 안의 하위 페이지만 등록한다
    assert set(found.pages) == {"root", "child", "grand", "rowchild"}
    # 뷰는 바로 위 제목으로 위치를 알려준다
    assert found.unreadable_views == [("기획", "개인 물품 > Untitled")]


def test_database_link_registers_each_row(workspace):
    ws = workspace
    ws["dbs"]["db"] = {"title": [{"plain_text": "챕터 2 몹 목록"}]}
    ws["rows"]["db"] = [{"id": "r1"}, {"id": "r2"}]
    ws["pages"].update({"r1": page("뿌리 마법사"), "r2": page("이끼거인")})
    found = import_notion.Discovery("tok")
    found.run("db")
    assert found.pages == {"r1": "뿌리 마법사", "r2": "이끼거인"}


def test_unknown_link_is_reported(workspace):
    found = import_notion.Discovery("tok")
    found.run("nowhere")
    assert found.pages == {} and found.missing == ["nowhere"]
