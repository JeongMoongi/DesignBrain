import csv
from datetime import datetime, timedelta
import pytest
import ai_engine
import backlog
import export_qa
import qa_log
import qa_slack
import slack_service

KST = backlog.KST


def stored(**entry):
    qa_log.record({"question": "질문", "status": "success", "used_docs": [], "answer_ts": "1", **entry})


@pytest.mark.parametrize("reaction,expected", [
    ("+1", "up"), ("thumbsup", "up"), ("+1::skin-tone-3", "up"), ("-1", "down"), ("heart", None),
])
def test_feedback_of(reaction, expected):
    assert qa_log.feedback_of(reaction) == expected


def test_feedback_is_kept_per_user_and_removed_only_when_matching():
    stored(answer_ts="A1")
    assert qa_log.set_feedback("A1", "U1", "up")
    assert qa_log.set_feedback("A1", "U1", "down")      # 한 사람의 평가는 마지막 것 하나
    assert qa_log.set_feedback("A1", "U2", "up")
    assert qa_log.set_feedback("A1", "U1", "up", added=False)  # 👍를 지워도 👎는 남는다
    assert qa_log.entries()[0]["feedback"] == {"U1": "down", "U2": "up"}
    assert qa_log.set_feedback("A1", "U1", "down", added=False)
    assert qa_log.entries()[0]["feedback"] == {"U2": "up"}
    assert qa_log.set_feedback("없는 답변", "U1", "up") is False


def test_entries_filters_by_time(monkeypatch):
    for days_ago, question in ((10, "오래된 질문"), (1, "최근 질문")):
        moment = datetime.now(KST) - timedelta(days=days_ago)
        monkeypatch.setattr(qa_log, "datetime", type("D", (), {"now": staticmethod(lambda tz=None, m=moment: m)}))
        stored(question=question)
    monkeypatch.undo()
    recent = qa_log.entries(since=datetime.now(KST) - timedelta(days=7))
    assert [e["question"] for e in recent] == ["최근 질문"]
    assert [e["question"] for e in qa_log.entries()] == ["오래된 질문", "최근 질문"]


def test_weekly_report_highlights_what_to_work_on_next():
    records = [
        {"question": "석화체 패턴", "status": "success", "used_docs": ["챕터 1 몹"], "user_id": "U1", "user_name": "정문기",
         "feedback": {"U1": "up"}},
        {"question": "우주선 연료", "status": "success", "used_docs": [], "user_id": "U2", "user_name": "박준수", "feedback": {}},
        {"question": "부활 시간", "status": "error", "used_docs": [], "user_id": "U1", "user_name": "정문기",
         "feedback": {"U1": "down"}},
    ]
    until = datetime(2026, 10, 19, 10, tzinfo=KST)
    text = qa_log.weekly_report(records, until - timedelta(days=7), until)
    assert "10/12 ~ 10/19" in text
    assert "질문 3건 · 질문한 사람 2명 · 👍 1 · 👎 1" in text
    assert "답변 실패* (1건)\n• 부활 시간 — 정문기" in text
    assert "답을 못 찾은 질문* (1건)\n• 우주선 연료 — 박준수" in text
    assert "아쉬웠던 답변* (1건)\n• 부활 시간" in text
    assert "• 챕터 1 몹 (1회)" in text
    assert "질문이 없었어요" in qa_log.weekly_report([], until - timedelta(days=7), until)


def test_seconds_until_weekly():
    monday_9 = datetime(2026, 10, 19, 9, tzinfo=KST)  # 2026-10-19는 월요일
    assert qa_log.seconds_until_weekly(0, 10, monday_9) == 3600
    assert qa_log.seconds_until_weekly(0, 10, monday_9 + timedelta(hours=1)) == 7 * 86400


class ReplySlack:
    def __init__(self):
        self.updates = []

    def conversations_replies(self, **kwargs):
        return {"messages": [{"ts": "1", "user": "U1", "text": "석화체 알려줘"}, {"ts": "2", "bot_id": "B", "text": "설명"},
                             {"ts": "3", "user": "U1", "text": "패턴은?"}]}

    def chat_update(self, **kwargs):
        self.updates.append(kwargs)

    def users_info(self, user):
        return {"user": {"profile": {"display_name": "정문기"}}}


def run(monkeypatch, answer, raw_text="패턴은?"):
    monkeypatch.setattr(ai_engine, "ask_ai_with_history", lambda q, h: answer)
    monkeypatch.setattr(slack_service, "_user_names", {})
    slack_service.process_chat_and_reply(ReplySlack(), lambda text, thread_ts=None: {"ts": "4"}, "D1", "1", "3",
                                         raw_text, "U1")
    return qa_log.entries()


def test_answered_question_is_logged(monkeypatch):
    [entry] = run(monkeypatch, {"status": "success", "AI_답변": "**석화체** 패턴", "page_ids": ["P1"], "images": [],
                                "search_query": "석화체의 패턴은?", "used_docs": ["챕터 1 몹"]})
    assert entry["question"] == "패턴은?" and entry["search_query"] == "석화체의 패턴은?"
    assert entry["user_name"] == "정문기" and entry["channel_type"] == "dm" and entry["follow_up"] is True
    assert entry["status"] == "success" and entry["used_docs"] == ["챕터 1 몹"] and entry["answer"] == "*석화체* 패턴"
    # 리액션이 달릴 답변 메세지('분석 중'을 바꾼 메세지)
    assert entry["answer_ts"] == "4"


def test_failed_question_is_logged(monkeypatch):
    [entry] = run(monkeypatch, {"status": "error", "stage": "기획서 검색", "reason": "DB 연결 실패", "hint": "다시"})
    assert entry["status"] == "error" and entry["error_stage"] == "기획서 검색" and entry["error_reason"] == "DB 연결 실패"


def test_unexpected_error_is_logged(monkeypatch):
    def boom(q, h):
        raise ValueError("boom")
    monkeypatch.setattr(ai_engine, "ask_ai_with_history", boom)
    slack_service.process_chat_and_reply(ReplySlack(), lambda text, thread_ts=None: {"ts": "4"}, "C1", "3", "3", "질문", "U1")
    [entry] = qa_log.entries()
    assert entry["status"] == "error" and entry["error_stage"] == "메세지 처리" and entry["channel_type"] == "channel"


def test_logging_failure_does_not_block_the_answer(monkeypatch):
    def broken(entry):
        raise RuntimeError("qdrant down")
    monkeypatch.setattr(qa_log, "record", broken)
    monkeypatch.setattr(ai_engine, "ask_ai_with_history", lambda q, h: {"status": "success", "AI_답변": "답변",
                                                                         "page_ids": [], "images": []})
    slack = ReplySlack()
    slack_service.process_chat_and_reply(slack, lambda text, thread_ts=None: {"ts": "4"}, "D1", "1", "3", "질문", "U1")
    assert slack.updates[-1]["text"] == "답변"


def test_reaction_updates_feedback():
    stored(answer_ts="A1")
    qa_slack.handle_reaction({"reaction": "+1", "user": "U1", "item": {"type": "message", "ts": "A1"}}, added=True)
    qa_slack.handle_reaction({"reaction": "heart", "user": "U2", "item": {"type": "message", "ts": "A1"}}, added=True)
    assert qa_log.entries()[0]["feedback"] == {"U1": "up"}
    qa_slack.handle_reaction({"reaction": "+1", "user": "U1", "item": {"type": "message", "ts": "A1"}}, added=False)
    assert qa_log.entries()[0]["feedback"] == {}


def test_weekly_report_is_sent_to_configured_user(monkeypatch):
    monkeypatch.setattr(qa_slack.config, "QA_REPORT_USER_ID", "UME")
    stored(question="석화체 패턴")
    posted = []
    client = type("C", (), {"chat_postMessage": lambda self, **kw: posted.append(kw)})()
    qa_slack.send_weekly_report(client, now=datetime.now(KST) + timedelta(minutes=1))
    assert posted[0]["channel"] == "UME" and "질문 1건" in posted[0]["text"]


def test_export_csv_opens_in_excel(tmp_path):
    stored(question="석화체 패턴", used_docs=["챕터 1 몹", "챕터 2 몹"], user_name="정문기")
    qa_log.set_feedback("1", "U1", "up")
    path = tmp_path / "qa.csv"
    assert export_qa.export(str(path)) == 1
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # 엑셀이 한글을 읽도록 BOM
    rows = list(csv.reader(raw.decode("utf-8-sig").splitlines()))
    header, row = rows
    values = dict(zip(header, row))
    assert values["질문"] == "석화체 패턴" and values["사용한 기획서"] == "챕터 1 몹 | 챕터 2 몹"
    assert values["👍"] == "1" and values["결과"] == "답변" and values["질문한 사람"] == "정문기"
