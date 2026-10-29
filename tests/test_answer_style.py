import re
import types
import pytest
import agent
import ai_engine
import answer_style
import backlog
import clients
import qa_lens
import search
import slack_service

format_markdown = slack_service.format_markdown_for_slack


# --- 별표가 그대로 보이던 굵게 표시 (조사가 바로 붙으면 슬랙이 굵게 처리하지 못한다) ---
@pytest.mark.parametrize("text,expected", [
    ("맵 풀은 초기 *50종*이며, 챕터마다", "맵 풀은 초기 50종이며, 챕터마다"),
    ("*고유 인카운터는 폐기된 콘텐츠*로 명시돼 있습니다.", "고유 인카운터는 폐기된 콘텐츠로 명시돼 있습니다."),
    ("저항 계산 *뒤에 가산*하지만, ‘경직’은 *지속 100이면 이동량 0*이라고", "저항 계산 뒤에 가산하지만, ‘경직’은 지속 100이면 이동량 0이라고"),
    ("**굵게**이며", "굵게이며"),                                                    # **형식도 같은 처리
    ("속도 *2*배, 공격력 *1.5*배", "속도 2배, 공격력 1.5배"),                         # 굵게 처리한 수치 뒤에 조사가 붙은 경우
    # 띄어 쓰지 않은 곱셈은 별표를 떼면 수치가 바뀌어 보인다 (2*3*4가 234로). 굵게와 같은 모양이라 확실한 경우가 아니면 그대로 둔다
    ("공격력 2*3*4", "공격력 2*3*4"),
    ("2*3*4", "2*3*4"),
    ("(2*3*4)", "(2*3*4)"),
    ("1*2*3*4*5", "1*2*3*4*5"),
    ("ATK*1.5*치명", "ATK*1.5*치명"),
    ("a*b*c", "a*b*c"),
    ("공격력*3*배", "공격력*3*배"),
    ("공격력*1.5, 방어*2", "공격력*1.5, 방어*2"),
    ("앞에 글자*가 붙은* 굵게", "앞에 글자*가 붙은* 굵게"),                          # 여는 별표가 글자에 붙으면 곱셈일 수도 있어 그대로
    ("*굵게*2배", "*굵게*2배"),                                                     # 닫는 별표 뒤가 숫자면 곱셈일 수 있어 그대로
    # 제대로 보이는 굵게, 코드, 곱셈은 건드리지 않는다
    ("• *로비* — 입장 연출", "• *로비* — 입장 연출"),
    ("(*굵게*), *굵게*. *굵게*, *굵게*!", "(*굵게*), *굵게*. *굵게*, *굵게*!"),
    ("*굵게*\n다음 줄", "*굵게*\n다음 줄"),
    ("**결론:** 폭주는 `3`에서", "*결론:* 폭주는 `3`에서"),
    ("공식은 `파워 * 저항 * 노이즈`예요", "공식은 `파워 * 저항 * 노이즈`예요"),
    ("파워 * 저항 * 노이즈", "파워 * 저항 * 노이즈"),
    ("`a*b*c`는 코드", "`a*b*c`는 코드"),
    ("```\n*50종*이며\n```", "```\n*50종*이며\n```"),                                # 코드 블록 안은 그대로
])
def test_bold_that_slack_cannot_render_loses_its_asterisks(text, expected):
    assert format_markdown(text) == expected


def test_tight_products_never_change_their_numbers():
    """어떤 인수 조합이든 별표를 떼어 2*3*4가 234처럼 바뀌어 보이면 안 된다 (기획서 답변에서 수치가 가장 정확해야 한다)"""
    factors = ["2", "1.5", "10", "ATK", "a", "공격력", "치명"]
    for x in factors:
        for y in factors:
            for z in factors:
                expr = f"{x}*{y}*{z}"
                assert format_markdown(expr) == expr and format_markdown(f"공격력 {expr}이에요") == f"공격력 {expr}이에요"


# --- 백로그 참고 문구: 같은 머리말을 줄마다 반복하지 않는다 ---
def waiting(text):
    return f"{backlog.WAITING_NOTE_PREFIX}{text}"


def test_waiting_notes_share_one_header():
    notes = [waiting("결정이 필요함: 계산 방식"), waiting("결정이 필요함: 한 축만 0인 경우"), "정문기 님이 A를 확인하셨습니다."]
    text = ai_engine.format_notes(notes)
    assert text.splitlines() == ["📌 참고 : 정문기 님이 A를 확인하셨습니다.", "📌 아직 기획자 확인을 기다리는 부분이에요",
                                 "• 결정이 필요함: 계산 방식", "• 결정이 필요함: 한 축만 0인 경우"]
    assert text.count("아직 기획자 확인을 기다리는") == 1


def test_a_single_waiting_note_and_other_notes_keep_their_old_look():
    assert ai_engine.format_notes([waiting("결정이 필요함: 계산 방식")]) == f"📌 참고 : {waiting('결정이 필요함: 계산 방식')}"
    assert ai_engine.format_notes(["A", "B"]) == "📌 참고 : A\n📌 참고 : B"


def test_answers_carry_the_grouped_notes(monkeypatch):
    monkeypatch.setattr(backlog, "notes_for_answer", lambda vec, pages: [waiting("A"), waiting("B"), waiting("C")])
    answer = ai_engine.finish_answer("질문", "답변", [], [], "질문", [0.1])["AI_답변"]
    assert answer == "답변\n\n📌 아직 기획자 확인을 기다리는 부분이에요\n• A\n• B\n• C"


# --- 두 답변 방식 모두에 읽기 기준이 들어간다 ---
def test_style_rules_cover_what_makes_answers_hard_to_read():
    text = answer_style.STYLE_PROMPT
    for rule in ("첫 줄에 결론", "40자 안팎", "60자를 넘기지", "3줄 이하의 덩어리", "한 줄에 하나씩", "본문에서 말하지 마라"):
        assert rule in text


def test_style_only_shapes_the_writing_and_never_trims_the_content():
    """eval_agent.py로 확인된 회귀: 항목 수 제한과 '군더더기' 규칙이 목록과 '참고로' 줄을 잘라냈다"""
    text = answer_style.STYLE_PROMPT
    assert "항상 우선" in text and "내용을 줄이는 데 이 기준을 쓰지 마라" in text           # 내용 규칙이 먼저다
    assert not re.search(r"최대 \d+개|\d+개까지|\d+개 이하", text)                          # 항목 개수 제한은 없다
    assert "개수를 줄이지 말고 문서에 있는 것을 모두" in text and "수치·조건을 빼지 마라" in text   # 목록·구성 질문은 빠짐없이
    assert "'참고로' 한 줄" in text and "군더더기가 아니라 가장 도움이 되는 부분" in text        # 헷갈린 것을 바로잡는 줄은 허용
    assert "수치·식·좌표에만" in text and "항목명" not in text and "이름은 코드로 감싸지 마라" in text   # 코드는 값에만


def test_style_does_not_fight_the_formatting_fix_for_bold():
    # 조사가 붙은 굵게는 slack_service.drop_unrenderable_bold가 정리하므로 모델에게 따로 시키지 않는다 (어색한 띄어쓰기가 생겼다)
    text = answer_style.STYLE_PROMPT
    assert "조사" not in text and "별표가 그대로" not in text


def test_style_agrees_with_the_agents_own_content_rules():
    prompt = agent.SYSTEM_PROMPT
    assert "'참고로 ~는 있어요'처럼" in prompt and "범주별로 각각 찾아 모아라" in prompt     # 에이전트의 기존 내용 규칙
    assert prompt.index("[답변 원칙]") < prompt.index("[읽기 쉬운 답변]")                    # 읽기 기준은 그 뒤에 덧붙는다


def test_agent_gets_style_before_qa_rules():
    prompt = agent.SYSTEM_PROMPT
    assert answer_style.STYLE_PROMPT in prompt and qa_lens.QA_PROMPT in prompt
    assert prompt.index(answer_style.STYLE_PROMPT) < prompt.index(qa_lens.QA_PROMPT)


def test_classic_answers_get_the_same_style(monkeypatch):
    sent = []
    point = types.SimpleNamespace(id="p1", score=0.9, payload={"page_id": "P1", "title": "문서", "text": "본문", "url": "https://notion/p"})
    completion = types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="답변"))])
    openai = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(
        create=lambda **kwargs: sent.append(kwargs) or completion)))
    monkeypatch.setattr(search, "rewrite_search_query", lambda query, history: query)
    monkeypatch.setattr(search, "search_chunks", lambda query, vector: ([point], 0))
    monkeypatch.setattr(clients, "get_embedding", lambda text: [0.1])
    monkeypatch.setattr(clients, "get_openai", lambda: openai)
    monkeypatch.setattr(backlog, "notes_for_answer", lambda vec, pages: [])
    ai_engine._ask_ai("질문", [], {})
    system = sent[0]["messages"][0]["content"]
    assert answer_style.STYLE_PROMPT in system and "정확하고 상세하게" not in system
