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


def test_image_filename_carries_block_id():
    name = slack_service.image_filename({"caption": "도판", "block_id": "2da107d6-982c-8023-9b15-d9d579e43695"})
    assert slack_service.BLOCK_ID_PATTERN.findall(name) == ["2da107d6-982c-8023-9b15-d9d579e43695"]


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
