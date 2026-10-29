import json
import threading
import types
import pytest
import agent
import ai_engine
import clients
import config
import indexing
import notion_parser
import notion_writer
import property_check
import proposal_slack
import qa_lens
import risk_notice

PAGE_ID = "11111111-1111-1111-1111-111111111111"
RISK_REPLY = {
    "level": "높음",
    "unknown": ["한 축만 0일 때 어떻게 밀리는지", "부호가 음수일 때 추가량이 더해지는지"],
    "tests": [{"name": "가산 순서", "test": "`(-1, 5)`에 `(3, 3)` 적용 → `(-4, 8)`"},
              {"name": "방향 비참조", "test": "바라보는 방향만 바꿔 비교 → 결과 동일"}],
    "views": ["입문 유저 기준 — 혈기 3중첩 조건을 설명 없이 이해하는지"]}
NOTHING = {"level": "낮음", "unknown": [], "tests": [], "views": []}


def response(text="", output=()):
    usage = types.SimpleNamespace(input_tokens=10, output_tokens=2)
    return types.SimpleNamespace(id="r", output=list(output), output_text=text, usage=usage)


class FakeAI:
    def __init__(self):
        self.reply = json.dumps(RISK_REPLY, ensure_ascii=False)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return response(self.reply)


class FakeSlack:
    def __init__(self):
        self.posts = []

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"channel": kwargs["channel"], "ts": "1.0"}


@pytest.fixture
def ai(monkeypatch):
    fake = FakeAI()
    monkeypatch.setattr(clients, "get_openai", lambda: types.SimpleNamespace(responses=fake))
    return fake


@pytest.fixture
def slack(monkeypatch):
    fake = FakeSlack()
    monkeypatch.setattr(proposal_slack, "web_client", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setattr(config, "TEST_NOTIFY_IDS", ["UQA1", "UQA2"])
    monkeypatch.setattr(config, "TEST_NOTICE_CHANGE_RATIO", 0.3)
    monkeypatch.setattr(risk_notice, "_analyzing", set())
    monkeypatch.setattr(config, "NOTION_INTEGRATION_TOKEN", None)                      # 문의할 사람 조회는 people 픽스처를 쓰는 테스트에서만


def body(changed=0, tag="수정"):
    """20줄짜리 본문. 앞에서부터 changed줄을 다른 내용으로 바꾼다"""
    return "\n".join(f"{tag} {i}" if i < changed else f"줄 {i}" for i in range(20))


def doc(text=None):
    return {"page_id": PAGE_ID, "title": "니사리엘", "url": "https://notion/P1", "path": ["영웅"], "full_text": body() if text is None else text}


# --- 분석 ---
def test_analyze_normalizes_the_model_output(ai):
    risks = risk_notice.analyze(doc())
    assert risks["level"] == "높음" and risks["unknown"] == RISK_REPLY["unknown"] and risks["views"] == RISK_REPLY["views"]
    assert risks["tests"] == RISK_REPLY["tests"]
    ai.reply = json.dumps({"level": "아주높음", "unknown": [f"상황{i}" for i in range(8)] + [3, ""], "views": [f"관점{i}" for i in range(5)],
                           "tests": [{"name": f"이름{i}", "test": f"해보기{i}"} for i in range(6)] + ["문자열", {"name": "내용 없음", "test": ""}]})
    risks = risk_notice.analyze(doc())
    assert risks["level"] == "보통" and len(risks["unknown"]) == risk_notice.MAX_UNKNOWN
    assert len(risks["tests"]) == risk_notice.MAX_TESTS and len(risks["views"]) == risk_notice.MAX_VIEWS
    ai.reply = json.dumps({"unknown": "문자열이 와도", "tests": "깨지지 않는다", "views": None})
    assert not risk_notice.has_content(risk_notice.analyze(doc()))
    ai.reply = "JSON이 아님"
    assert not risk_notice.has_content(risk_notice.analyze(doc()))


def test_analysis_prompt_separates_unknowns_from_testable_things_and_filters_views():
    prompt = risk_notice.PROMPT
    assert "추상적인 이름('부호·0 경계') 대신 구체적인 상황" in prompt                      # 미정의는 상황 문장으로
    assert "정답이 적혀 있지 않은 것을 tests에 넣지 말고 unknown에 넣어라" in prompt         # 정답을 모르는 것은 바로 테스트할 것이 아니다
    assert qa_lens.VIEW_RULE in prompt and qa_lens.JUDGE_RULE in prompt
    assert "개발자 관점" in prompt and "해당하는 게 없으면 쓰지 않는다" in prompt           # 추상적·개발자 관점은 쓰지 않는다
    assert "해보기 → 정상 기준" in prompt and "세 배열을 모두 비워라" in prompt
    assert "연출·애니메이션의 타이밍이 동작과 맞물린다" in prompt                      # 위험 신호를 빠뜨리지 않았다


def test_overlong_items_are_cut_with_an_ellipsis(ai):
    ai.reply = json.dumps({"level": "보통", "unknown": ["가" * 200], "views": ["나" * 200],
                           "tests": [{"name": "다" * 80, "test": "라" * 500}]})
    risks = risk_notice.analyze(doc())
    assert len(risks["unknown"][0]) == risk_notice.UNKNOWN_CHARS and risks["unknown"][0].endswith("…")
    assert len(risks["views"][0]) == risk_notice.VIEW_CHARS
    assert len(risks["tests"][0]["name"]) == risk_notice.TEST_NAME_CHARS and len(risks["tests"][0]["test"]) == risk_notice.TEST_CHARS


# --- 얼마나 바뀌었나 ---
def test_change_ratio():
    same = risk_notice.fingerprint("가\n나\n다")
    assert risk_notice.change_ratio(same, risk_notice.fingerprint("다\n  나  \n\n가")) == 0       # 순서·공백·빈 줄은 무시
    assert risk_notice.change_ratio(same, risk_notice.fingerprint("라\n마")) == 1
    assert risk_notice.change_ratio([], []) == 0
    base = risk_notice.fingerprint(body())
    assert risk_notice.change_ratio(base, risk_notice.fingerprint(body(4))) == pytest.approx(0.2)              # 20줄 중 4줄 수정
    assert risk_notice.change_ratio(base, risk_notice.fingerprint(body() + "\n추가 1\n추가 2")) == pytest.approx(0.1)  # 2줄 추가
    assert risk_notice.change_ratio(base, risk_notice.fingerprint("\n".join(f"줄 {i}" for i in range(14)))) == pytest.approx(0.3)  # 6줄 삭제
    assert risk_notice.change_ratio([], risk_notice.fingerprint("새 문서")) == 1


# --- 알림 ---
def test_first_registration_sends_one_plain_dm_to_every_qa(ai, slack):
    assert risk_notice.notify(doc()) is True
    assert [p["channel"] for p in slack.posts] == ["UQA1", "UQA2"]
    assert "blocks" not in slack.posts[0]                                              # 버튼 없이 글만 간다
    assert slack.posts[0]["text"] == (
        "🔎 <https://notion/P1|니사리엘> 등록됐어요 (🔴 높음) · 🆕 처음 등록\n"
        "\n"
        "❓ *미정의* — 기획에 먼저 물어보세요\n"
        "• 한 축만 0일 때 어떻게 밀리는지\n"
        "• 부호가 음수일 때 추가량이 더해지는지\n"
        "\n"
        "🎯 *바로 테스트할 것*\n"
        "1. 가산 순서 — `(-1, 5)`에 `(3, 3)` 적용 → `(-4, 8)`\n"
        "2. 방향 비참조 — 바라보는 방향만 바꿔 비교 → 결과 동일\n"
        "\n"
        "🎭 *이 관점으로 보세요*\n"
        "• 입문 유저 기준 — 혈기 3중첩 조건을 설명 없이 이해하는지\n"
        "\n"
        "💬 더 궁금하면 `@Brain 니사리엘 QA 뭘 볼까?`")


def test_sections_without_content_are_left_out(ai, slack):
    ai.reply = json.dumps({"level": "보통", "unknown": [], "views": [], "tests": [{"name": "", "test": "A 적용 → B"}]})
    risk_notice.notify(doc())
    text = slack.posts[0]["text"]
    assert "🎯 *바로 테스트할 것*\n1. A 적용 → B" in text                                # 이름이 없으면 해보기만
    assert "❓" not in text and "🎭" not in text


def test_notice_is_short_enough_to_skim(ai, slack):
    ai.reply = json.dumps({"level": "보통", "unknown": ["u" * 300] * 9, "views": ["v" * 300] * 9,
                           "tests": [{"name": "n" * 99, "test": "t" * 500}] * 9})
    risk_notice.notify(doc())
    lines = slack.posts[0]["text"].splitlines()
    assert sum(l.startswith("• ") for l in lines) == risk_notice.MAX_UNKNOWN + risk_notice.MAX_VIEWS
    assert sum(l[:2] in ("1.", "2.", "3.", "4.") for l in lines) == risk_notice.MAX_TESTS
    assert max(len(l) for l in lines) <= risk_notice.TEST_NAME_CHARS + risk_notice.TEST_CHARS + 6      # 어떤 줄도 길게 늘어지지 않는다


def test_first_alert_after_a_quiet_analysis_is_still_a_first_registration(ai, slack):
    """처음엔 알릴 게 없어 기록만 남겼다면, 나중에 많이 바뀌어 처음 받는 알림은 '수정'이 아니라 '처음 등록'이다"""
    ai.reply = json.dumps(NOTHING)
    assert risk_notice.notify(doc()) is False and slack.posts == []
    ai.reply = json.dumps(RISK_REPLY)
    assert risk_notice.notify(doc(body(8))) is True
    first = slack.posts[0]["text"]
    assert "· 🆕 처음 등록" in first and "수정" not in first.splitlines()[0]
    assert risk_notice.get_notice(PAGE_ID)["notified"] is True
    assert risk_notice.notify(doc(body(20, tag="전면수정"))) is True                  # 알림을 받은 뒤의 변경은 수정으로 표시
    assert "· 🔄 " in slack.posts[2]["text"] and "% 수정" in slack.posts[2]["text"]


def test_a_changed_document_says_how_much_changed(ai, slack):
    risk_notice.notify(doc())
    assert risk_notice.notify(doc(body(8))) is True
    assert "· 🔄 40% 수정" in slack.posts[2]["text"] and "🆕" not in slack.posts[2]["text"]   # 20줄 중 8줄


def test_small_edits_are_not_announced_again(ai, slack):
    risk_notice.notify(doc())
    calls = len(ai.requests)
    assert risk_notice.notify(doc()) is False                                          # 같은 내용으로 'DB 등록'을 또 눌러도
    assert risk_notice.notify(doc(body(2))) is False                                   # 20줄 중 2줄만 바뀌어도
    assert len(ai.requests) == calls and len(slack.posts) == 2                         # 분석도 하지 않는다


def test_a_big_change_is_announced_again(ai, slack):
    risk_notice.notify(doc())
    assert risk_notice.notify(doc(body(8))) is True
    assert len(slack.posts) == 4


def test_small_edits_pile_up_until_they_add_up_to_a_big_change(ai, slack):
    risk_notice.notify(doc())
    assert risk_notice.notify(doc(body(2))) is False
    assert risk_notice.notify(doc(body(3, tag="또수정"))) is False                      # 아직 지난번 알린 본문 기준으로 15%
    assert risk_notice.notify(doc(body(6, tag="또수정"))) is True                       # 지난번 알린 본문 기준으로 30%를 넘으면 알린다


def test_change_threshold_is_configurable(ai, slack, monkeypatch):
    risk_notice.notify(doc())
    monkeypatch.setattr(config, "TEST_NOTICE_CHANGE_RATIO", 0.05)
    assert risk_notice.notify(doc(body(2))) is True


def test_no_risk_means_no_message_and_no_repeated_analysis(ai, slack):
    ai.reply = json.dumps(NOTHING)
    assert risk_notice.notify(doc()) is False and slack.posts == []
    calls = len(ai.requests)
    assert risk_notice.notify(doc()) is False and len(ai.requests) == calls            # 같은 본문을 또 분석하지 않는다
    ai.reply = json.dumps(RISK_REPLY)
    assert risk_notice.notify(doc(body(8))) is True                                    # 많이 바뀌면 다시 본다


def test_nothing_happens_without_recipients(ai, slack, monkeypatch):
    monkeypatch.setattr(config, "TEST_NOTIFY_IDS", [])
    assert risk_notice.notify(doc()) is False and ai.requests == [] and slack.posts == []


def test_no_slack_token_skips_without_wasting_an_analysis(ai, monkeypatch):
    monkeypatch.setattr(proposal_slack, "web_client", lambda: None)
    assert risk_notice.notify(doc()) is False and ai.requests == []


def test_one_unreachable_recipient_does_not_block_the_others(ai, slack):
    original = slack.chat_postMessage

    def flaky(**kwargs):
        if kwargs["channel"] == "UQA1":
            raise RuntimeError("channel_not_found")
        return original(**kwargs)
    slack.chat_postMessage = flaky
    assert risk_notice.notify(doc()) is True
    assert [p["channel"] for p in slack.posts] == ["UQA2"]


def test_failed_delivery_is_retried_next_time(ai, slack):
    slack.chat_postMessage = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("down"))
    assert risk_notice.notify(doc()) is False
    assert risk_notice.get_notice(PAGE_ID) is None                                     # 한 명에게도 못 보냈으면 기록하지 않는다
    assert len(ai.requests) == 1


def test_a_second_register_click_during_analysis_does_not_double_notify(ai, slack, monkeypatch):
    original, inner = risk_notice.analyze, []

    def analyze_and_click_again(doc_data):
        inner.append(risk_notice.notify(doc_data))                                     # 분석하는 동안 같은 문서를 또 등록했다
        return original(doc_data)
    monkeypatch.setattr(risk_notice, "analyze", analyze_and_click_again)
    assert risk_notice.notify(doc()) is True
    assert inner == [False] and len(slack.posts) == 2 and risk_notice._analyzing == set()


class Immediate:
    def __init__(self, target, args=(), **kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


def test_async_runs_in_the_background_and_never_raises(monkeypatch):
    ran = []
    monkeypatch.setattr(risk_notice, "threading", types.SimpleNamespace(Thread=Immediate))
    monkeypatch.setattr(risk_notice, "notify", lambda d, requested_by=None: ran.append((d["title"], requested_by)))
    risk_notice.notify_async(doc(), "N1")
    assert ran == [("니사리엘", "N1")]                                                  # 'DB 등록'을 누른 사람이 넘어간다
    monkeypatch.setattr(risk_notice, "notify", lambda d, requested_by=None: (_ for _ in ()).throw(RuntimeError("분석 실패")))
    risk_notice.notify_async(doc())                                                    # 실패해도 예외가 새지 않는다
    monkeypatch.setattr(config, "TEST_NOTIFY_IDS", [])
    monkeypatch.setattr(risk_notice, "notify", lambda d, requested_by=None: ran.append("호출됨"))
    risk_notice.notify_async(doc())
    assert ran == [("니사리엘", "N1")]                                                  # 받을 사람이 없으면 아무것도 하지 않는다


# --- 미정의를 물어볼 사람 ---
OWNER_PAGE = {"properties": {"담당자": {"type": "people", "people": [{"id": "N-OWNER", "name": "박상우"}]}}}


@pytest.fixture
def people(monkeypatch):
    """노션 문서의 담당자와 노션 → 슬랙 사용자 연결을 가짜로 바꾼다"""
    state = types.SimpleNamespace(page=OWNER_PAGE, fetched=[], slack_ids={"N-OWNER": "USANGWOO", "N-REQ": "UREQ"},
                                  names={"N-REQ": "김제민"})
    monkeypatch.setattr(config, "NOTION_INTEGRATION_TOKEN", "tok")
    monkeypatch.setattr(notion_parser, "fetch_page", lambda page_id, token: state.fetched.append(page_id) or state.page)
    monkeypatch.setattr(proposal_slack, "slack_user_for_notion", lambda client, notion_id: state.slack_ids.get(notion_id))
    monkeypatch.setattr(notion_writer, "user_info", lambda notion_id: {"name": state.names.get(notion_id, "")})
    return state


def test_unknowns_name_the_document_owner_to_ask(ai, slack, people):
    risk_notice.notify(doc())
    assert "❓ *미정의* — 기획에 먼저 물어보세요 (문의: <@USANGWOO>)" in slack.posts[0]["text"]     # DM에는 슬랙 멘션


def test_channels_get_the_name_so_the_owner_is_not_pinged(ai, slack, people, monkeypatch):
    monkeypatch.setattr(config, "TEST_NOTIFY_IDS", ["UQA1", "C0QACHANNEL"])
    risk_notice.notify(doc())
    assert "(문의: <@USANGWOO>)" in slack.posts[0]["text"] and "(문의: 박상우)" in slack.posts[1]["text"]


def test_without_an_owner_the_person_who_registered_is_asked(ai, slack, people):
    people.page = {"properties": {"담당자": {"type": "people", "people": []}}}
    risk_notice.notify(doc(), requested_by="N-REQ")
    assert "(문의: <@UREQ>)" in slack.posts[0]["text"]
    assert people.names == {"N-REQ": "김제민"}


def test_name_is_used_when_the_owner_is_not_in_slack(ai, slack, people):
    people.slack_ids = {}
    risk_notice.notify(doc())
    assert "(문의: 박상우)" in slack.posts[0]["text"]


def test_no_contact_is_better_than_a_wrong_one(ai, slack, people):
    people.page = {"properties": {}}
    risk_notice.notify(doc())                                                          # 담당자도 누른 사람도 모른다
    assert "미정의* — 기획에 먼저 물어보세요\n" in slack.posts[0]["text"] and "문의:" not in slack.posts[0]["text"]


def test_contact_lookup_failure_still_sends_the_notice(ai, slack, people, monkeypatch):
    monkeypatch.setattr(notion_parser, "fetch_page", lambda page_id, token: (_ for _ in ()).throw(RuntimeError("노션 장애")))
    monkeypatch.setattr(proposal_slack, "slack_user_for_notion", lambda client, notion_id: (_ for _ in ()).throw(RuntimeError("슬랙 장애")))
    assert risk_notice.notify(doc(), requested_by="N-REQ") is True
    assert "문의:" not in slack.posts[0]["text"] and "❓ *미정의*" in slack.posts[0]["text"]


def test_contact_is_only_looked_up_when_there_is_something_to_ask(ai, slack, people):
    ai.reply = json.dumps({"level": "보통", "unknown": [], "views": [], "tests": [{"name": "a", "test": "b → c"}]})
    risk_notice.notify(doc())
    assert people.fetched == []


# --- 연결 ---
@pytest.fixture
def sync_env(local_db, monkeypatch, no_network):
    calls = []
    monkeypatch.setattr(property_check, "run", lambda *a: None)
    monkeypatch.setattr(risk_notice, "notify_async", lambda doc_data, requested_by=None: calls.append((doc_data["title"], requested_by)))
    monkeypatch.setattr(notion_parser, "fetch_page", lambda page_id, token: {
        "in_trash": False, "archived": False, "url": "https://notion/P", "properties": {"n": {"type": "title", "title": [{"plain_text": "문서"}]}}})
    monkeypatch.setattr(notion_parser, "extract_blocks_recursive", lambda page_id, token, out, images, *a, **k: out.extend(["본문 한 줄", "본문 두 줄"]))
    monkeypatch.setattr(clients, "get_embeddings", lambda texts: [[0.1] * 1536 for _ in texts])
    return calls


def test_notice_comes_only_with_the_db_register_button(sync_env):
    indexing.sync_document_to_db(PAGE_ID, "tok")                                       # 전체 재동기화
    indexing.sync_document_to_db(PAGE_ID, "tok", requested_by="N1")                    # 사서 제안 승인 뒤 재동기화
    assert sync_env == []
    indexing.sync_document_to_db(PAGE_ID, "tok", requested_by="N1", check_properties=True)   # 'DB 등록' 버튼
    assert sync_env == [("문서", "N1")]                                                # 누른 사람도 함께 넘어간다


# --- 질문 답변 에이전트의 QA 답변 ---
def test_qa_rules_reach_the_agent_and_cover_every_lens(monkeypatch):
    sent = []

    class Responses:
        def create(self, **kwargs):
            sent.append(kwargs)
            return response("니사리엘 QA 포인트예요.")
    monkeypatch.setattr(clients, "get_openai", lambda: types.SimpleNamespace(responses=Responses()))
    monkeypatch.setattr(ai_engine, "finish_answer", lambda query, reply, *args: {"AI_답변": reply})
    result = agent.ask_agent("니사리엘 QA 할건데 뭘 볼까?", [], {})
    instructions = sent[0]["instructions"]
    assert qa_lens.QA_PROMPT in instructions and instructions.startswith(agent.SYSTEM_PROMPT[:50])
    assert "해보기: ○○ → 정상: ○○" in instructions and "전체 15줄 안팎" in instructions   # 따라 할 수 있는 모양으로 답하게 한다
    # 알림 DM과 같은 세 덩어리, 같은 관점 규칙
    assert "❓ 미정의" in instructions and "🎯 바로 테스트할 것" in instructions and "🎭 이 관점으로 보세요" in instructions
    assert qa_lens.VIEW_RULE in instructions and qa_lens.JUDGE_RULE in instructions
    assert all(lens.name in instructions and lens.focus in instructions for lens in qa_lens.LENSES)
    assert "되묻지 말고" in instructions and "export_markdown" in qa_lens.QA_PROMPT and "- [ ]" in qa_lens.QA_PROMPT
    assert result["AI_답변"] == "니사리엘 QA 포인트예요."
