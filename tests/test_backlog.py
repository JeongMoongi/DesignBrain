import math
from datetime import datetime
import pytest
import ai_engine
import clients
import indexing
import backlog
import config
import slack_service
from fakes import FakeQdrant, fake_openai, hit, raiser

DIM = config.EMBEDDING_DIM


def vector_at(score):
    """질문 벡터 [1, 0, ...]과의 코사인 유사도가 score가 되는 단위 벡터"""
    return [score, math.sqrt(1 - score * score)] + [0.0] * (DIM - 2)


QUERY = vector_at(1.0)


@pytest.fixture
def store(local_db, monkeypatch, no_network):
    """백로그 내용 문자열 -> 유사도를 정해 두고, OpenAI 없이 등록한다"""
    scores = {}
    monkeypatch.setattr(clients, "get_embedding", lambda text: vector_at(scores.get(text, 0.0)))
    monkeypatch.setattr(backlog, "write_note", lambda name, content: f"{name or '팀원'} 님이 {content} 확인하셨습니다.")

    def add(content, score, assignee="UASSIGNEE", page_ids=()):
        scores[content] = score
        docs = [{"page_id": p, "title": f"문서 {p}", "url": ""} for p in page_ids]
        return backlog.add("UREPORTER", "정문기", assignee, "담당", content, docs, {})
    return add


def test_parse_command():
    assert backlog.parse_command("<@U123ABC> 슈퍼아머 연출 반영 안 됨") == ("U123ABC", "슈퍼아머 연출 반영 안 됨")
    assert backlog.parse_command("<@U123ABC|moon> 내용") == ("U123ABC", "내용")
    assert backlog.parse_command("@정문기 내용") is None
    assert backlog.parse_command("<@U123ABC>") is None


def test_page_ids_in_text_reads_slack_links():
    text = ("답변\n• *참고 문서:* <https://app.notion.com/p/UI-371107d6982c80948620e95820d7fa8e|🔗 UI>, "
            "<https://app.notion.com/p/33f107d6982c803ea10dff47449e48b1|🔗 맵>")
    assert backlog.page_ids_in_text(text) == ["371107d6-982c-8094-8620-e95820d7fa8e", "33f107d6-982c-803e-a10d-ff47449e48b1"]


def test_notes_follow_link_and_similarity_rules(store):
    linked, unlinked = backlog.LINKED_MIN_SCORE, backlog.UNLINKED_MIN_SCORE
    store("연결됨+약한 관련", linked + 0.02, page_ids=["P1"])
    store("연결됨+무관", linked - 0.05, page_ids=["P1"])
    store("미연결+강한 관련", unlinked + 0.10)
    store("미연결+약한 관련", unlinked - 0.03)
    store("다른 문서에 연결+약한 관련", unlinked - 0.03, page_ids=["P9"])
    notes = backlog.notes_for_answer(QUERY, ["P1"])
    assert notes == ["정문기 님이 미연결+강한 관련 확인하셨습니다.", "정문기 님이 연결됨+약한 관련 확인하셨습니다."]


def test_resolved_backlog_is_no_longer_shown(store):
    record = store("슈퍼아머 연출", 0.9)
    assert backlog.resolve(record["backlog_id"], "UASSIGNEE") == "resolved"
    assert backlog.notes_for_answer(QUERY, []) == []
    assert backlog.resolve(record["backlog_id"], "UASSIGNEE") == "already"


def test_only_assignee_can_resolve(store):
    record = store("슈퍼아머 연출", 0.9)
    assert backlog.resolve(record["backlog_id"], "UREPORTER") == "not_assignee"
    assert backlog.get(record["backlog_id"])["status"] == backlog.OPEN
    assert backlog.resolve("00000000-0000-0000-0000-000000000000", "UASSIGNEE") == "not_found"


def test_open_by_assignee_groups_open_items(store):
    a = store("첫 번째", 0.1, assignee="UA")
    store("두 번째", 0.1, assignee="UA")
    done = store("해결된 것", 0.1, assignee="UB")
    backlog.resolve(done["backlog_id"], "UB")
    grouped = backlog.open_by_assignee()
    assert set(grouped) == {"UA"}
    assert [r["content"] for r in grouped["UA"]] == ["첫 번째", "두 번째"]
    assert grouped["UA"][0]["backlog_id"] == a["backlog_id"]


def test_backlog_survives_document_resync_and_delete(store, local_db):
    store("기획서와 무관하게 남아야 함", 0.5, page_ids=["P1"])
    indexing.delete_document_from_db("P1")
    assert len(backlog.open_by_assignee()["UASSIGNEE"]) == 1


def test_answer_gets_notes_appended(monkeypatch):
    found = hit({"text": "본문", "title": "문서", "url": "https://notion/p", "page_id": "P1"})
    monkeypatch.setattr(clients, "get_embedding", lambda q: QUERY)
    monkeypatch.setattr(clients, "get_qdrant", lambda: FakeQdrant([found]))
    monkeypatch.setattr(clients, "get_openai", lambda: fake_openai("답변"))
    monkeypatch.setattr(backlog, "notes_for_answer", lambda vec, pages: ["정문기 님이 A를 확인하셨습니다."] if pages == ["P1"] else [])
    answer = ai_engine.ask_ai_with_history("질문", [])["AI_답변"]
    assert answer.startswith("답변\n\n📌 참고 : 정문기 님이 A를 확인하셨습니다.")
    assert answer.endswith("[문서](https://notion/p)")


def test_answer_survives_backlog_failure(monkeypatch):
    monkeypatch.setattr(backlog, "notes_for_answer", raiser(RuntimeError("backlog down")))
    found = hit({"text": "본문", "title": "문서", "url": "", "page_id": "P1"})
    monkeypatch.setattr(clients, "get_embedding", lambda q: QUERY)
    monkeypatch.setattr(clients, "get_qdrant", lambda: FakeQdrant([found]))
    monkeypatch.setattr(clients, "get_openai", lambda: fake_openai("답변"))
    result = ai_engine.ask_ai_with_history("질문", [])
    assert result["status"] == "success" and result["AI_답변"] == "답변"


def fake_note_openai(body):
    return lambda: fake_openai(body)


CONTENT = "보스의 슈퍼아머 파괴 연출 도중 쉐이더 이펙트를 쓰기로 했는데 챕터 3 기획서에 반영되지 않은 것을 발견함"


def test_write_note_uses_generated_sentence(monkeypatch):
    monkeypatch.setattr(clients, "get_openai", fake_note_openai(
        "보스의 슈퍼아머 파괴 연출 도중 쉐이더 이펙트를 쓰기로 했는데 챕터 3 기획서에 반영되지 않은 것을 확인하셨습니다."))
    assert backlog.write_note("정문기", CONTENT).startswith("정문기 님이 보스의 슈퍼아머")


@pytest.mark.parametrize("body", [
    "보스 연출에 쉐이더가 빠진 것을 확인하셨습니다.",  # 내용이 빠짐
    "보스의 슈퍼아머 파괴 연출 도중 쉐이더 이펙트를 쓰기로 했는데 챕터 3 기획서에 반영되지 않은 것을 발견하셨습니다. 확인하셨습니다.",  # 어미 중복
    "보스의 슈퍼아머 파괴 연출 도중 쉐이더 이펙트를 쓰기로 했는데 챕터 3 기획서에 반영되지 않았습니다.",  # 형식 틀림
])
def test_write_note_rejects_bad_sentences(monkeypatch, body):
    monkeypatch.setattr(clients, "get_openai", fake_note_openai(body))
    assert backlog.write_note("정문기", CONTENT) == f"정문기 님이 남긴 메모: {CONTENT}"


def test_write_note_falls_back_when_openai_fails(monkeypatch):
    def broken():
        raise RuntimeError("no openai")
    monkeypatch.setattr(clients, "get_openai", broken)
    assert backlog.write_note("정문기", "쉐이더 미반영") == "정문기 님이 남긴 메모: 쉐이더 미반영"
    assert backlog.write_note(None, "쉐이더 미반영") == "팀원이 남긴 메모: 쉐이더 미반영"


@pytest.mark.parametrize("now,expected_hours", [
    (datetime(2026, 10, 13, 9, 0, tzinfo=backlog.KST), 1),
    (datetime(2026, 10, 13, 10, 0, tzinfo=backlog.KST), 24),
    (datetime(2026, 10, 13, 18, 0, tzinfo=backlog.KST), 16),
])
def test_seconds_until_next(now, expected_hours):
    assert backlog.seconds_until_next(10, now) == expected_hours * 3600


# --- 슬랙 연동 ---

@pytest.mark.parametrize("text,expected", [
    ("<@UBOT> 백로그 <@U1> 내용", "<@U1> 내용"),
    ("백로그 <@U1> 내용", "<@U1> 내용"),
    ("<@UBOT> 보스 패턴 알려줘", None),
    ("<@UBOT> 백로그", ""),
])
def test_backlog_request_text(text, expected):
    assert slack_service.backlog_request_text(text) == expected


class FakeSlack:
    def __init__(self, messages=(), names=None):
        self.messages = list(messages)
        self.names = names or {}
        self.updates, self.ephemerals, self.posts = [], [], []

    def conversations_replies(self, **kwargs):
        return {"messages": self.messages}

    def users_info(self, user):
        if user not in self.names:
            raise RuntimeError("missing_scope")
        return {"user": {"profile": {"display_name": self.names[user]}}}

    def chat_update(self, **kwargs):
        self.updates.append(kwargs)

    def chat_postEphemeral(self, **kwargs):
        self.ephemerals.append(kwargs)

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)


def test_mention_in_answer_thread_links_answered_docs(monkeypatch):
    slack_service._user_names.clear()
    captured = {}
    monkeypatch.setattr(backlog, "linked_docs", lambda ids: [{"page_id": i, "title": "보스 기획서", "url": "https://n/p"} for i in ids])
    monkeypatch.setattr(backlog, "add", lambda **kw: captured.update(kw) or {**kw, "note": "정문기 님이 … 확인하셨습니다."})
    thread = [
        {"ts": "1.0", "user": "UQ", "text": "<@UBOT> 보스 슈퍼아머?"},
        {"ts": "2.0", "bot_id": "B1", "text": "답변\n<https://app.notion.com/p/371107d6982c80948620e95820d7fa8e|🔗 보스>"},
        {"ts": "3.0", "user": "UREPORTER", "text": "<@UBOT> 백로그 <@UASSIGNEE> 쉐이더 미반영"},
    ]
    client = FakeSlack(thread, names={"UREPORTER": "정문기"})
    said = []
    event = {"channel": "C1", "thread_ts": "1.0", "ts": "3.0", "user": "UREPORTER", "text": thread[-1]["text"]}
    slack_service.handle_backlog_mention(client, lambda text, thread_ts=None: said.append((text, thread_ts)), event,
                                         slack_service.backlog_request_text(event["text"]))
    assert captured["assignee_id"] == "UASSIGNEE" and captured["content"] == "쉐이더 미반영"
    assert captured["reporter_name"] == "정문기" and captured["assignee_name"] is None
    assert [d["page_id"] for d in captured["docs"]] == ["371107d6-982c-8094-8620-e95820d7fa8e"]
    text, thread_ts = said[-1]
    assert thread_ts == "1.0" and text.startswith(slack_service.BACKLOG_CONFIRM_PREFIX) and "보스 기획서" in text


def test_bad_backlog_command_shows_usage():
    said = []
    slack_service.register_backlog(FakeSlack(), lambda text, thread_ts=None: said.append(text), "@정문기 내용", "U1", None)
    assert "@멘션" in said[-1]


def test_reminder_messages_split_and_include_buttons():
    records = [{"backlog_id": f"id{i}", "content": f"내용{i}", "reporter_name": "정문기", "created_at": "2026-10-13T10:00:00+09:00", "docs": []}
               for i in range(25)]
    messages = slack_service.reminder_messages(records)
    assert len(messages) == 2 and all(len(m) <= 50 for m in messages)
    actions = [b for m in messages for b in m if b["type"] == "actions"]
    assert len(actions) == 25
    assert {e["action_id"] for e in actions[0]["elements"]} == {"backlog_resolve", "backlog_pending"}


def test_resolve_button_by_assignee_updates_message(monkeypatch):
    monkeypatch.setattr(backlog, "resolve", lambda bid, uid: "resolved" if uid == "UA" else "not_assignee")
    blocks = slack_service.reminder_item_blocks({"backlog_id": "B1", "content": "c", "created_at": "2026-10-13", "docs": []})
    body = {"user": {"id": "UA"}, "actions": [{"value": "B1"}], "channel": {"id": "D1"}, "message": {"ts": "9.0", "blocks": blocks, "text": "t"}}
    client = FakeSlack()
    slack_service.handle_backlog_button(body, client, resolve=True)
    new_blocks = client.updates[-1]["blocks"]
    assert not any(b["type"] == "actions" for b in new_blocks)
    assert "해결됨으로 처리" in new_blocks[-1]["elements"][0]["text"]


def test_resolve_button_by_someone_else_is_refused(monkeypatch):
    monkeypatch.setattr(backlog, "resolve", lambda bid, uid: "resolved" if uid == "UA" else "not_assignee")
    body = {"user": {"id": "UX"}, "actions": [{"value": "B1"}], "channel": {"id": "D1"}, "message": {"ts": "9.0", "blocks": [], "text": "t"}}
    client = FakeSlack()
    slack_service.handle_backlog_button(body, client, resolve=True)
    assert client.updates == [] and "담당자만" in client.ephemerals[-1]["text"]


def test_send_reminders_dms_each_assignee(monkeypatch):
    record = {"backlog_id": "B1", "content": "c", "reporter_name": None, "created_at": "2026-10-13", "docs": []}
    monkeypatch.setattr(backlog, "open_by_assignee", lambda: {"UA": [record], "UB": [record, {**record, "backlog_id": "B2"}]})
    client = FakeSlack()
    slack_service.send_backlog_reminders(client)
    assert [p["channel"] for p in client.posts] == ["UA", "UB"]
