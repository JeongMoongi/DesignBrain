import pytest
import notion_parser as np


def rt(text):
    return [{"plain_text": text}]


def text_block(b_type, text, children=None, block_id=None):
    return {"id": block_id or f"{b_type}-{text}", "type": b_type, "has_children": bool(children), b_type: {"rich_text": rt(text)}}


def table_block(block_id, header=True):
    return {"id": block_id, "type": "table", "has_children": True, "table": {"has_column_header": header}}


def row(*cells):
    return {"id": "row", "type": "table_row", "has_children": False, "table_row": {"cells": [rt(c) for c in cells]}}


@pytest.fixture
def fake_notion(monkeypatch, no_network):
    tree = {}
    monkeypatch.setattr(np, "fetch_block_children", lambda block_id, token: tree.get(block_id, []))
    return tree


def test_table_rows_are_labeled_with_header(fake_notion):
    fake_notion["t"] = [row("태그 명", "설명"), row("Intro_1", "점프"), row("Down_In", "")]
    assert np.fetch_page_text_blocks("t", "tok", column_header=True) == "태그 명: Intro_1 | 설명: 점프\n태그 명: Down_In"


def test_table_without_header_keeps_plain_rows(fake_notion):
    fake_notion["t"] = [row("a", "b"), row("c", "d")]
    assert np.fetch_page_text_blocks("t", "tok", column_header=False) == "a | b\nc | d"


def test_multiline_cell_stays_on_one_line(fake_notion):
    fake_notion["t"] = [row("태그", "설명"), row("Walk", "첫 줄\n둘째 줄")]
    assert np.fetch_page_text_blocks("t", "tok", column_header=True) == "태그: Walk | 설명: 첫 줄 둘째 줄"


def test_header_only_table_is_kept(fake_notion):
    fake_notion["t"] = [row("a", "b")]
    assert np.fetch_page_text_blocks("t", "tok", column_header=True) == "a | b"


def test_table_location_uses_heading_path_and_caption(fake_notion):
    fake_notion["page"] = [
        text_block("heading_1", "Ripper"),
        text_block("heading_3", "VFX 적용 가이드"),
        text_block("paragraph", ""),
        text_block("paragraph", "attack"),
        table_block("t1"),
        text_block("heading_1", "Tank"),
        table_block("t2"),
    ]
    fake_notion["t1"] = [row("시간", "VFX"), row("0", "Slash")]
    fake_notion["t2"] = [row("태그 명"), row("Down_In")]
    out, images = [], []
    np.extract_blocks_recursive("page", "tok", out, images)
    markers = [line for part in out for line in part.split("\n") if line.startswith(np.TABLE_MARKER_PREFIX)]
    # h1이 바뀌면 하위 제목(h3)은 초기화된다
    assert markers == [np.table_marker("Ripper > VFX 적용 가이드 > attack"), np.table_marker("Tank")]


def test_long_paragraph_is_not_used_as_table_caption(fake_notion):
    fake_notion["page"] = [text_block("heading_2", "상세"), text_block("paragraph", "가" * 41), table_block("t")]
    fake_notion["t"] = [row("a"), row("b")]
    out = []
    np.extract_blocks_recursive("page", "tok", out, [])
    assert np.table_marker("상세") in out[-1]


def test_short_parent_block_is_part_of_location(fake_notion):
    fake_notion["page"] = [text_block("heading_1", "보스"), text_block("toggle", "Intro", children=True, block_id="tg")]
    fake_notion["tg"] = [table_block("t")]
    fake_notion["t"] = [row("a"), row("b")]
    out = []
    np.extract_blocks_recursive("page", "tok", out, [])
    assert np.table_marker("보스 > Intro") in out[-1]


def test_images_are_collected_without_url(fake_notion):
    fake_notion["page"] = [text_block("heading_1", "패턴"), text_block("heading_3", "Powerbomb"),
                           {"id": "img1", "type": "image", "has_children": False,
                            "image": {"file": {"url": "https://s3/x.png"}, "caption": rt("도판")}},
                           {"id": "img2", "type": "image", "has_children": False,
                            "image": {"file": {"url": "https://s3/y.png"}, "caption": []}}]
    images = []
    np.extract_blocks_recursive("page", "tok", [], images)
    # 이미지가 놓인 제목 경로를 함께 모은다 (캡션이 없는 이미지의 설명으로 쓴다)
    assert images == [{"block_id": "img1", "caption": "도판", "section": "패턴 > Powerbomb"},
                      {"block_id": "img2", "caption": "", "section": "패턴 > Powerbomb"}]


def test_page_link_is_kept_as_reference_without_its_content(monkeypatch, fake_notion):
    # 링크 대상 페이지의 본문은 읽지 않는다 (fake_notion에 'linked' 자식이 있어도 쓰이면 안 됨)
    fake_notion["linked"] = [text_block("paragraph", "링크 너머의 본문")]
    fake_notion["page"] = [{"id": "l1", "type": "link_to_page", "has_children": False,
                            "link_to_page": {"type": "page_id", "page_id": "linked"}}]
    pages = {"linked": {"properties": {"n": {"type": "title", "title": rt("전투 공식")}}, "url": "https://notion.so/linked"}}

    def fake_get(url, headers, timeout):
        page_id = url.rsplit("/", 1)[-1]
        found = page_id in pages
        return type("R", (), {"status_code": 200 if found else 404, "json": lambda self: pages.get(page_id, {})})()
    monkeypatch.setattr(np.requests, "get", fake_get)

    out = []
    np.extract_blocks_recursive("page", "tok", out, [])
    assert out == [f"{np.LINK_REFERENCE_PREFIX} [전투 공식](https://notion.so/linked)"]

    # 연동에 공유되지 않아 조회할 수 없는 링크는 남기지 않는다
    fake_notion["page"][0]["link_to_page"]["page_id"] = "private"
    out = []
    np.extract_blocks_recursive("page", "tok", out, [])
    assert out == []


def test_database_item_is_single_line(monkeypatch, fake_notion):
    monkeypatch.setattr(np, "query_notion_database", lambda db_id, token: [{
        "id": "r1",
        "properties": {
            "이름": {"type": "title", "title": rt("포자")},
            "요약": {"type": "rich_text", "rich_text": rt("첫 줄\n\n둘째 줄")},
        },
    }])
    fake_notion["r1"] = [text_block("paragraph", "본문 1"), text_block("bulleted_list_item", "본문 2"),
                         text_block("heading_3", "Spore"),
                         {"id": "img1", "type": "image", "has_children": False,
                          "image": {"file": {"url": "https://s3/x.png"}, "caption": rt("포자 도판")}}]
    images = []
    text = np.fetch_database_rows("db", "패턴 목록", "tok", images)
    lines = text.strip("\n").split("\n")
    assert lines == [f"{np.DB_MARKER_PREFIX} 패턴 목록] ###",
                     "- 항목: [포자] ➔ 요약='첫 줄 / 둘째 줄' | 내용: 본문 1 / • 본문 2 / Spore"]
    # 행 페이지의 이미지는 그 항목 이름과 함께 모인다
    assert images == [{"block_id": "img1", "caption": "포자 도판", "section": "Spore", "item": "포자"}]


@pytest.mark.parametrize("page,expected", [
    ({"in_trash": True, "archived": False}, True),
    ({"in_trash": False, "archived": True}, True),
    ({"in_trash": False, "archived": False}, False),
])
def test_trashed_page_is_not_parsed(monkeypatch, fake_notion, page, expected):
    monkeypatch.setattr(np, "fetch_page", lambda page_id, token: {**page, "properties": {}, "url": ""})
    result = np.parse_notion_page_deep("p", "tok")
    assert bool(result.get("trashed")) is expected


def test_page_state_check_failure_raises(monkeypatch, no_network):
    monkeypatch.setattr(np, "fetch_page", lambda page_id, token: None)
    with pytest.raises(RuntimeError):
        np.is_page_trashed("p", "tok")
