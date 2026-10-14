import httpx
import openai
import pytest
from qdrant_client.http import exceptions as qe
import ai_engine
import slack_service
from fakes import FakeQdrant, fake_openai, hit, raiser

REQ = httpx.Request("POST", "https://api.openai.com/v1/x")


def status_error(cls, code, body=None):
    return cls("err", response=httpx.Response(code, request=REQ), body=body)


@pytest.mark.parametrize("error,reason_part,hint", [
    (status_error(openai.AuthenticationError, 401), "API 키", "관리자에게 알려주세요."),
    (status_error(openai.RateLimitError, 429, {"code": "insufficient_quota"}), "크레딧", "관리자에게 알려주세요."),
    (status_error(openai.RateLimitError, 429), "몰렸습니다", ai_engine.RETRY_HINT),
    (openai.APITimeoutError(request=REQ), "오래 걸려", ai_engine.RETRY_HINT),
    (openai.APIConnectionError(request=REQ), "연결하지 못했습니다", ai_engine.RETRY_HINT),
    (status_error(openai.InternalServerError, 500), "일시적인 문제", ai_engine.RETRY_HINT),
    (qe.UnexpectedResponse(401, "Unauthorized", b"", httpx.Headers()), "인증에 실패", "관리자에게 알려주세요."),
    (qe.UnexpectedResponse(500, "Error", b"", httpx.Headers()), "HTTP 500", ai_engine.ADMIN_HINT),
    (qe.ResponseHandlingException(Exception("timed out")), "Qdrant)에 연결하지 못했습니다", ai_engine.RETRY_HINT),
    (KeyError("x"), "KeyError", ai_engine.ADMIN_HINT),
])
def test_describe_failure(error, reason_part, hint):
    failure = ai_engine.describe_failure(error)
    assert reason_part in failure["reason"]
    assert failure["hint"] == hint


HIT = hit({"text": "본문", "title": "문서", "url": "https://notion/p", "page_id": "p", "images": []})


@pytest.mark.parametrize("stage,patch", [
    ("질문 분석", {"get_embedding": raiser(openai.APIConnectionError(request=REQ))}),
    ("기획서 검색", {"get_embedding": lambda q: [0.0], "get_qdrant": lambda: FakeQdrant(error=qe.ResponseHandlingException(Exception()))}),
    ("답변 생성", {"get_embedding": lambda q: [0.0], "get_qdrant": lambda: FakeQdrant([HIT]),
                "get_openai": lambda: fake_openai(error=openai.APITimeoutError(request=REQ))}),
])
def test_answer_failure_reports_stage(monkeypatch, stage, patch):
    for name, value in patch.items():
        monkeypatch.setattr(ai_engine, name, value)
    result = ai_engine.ask_ai_with_history("질문", [])
    assert result["status"] == "error"
    assert result["stage"] == stage
    assert result["reason"] and result["hint"]


def test_answer_success_appends_source(monkeypatch):
    monkeypatch.setattr(ai_engine, "get_embedding", lambda q: [0.0])
    monkeypatch.setattr(ai_engine, "get_qdrant", lambda: FakeQdrant([HIT]))
    monkeypatch.setattr(ai_engine, "get_openai", lambda: fake_openai(reply="답변"))
    result = ai_engine.ask_ai_with_history("질문", [])
    assert result["status"] == "success"
    assert result["AI_답변"].startswith("답변")
    assert "[문서](https://notion/p)" in result["AI_답변"]
    assert result["page_ids"] == ["p"]


HISTORY = [{"role": "user", "content": "디고리 체력이 얼마야?"}, {"role": "assistant", "content": "디고리 체력은 500입니다."}]


def test_first_question_is_searched_as_is(monkeypatch):
    # 스레드 첫 질문은 재작성 호출 없이 그대로 검색한다
    monkeypatch.setattr(ai_engine, "get_openai", raiser(AssertionError("재작성을 호출하면 안 됩니다")))
    assert ai_engine.rewrite_search_query("디고리 체력이 얼마야?", []) == "디고리 체력이 얼마야?"


def test_follow_up_is_searched_with_rewritten_query(monkeypatch):
    embedded = []
    monkeypatch.setattr(ai_engine, "get_embedding", lambda q: embedded.append(q) or [0.0])
    monkeypatch.setattr(ai_engine, "get_qdrant", lambda: FakeQdrant([HIT]))
    monkeypatch.setattr(ai_engine, "get_openai", lambda: fake_openai(reply="디고리의 공격 패턴은 어떻게 돼?"))
    result = ai_engine.ask_ai_with_history("공격 패턴은 어떻게 돼?", HISTORY)
    assert result["status"] == "success"
    assert embedded == ["디고리의 공격 패턴은 어떻게 돼?"]


@pytest.mark.parametrize("openai_client", [
    lambda: fake_openai(error=openai.APITimeoutError(request=REQ)),
    lambda: fake_openai(reply="  "),
])
def test_rewrite_failure_falls_back_to_original_query(monkeypatch, openai_client):
    monkeypatch.setattr(ai_engine, "get_openai", openai_client)
    assert ai_engine.rewrite_search_query("공격 패턴은?", HISTORY) == "공격 패턴은?"


class FakeSlack:
    def __init__(self):
        self.updates = []

    def conversations_replies(self, **kwargs):
        return {"messages": []}

    def chat_update(self, **kwargs):
        self.updates.append(kwargs)


def recorder(said):
    def say(text, thread_ts=None):
        said.append(text)
        return {"ts": str(len(said))}
    return say


def test_slack_shows_failure_reason(monkeypatch):
    monkeypatch.setattr(ai_engine, "ask_ai_with_history",
                        lambda q, h: {"status": "error", "stage": "기획서 검색", "reason": "원인", "hint": "안내"})
    said, slack = [], FakeSlack()
    slack_service.process_chat_and_reply(slack, recorder(said), "C", "1", "1", "<@BOT> 질문")
    # 실패 안내는 '분석 중' 메세지를 바꿔서 보여준다
    assert len(said) == 1
    assert slack.updates[-1]["ts"] == "1"
    assert slack.updates[-1]["text"] == slack_service.failure_message("기획서 검색", "원인", "안내")


def test_slack_reports_unexpected_errors_in_place(monkeypatch):
    monkeypatch.setattr(ai_engine, "ask_ai_with_history", raiser(ValueError("boom")))
    said, slack = [], FakeSlack()
    slack_service.process_chat_and_reply(slack, recorder(said), "C", "1", "1", "<@BOT> 질문")
    # '분석 중' 메세지가 실패 안내로 바뀌고, 새 메세지는 보내지 않는다
    assert len(said) == 1
    assert slack.updates[-1]["ts"] == "1"
    assert "메세지 처리" in slack.updates[-1]["text"] and "ValueError" in slack.updates[-1]["text"]


def test_slack_reports_errors_as_new_message_when_update_fails(monkeypatch):
    monkeypatch.setattr(ai_engine, "ask_ai_with_history", raiser(ValueError("boom")))
    slack = FakeSlack()
    slack.chat_update = raiser(RuntimeError("update failed"))
    said = []
    slack_service.process_chat_and_reply(slack, recorder(said), "C", "1", "1", "<@BOT> 질문")
    assert len(said) == 2 and "ValueError" in said[-1]
