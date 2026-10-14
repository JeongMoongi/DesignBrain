import pytest
from fastapi.testclient import TestClient
import ai_engine
import config
import main
import slack_service


@pytest.mark.parametrize("text,expected", [
    ("도판 ![캐릭터](https://s3/a.png?x=1) 끝", "도판  끝"),
    ("참고: [영웅 문서](https://notion.so/abc)입니다", "참고: <https://notion.so/abc|🔗 영웅 문서>입니다"),
    ("([문서](https://notion.so/abc))", "(<https://notion.so/abc|🔗 문서>)"),
    ("**굵게**", "*굵게*"),
    ("- 참고 문서 : [a](https://x.com)", "• *참고 문서:* <https://x.com|🔗 a>"),
])
def test_format_markdown_for_slack(text, expected):
    assert slack_service.format_markdown_for_slack(text) == expected


def test_thread_reply_uses_parent_thread_history(monkeypatch):
    # DM 메인 창의 질문(ts=100) → 봇 답변 스레드 안의 후속 질문(ts=300, thread_ts=100)
    thread = [{"ts": "100", "user": "U", "text": "석화체에 대해 알려줘"},
              {"ts": "200", "bot_id": "B", "text": "석화체는 이착륙 몹입니다."},
              {"ts": "300", "user": "U", "text": "패턴은 뭐가 있어?"}]

    class ThreadSlack:
        def chat_update(self, **kwargs):
            pass

        def conversations_replies(self, channel, ts, **kwargs):
            return {"messages": thread if ts == "100" else [m for m in thread if m["ts"] == ts]}

    received = {}
    def fake_ask(query, history):
        received.update(query=query, history=history)
        return {"status": "success", "AI_답변": "답변", "page_ids": [], "images": []}
    monkeypatch.setattr(ai_engine, "ask_ai_with_history", fake_ask)

    event = {"ts": "300", "thread_ts": "100", "channel": "D1", "text": "패턴은 뭐가 있어?"}
    thread_ts = slack_service.event_thread_ts(event)
    assert thread_ts == "100"
    slack_service.process_chat_and_reply(ThreadSlack(), lambda text, thread_ts=None: {"ts": "400"}, "D1", thread_ts, event["text"])
    assert received["query"] == "패턴은 뭐가 있어?"
    assert received["history"] == [{"role": "user", "content": "석화체에 대해 알려줘"},
                                   {"role": "assistant", "content": "석화체는 이착륙 몹입니다."}]


class AnswerSlack:
    """이전 답변(도판 b1을 첨부)이 있는 스레드"""
    def __init__(self, fail_with_files=False):
        self.updates, self.uploads, self.fail_with_files = [], [], fail_with_files

    def conversations_replies(self, channel, ts, include_all_metadata=False):
        previous = {"ts": "2", "bot_id": "B", "text": "이전 답변"}
        if include_all_metadata:
            previous["metadata"] = {"event_type": slack_service.ANSWER_EVENT_TYPE,
                                    "event_payload": {"image_block_ids": ["b1"]}}
        return {"messages": [{"ts": "1", "user": "U", "text": "석화체 알려줘"}, previous,
                             {"ts": "3", "user": "U", "text": "패턴은?"}]}

    def files_upload_v2(self, **kwargs):
        self.uploads.append(kwargs)
        return {"file": {"id": f"F{len(self.uploads)}"}}

    def chat_update(self, **kwargs):
        if self.fail_with_files and kwargs.get("file_ids"):
            raise RuntimeError("invalid file")
        self.updates.append(kwargs)


@pytest.fixture
def answer_with_images(monkeypatch):
    monkeypatch.setattr(ai_engine, "ask_ai_with_history", lambda q, h: {
        "status": "success", "AI_답변": "**답변**", "page_ids": [],
        "images": [{"block_id": "b1", "caption": "도판"}, {"block_id": "b2", "caption": "도판"}, {"block_id": "b3", "caption": "도판"}]})
    monkeypatch.setattr(config, "NOTION_INTEGRATION_TOKEN", "tok")
    monkeypatch.setattr(slack_service.notion_parser, "fetch_image_by_block_id",
                        lambda block_id, token: {"block_id": block_id, "caption": "도판", "url": f"https://s3/{block_id}"})
    monkeypatch.setattr(slack_service.requests, "get",
                        lambda url, timeout: type("R", (), {"status_code": 200, "content": b"img"})())


def run_reply(slack):
    said = []
    def say(text, thread_ts=None):
        said.append(text)
        return {"ts": "4"}
    slack_service.process_chat_and_reply(slack, say, "D1", "1", "패턴은?")
    return said


def test_answer_and_new_images_arrive_in_one_message(answer_with_images):
    slack = AnswerSlack()
    said = run_reply(slack)
    # 새 메세지는 '분석 중' 하나뿐이고, 그 메세지가 답변 + 도판으로 바뀐다
    assert len(said) == 1
    update = slack.updates[-1]
    assert update["ts"] == "4" and update["text"] == "*답변*"
    # 이미 보여준 b1은 건너뛰고 새 도판 2장만 붙인다
    assert update["file_ids"] == ["F1", "F2"]
    assert update["metadata"]["event_payload"]["image_block_ids"] == ["b2", "b3"]
    assert all("channel" not in u for u in slack.uploads)


def test_answer_is_sent_even_if_attaching_images_fails(answer_with_images):
    slack = AnswerSlack(fail_with_files=True)
    run_reply(slack)
    assert slack.updates == [{"channel": "D1", "ts": "4", "text": "*답변*"}]


def test_thread_say_posts_directly_without_metadata():
    # Bolt의 say는 스레드 첫 봇 답변의 메타데이터(도판 기록)를 어시스턴트 정보로 읽다 실패하므로 직접 보낸다
    posted = []
    client = type("Client", (), {"chat_postMessage": lambda self, **kw: posted.append(kw) or {"ts": "9"}})()
    say = slack_service.thread_say(client, "D1")
    assert say(text="분석 중", thread_ts="1") == {"ts": "9"}
    assert posted == [{"channel": "D1", "text": "분석 중", "thread_ts": "1"}]


def test_top_level_message_starts_new_thread():
    assert slack_service.event_thread_ts({"ts": "100", "channel": "D1", "text": "석화체"}) == "100"


@pytest.fixture
def app_client(monkeypatch):
    calls = []
    monkeypatch.setattr(slack_service, "start_slack_bot", lambda: None)
    monkeypatch.setattr(slack_service, "stop_slack_bot", lambda: None)
    monkeypatch.setattr(ai_engine, "get_qdrant", lambda: None)
    monkeypatch.setattr(ai_engine, "close_clients", lambda: None)
    monkeypatch.setattr(ai_engine, "sync_document_to_db", lambda pid, tok: calls.append(("sync", pid)))
    monkeypatch.setattr(ai_engine, "delete_document_from_db", lambda pid: calls.append(("delete", pid)))
    with TestClient(main.app) as client:
        yield client, calls


def test_webhook_rejects_missing_or_wrong_secret(app_client, monkeypatch):
    client, calls = app_client
    monkeypatch.setattr(config, "NOTION_WEBHOOK_SECRET", "s3cr3t")
    body = {"data": {"id": "p1"}}
    assert client.post("/webhook/notion", json=body).status_code == 401
    assert client.post("/webhook/notion/delete", json=body, headers={"X-Webhook-Secret": "wrong"}).status_code == 401
    assert calls == []


def test_webhook_accepts_correct_secret(app_client, monkeypatch):
    client, calls = app_client
    monkeypatch.setattr(config, "NOTION_WEBHOOK_SECRET", "s3cr3t")
    headers = {"X-Webhook-Secret": "s3cr3t"}
    assert client.post("/webhook/notion", json={"data": {"id": "p1"}}, headers=headers).status_code == 200
    assert client.post("/webhook/notion/delete", json={"source": {"id": "p2"}}, headers=headers).status_code == 200
    assert calls == [("sync", "p1"), ("delete", "p2")]
