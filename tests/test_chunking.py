import ai_engine
import notion_parser as np


def chunks(text, title="몹"):
    return ai_engine.split_into_chunks(title, text)


def test_plain_text_is_grouped_up_to_limit():
    lines = [f"문장 {i} " + "가" * 40 for i in range(10)]
    result = chunks("\n\n".join(lines))
    assert len(result) > 1
    assert all(c.startswith("문서: 몹\n") for c in result)
    assert all(len(c) <= ai_engine.TEXT_CHUNK_CHARS + 50 for c in result)
    # 내용 누락 없음
    assert all(any(line in c for c in result) for line in lines)


def test_every_table_row_is_its_own_chunk_even_without_separator():
    table = "\n".join(["태그 명: Intro_1 | 설명: 점프", "태그 명: Down_In", "태그 명: Walk | 설명: 걷기"])
    text = "\n\n".join(["소개", f"\n{np.table_marker('Ripper > VFX')}\n{table}", "표 뒤 문단"])
    result = chunks(text)
    assert result == [
        "문서: 몹\n소개",
        "문서: 몹\n[상세 기획 표: Ripper > VFX]\n태그 명: Intro_1 | 설명: 점프",
        "문서: 몹\n[상세 기획 표: Ripper > VFX]\n태그 명: Down_In",
        "문서: 몹\n[상세 기획 표: Ripper > VFX]\n태그 명: Walk | 설명: 걷기",
        "문서: 몹\n표 뒤 문단",
    ]


def test_table_without_location_has_no_label():
    result = chunks(f"\n{np.table_marker()}\nA: 1 | B: 2")
    assert result == ["문서: 몹\nA: 1 | B: 2"]


def test_database_items_carry_database_name():
    db = f"\n\n{np.DB_MARKER_PREFIX} 패턴 목록] ###\n- 항목: [포자] ➔ 유형='구조물'\n- 항목: [항목2] ➔ 세부 정보 없음"
    result = chunks(db)
    assert result == [
        "문서: 몹\n[데이터베이스: 패턴 목록]\n- 항목: [포자] ➔ 유형='구조물'",
        "문서: 몹\n[데이터베이스: 패턴 목록]\n- 항목: [항목2] ➔ 세부 정보 없음",
    ]


def test_pipe_in_plain_text_does_not_split_it():
    assert chunks("문단 하나 | 파이프\n\n이어지는 문단") == ["문서: 몹\n문단 하나 | 파이프\n이어지는 문단"]


def test_long_unit_is_split_with_item_name_repeated():
    unit = "- 항목: [긴항목] ➔ " + " / ".join(f"문장{i} " + "가" * 300 for i in range(10))
    pieces = ai_engine.split_long_unit(unit)
    assert len(pieces) > 1
    assert all(len(p) <= ai_engine.MAX_UNIT_CHARS for p in pieces)
    assert all(p.startswith("- 항목: [긴항목]") for p in pieces)


def test_unit_with_one_huge_part_is_still_bounded():
    pieces = ai_engine.split_long_unit("가" * 5000)
    assert all(len(p) <= ai_engine.MAX_UNIT_CHARS for p in pieces)
    assert "".join(pieces) == "가" * 5000


def test_short_unit_is_untouched():
    assert ai_engine.split_long_unit("- 항목: [a] ➔ b") == ["- 항목: [a] ➔ b"]


def test_chunk_point_id_is_stable():
    assert ai_engine.chunk_point_id("page", 3) == ai_engine.chunk_point_id("page", 3)
    assert ai_engine.chunk_point_id("page", 3) != ai_engine.chunk_point_id("page", 4)
