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
import property_check
import proposal_slack
import qa_lens
import risk_notice

PAGE_ID = "11111111-1111-1111-1111-111111111111"
RISK_REPLY = {"level": "높음", "areas": [
    {"area": "폭주 상태 전이", "reason": "혈기 3중첩에서 폭주로 바뀌는 조건이 겹친다", "lens": ["edge", "combo", "없는관점"], "check": "중첩 직전/직후 값"},
    {"area": "수치 모호", "reason": "폭주 지속시간이 적혀 있지 않다", "lens": ["data"], "check": ""}]}


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


def body(changed=0, tag="수정"):
    """20줄짜리 본문. 앞에서부터 changed줄을 다른 내용으로 바꾼다"""
    return "\n".join(f"{tag} {i}" if i < changed else f"줄 {i}" for i in range(20))


def doc(text=None):
    return {"page_id": PAGE_ID, "title": "니사리엘", "url": "https://notion/P1", "path": ["영웅"], "full_text": body() if text is None else text}


# --- 분석 ---
def test_analyze_normalizes_the_model_output(ai):
    risks = risk_notice.analyze(doc())
    assert risks["level"] == "높음" and [a["area"] for a in risks["areas"]] == ["폭주 상태 전이", "수치 모호"]
    assert risks["areas"][0]["lens"] == ["edge", "combo"]                              # 모르는 관점은 버린다
    ai.reply = json.dumps({"level": "아주높음", "areas": [{"area": f"영역{i}", "reason": "r"} for i in range(8)] + ["문자열", {"reason": "이름 없음"}]})
    risks = risk_notice.analyze(doc())
    assert risks["level"] == "보통" and len(risks["areas"]) == risk_notice.MAX_AREAS
    ai.reply = "JSON이 아님"
    assert risk_notice.analyze(doc())["areas"] == []


def test_analysis_prompt_uses_the_shared_lenses():
    assert all(f"{lens.key}({lens.name})" in risk_notice.PROMPT for lens in qa_lens.LENSES)


# --- 얼마나 바뀌었나 ---
def test_change_ratio():
    same = risk_notice.fingerprint("가\n나\n다")
    assert risk_notice.change_ratio(same, risk_notice.fingerprint("다\n  나  \n\n가")) == 0       # 순서·공백·빈 줄은 무시
    assert risk_notice.change_ratio(same, risk_notice.fingerprint("라\n마")) == 1
    assert risk_notice.change_ratio([], []) == 0
    assert 0.3 < risk_notice.change_ratio(risk_notice.fingerprint(body()), risk_notice.fingerprint(body(8))) < 0.7


# --- 알림 ---
def test_first_registration_sends_one_plain_dm_to_every_qa(ai, slack):
    assert risk_notice.notify(doc()) is True
    assert [p["channel"] for p in slack.posts] == ["UQA1", "UQA2"]
    sent = slack.posts[0]
    assert "blocks" not in sent                                                        # 버튼 없이 글만 간다
    text = sent["text"]
    assert "<https://notion/P1|니사리엘>" in text and "🔴 높음" in text and "폭주 상태 전이" in text
    assert "↳ 확인: 중첩 직전/직후 값" in text and "↳ 관점: 경계·예외, 상호작용·조합" in text
    assert "`@Brain 니사리엘 QA 할건데 뭘 볼까?`" in text                                  # 더 궁금하면 편하게 물어보라는 안내


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
    ai.reply = json.dumps({"level": "낮음", "areas": []})
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
    monkeypatch.setattr(risk_notice, "notify", lambda d: ran.append(d["title"]))
    risk_notice.notify_async(doc())
    assert ran == ["니사리엘"]
    monkeypatch.setattr(risk_notice, "notify", lambda d: (_ for _ in ()).throw(RuntimeError("분석 실패")))
    risk_notice.notify_async(doc())                                                    # 실패해도 예외가 새지 않는다
    monkeypatch.setattr(config, "TEST_NOTIFY_IDS", [])
    monkeypatch.setattr(risk_notice, "notify", lambda d: ran.append("호출됨"))
    risk_notice.notify_async(doc())
    assert ran == ["니사리엘"]                                                         # 받을 사람이 없으면 아무것도 하지 않는다


# --- 연결 ---
@pytest.fixture
def sync_env(local_db, monkeypatch, no_network):
    calls = []
    monkeypatch.setattr(property_check, "run", lambda *a: None)
    monkeypatch.setattr(risk_notice, "notify_async", lambda doc_data: calls.append(doc_data["title"]))
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
    assert sync_env == ["문서"]


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
    assert all(lens.name in instructions and lens.focus in instructions for lens in qa_lens.LENSES)
    assert "되묻지 말고" in instructions and "export_markdown" in qa_lens.QA_PROMPT and "- [ ]" in qa_lens.QA_PROMPT
    assert result["AI_답변"] == "니사리엘 QA 포인트예요."
