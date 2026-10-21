import json
import pytest
from qdrant_client.models import PointStruct
import backlog
import backlog_slack
import clients
import collector
import config
import indexing
import notion_writer
import proposal_slack
import review

VECTOR = [1.0] + [0.0] * (config.EMBEDDING_DIM - 1)
TARGET = {"page_id": "P1", "title": "디고리", "url": "https://notion/P1"}
DECISION = {"type": "decision", "title": "결정이 필요함: 잔불 강타 위치", "problem": "문서와 DB가 다름",
            "proposal": "- 잔불 강타는 파트너 스킬로 한다", "related_docs": ["전투 요소"]}
WRITING = {"type": "writing", "title": "작성이 필요함: 챕터 1 적 총정리", "problem": "흩어져 있음",
           "proposal": "## 일반몹\n- 석화체 (참고: 챕터 1 몹)", "related_docs": [], "new_doc_title": "챕터 1 적 총정리"}


@pytest.fixture
def store(local_db, monkeypatch):
    monkeypatch.setattr(clients, "get_embedding", lambda text: VECTOR)
    return local_db


class FakeSlack:
    """보낸 메세지/업데이트/모달을 기록하는 슬랙 클라이언트"""
    def __init__(self):
        self.posts, self.updates, self.ephemerals, self.views = [], [], [], []
        self.thread = []

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"channel": kwargs["channel"], "ts": f"{len(self.posts)}.0"}

    def chat_update(self, **kwargs):
        self.updates.append(kwargs)

    def chat_postEphemeral(self, **kwargs):
        self.ephemerals.append(kwargs)

    def views_open(self, **kwargs):
        self.views.append(kwargs["view"])

    def conversations_replies(self, **kwargs):
        return {"messages": self.thread}

    def users_info(self, user):
        return {"user": {"real_name": f"이름-{user}", "profile": {"email": f"{user}@game.com"}}}

    def users_lookupByEmail(self, email):
        return {"user": {"id": email.split("@")[0].upper()}}


def button_body(record, action_id, user="U1"):
    blocks = proposal_slack.item_blocks(record)
    return {"user": {"id": user}, "channel": {"id": "D1"}, "trigger_id": "T",
            "actions": [{"action_id": action_id, "value": record["backlog_id"]}],
            "message": {"ts": "5.0", "thread_ts": "1.0", "text": record["content"], "blocks": blocks}}


def add(finding=DECISION, assignee="U1"):
    return backlog.add_proposal(finding, TARGET, [{"page_id": "P2", "title": "전투 요소", "url": "https://notion/P2"}],
                                assignee, "담당", "r1")


# --- 노션 쓰기 ---
def test_text_to_blocks_reads_simple_markdown():
    blocks = notion_writer.text_to_blocks("## 소제목\n\n- 목록\n1. 번호\n문단")
    assert [b["type"] for b in blocks] == ["heading_2", "bulleted_list_item", "numbered_list_item", "paragraph"]
    assert blocks[2]["numbered_list_item"]["rich_text"][0]["text"]["content"] == "번호"


def test_long_text_is_split_into_rich_text_pieces():
    assert len(notion_writer.rich_text("가" * 4500)) == 3


@pytest.fixture
def notion_calls(monkeypatch):
    calls = []

    def request(method, path, body=None):
        calls.append((method, path, body))
        return {"id": "NEW", "url": "https://notion/NEW"}
    monkeypatch.setattr(notion_writer, "_request", request)
    return calls


def test_append_decision_adds_heading_once(notion_calls, monkeypatch):
    monkeypatch.setattr(notion_writer, "has_decision_heading", lambda page_id: False)
    notion_writer.append_decision("P1", "결정이 필요함: X", "- 이렇게 한다", "정문기")
    (method, path, body), = notion_calls
    assert (method, path) == ("PATCH", "blocks/P1/children")
    kinds = [b["type"] for b in body["children"]]
    assert kinds == ["heading_2", "paragraph", "bulleted_list_item"]
    title, stamp = body["children"][1]["paragraph"]["rich_text"]
    # 결정 기록에는 승인된 결정만 들어가므로 '결정이 필요함:'을 뗀다
    assert title["text"]["content"] == "X"
    assert "승인: 정문기" in stamp["text"]["content"]

    monkeypatch.setattr(notion_writer, "has_decision_heading", lambda page_id: True)
    notion_writer.append_decision("P1", "결정이 필요함: Y", "내용", None)
    assert notion_calls[1][2]["children"][0]["type"] == "paragraph"


def test_create_proposal_page_sets_assignee(notion_calls):
    page = notion_writer.create_proposal_page("총정리", "## 개요\n내용", "N-USER")
    method, path, body = notion_calls[0]
    assert (method, path, page["page_id"]) == ("POST", "pages", "NEW")
    assert body["parent"] == {"database_id": config.PROPOSAL_DATABASE_ID}
    assert body["properties"]["예상 담당자"] == {"people": [{"id": "N-USER"}]}


# --- 검토 결과 읽기 ---
def test_parse_findings_keeps_valid_items():
    text = "검토했습니다.\n" + json.dumps({"findings": [
        DECISION, {"type": "writing", "title": "작성이 필요함: 총정리", "proposal": "초안"},
        {"type": "unknown", "title": "x", "proposal": "y"}, {"type": "decision", "title": "제안 없음"}]}, ensure_ascii=False)
    findings = review.parse_findings(text)
    assert [f["title"] for f in findings] == [DECISION["title"], "작성이 필요함: 총정리"]
    assert findings[1]["new_doc_title"] == "총정리"
    assert review.parse_findings("JSON 아님") == []


def test_earlier_proposals_show_status_and_reason(store):
    record = add()
    backlog.close(record["backlog_id"], "U1", backlog.REJECTED, reject_reason="폐기된 콘텐츠")
    assert review.earlier_proposals("P1") == f"- [반려됨] {DECISION['title']} (사유: 폐기된 콘텐츠)"
    assert review.earlier_proposals("P9") == "(없음)"


# --- 수집가 작업: 정리 후 검토 ---
def test_enqueue_keeps_requester_when_resynced_without_one(store):
    collector.enqueue("P1", "N1")
    collector.enqueue("P1")
    assert collector.pending_jobs()[0]["requested_by"] == "N1"


def test_review_runs_once_per_original_and_sends_proposals(store, monkeypatch):
    store.upsert(config.REFINED_COLLECTION, [PointStruct(id=collector.refined_point_id("P1", 0), vector=VECTOR, payload={
        "page_id": "P1", "title": "디고리", "url": "https://notion/P1", "text": "문서: 디고리\n## 개요", "chunk_idx": 0,
        "source_hash": "h1"})])
    reviews, slack = [], FakeSlack()
    monkeypatch.setattr(review, "review_document", lambda page_id, title: reviews.append(page_id) or [DECISION, WRITING])
    monkeypatch.setattr(review, "related_docs", lambda names: [])
    monkeypatch.setattr(proposal_slack, "web_client", lambda: slack)
    monkeypatch.setattr(notion_writer, "user_info", lambda notion_id: {"email": "u7@game.com", "name": "칠번"})

    records = collector.review_and_notify("P1", "N7")
    assert [r["assignee_id"] for r in records] == ["U7", "U7"]
    assert [p["channel"] for p in slack.posts] == ["U7", "U7", "U7"]
    assert all(p.get("thread_ts") == "1.0" for p in slack.posts[1:])
    assert collector.review_and_notify("P1", "N7") == []
    assert reviews == ["P1"]


def test_item_shows_assumptions_before_proposal(store):
    blocks = proposal_slack.item_blocks(add({**DECISION, "assumptions": "쿨타임 3초는 새로 정함"}))
    assert blocks[1]["text"]["text"].startswith("⚠️ *근거 없이 정한 부분:* 쿨타임 3초는 새로 정함\n💡")
    assert "근거 없이" not in proposal_slack.item_blocks(add())[1]["text"]["text"]


def test_slack_user_is_found_by_name_when_emails_differ(monkeypatch):
    monkeypatch.setattr(notion_writer, "user_info", lambda notion_id: {"email": "me@company.com", "name": "정 문기"})

    class Slack(FakeSlack):
        def users_lookupByEmail(self, email):
            raise RuntimeError("users_not_found")

        def users_list(self, limit):
            return [{"members": [{"id": "U9", "real_name": "정문기", "profile": {}},
                                 {"id": "U8", "real_name": "정진섭", "profile": {}},
                                 {"id": "B1", "real_name": "정문기", "is_bot": True, "profile": {}}]}]
    assert proposal_slack.slack_user_for_notion(Slack(), "N1") == "U9"


def test_review_goes_to_report_user_when_requester_is_unknown(store, monkeypatch):
    """대신 받은 사람이 담당자가 되어 버튼을 누를 수 있어야 한다"""
    monkeypatch.setattr(config, "DIAGNOSIS_REPORT_USER_ID", "UADMIN")
    store.upsert(config.REFINED_COLLECTION, [PointStruct(id=collector.refined_point_id("P1", 0), vector=VECTOR, payload={
        "page_id": "P1", "title": "디고리", "text": "문서: 디고리\n## 개요", "chunk_idx": 0, "source_hash": "h1"})])
    slack = FakeSlack()
    monkeypatch.setattr(review, "review_document", lambda page_id, title: [DECISION])
    monkeypatch.setattr(review, "related_docs", lambda names: [])
    monkeypatch.setattr(proposal_slack, "web_client", lambda: slack)
    monkeypatch.setattr(notion_writer, "user_info", lambda notion_id: {})
    records = collector.review_and_notify("P1", "N-unknown")
    assert records[0]["assignee_id"] == "UADMIN"
    assert slack.posts[0]["channel"] == "UADMIN"
    assert "찾지 못해" in slack.posts[0]["text"]


# --- 슬랙 버튼 ---
@pytest.fixture
def notion_fakes(monkeypatch):
    written, synced = [], []
    monkeypatch.setattr(notion_writer, "append_decision", lambda *args: written.append(("decision",) + args))
    monkeypatch.setattr(notion_writer, "create_proposal_page",
                        lambda *args: written.append(("page",) + args) or {"page_id": "NEW", "url": "https://notion/NEW"})
    monkeypatch.setattr(notion_writer, "user_id_by", lambda email, names: f"N-{email}")
    monkeypatch.setattr(indexing, "sync_document_to_db", lambda page_id, token, requested_by=None: synced.append((page_id, requested_by)))

    class NowThread:
        def __init__(self, target, args, kwargs, **_):
            self.run = lambda: target(*args, **kwargs)

        def start(self):
            self.run()
    monkeypatch.setattr(proposal_slack.threading, "Thread", NowThread)
    return written, synced


def test_approve_appends_decision_and_resyncs(store, notion_fakes):
    written, synced = notion_fakes
    record, slack = add(), FakeSlack()
    proposal_slack.handle_approve(button_body(record, "proposal_approve"), slack)
    assert written == [("decision", "P1", DECISION["title"], DECISION["proposal"], "이름-U1")]
    assert synced == [("P1", "N-U1@game.com")]
    saved = backlog.get(record["backlog_id"])
    assert (saved["status"], saved["approved_text"], saved["notion_url"]) == (backlog.RESOLVED, DECISION["proposal"], "https://notion/P1")
    status = slack.updates[0]["blocks"][-1]
    assert status["type"] == "context" and "통과" in status["elements"][0]["text"]


def test_edit_then_submit_creates_page_with_edited_text(store, notion_fakes):
    written, synced = notion_fakes
    record, slack = add(WRITING), FakeSlack()
    body = button_body(record, "proposal_edit")
    proposal_slack.handle_edit(body, slack)
    view = slack.views[0]
    assert view["blocks"][1]["element"]["initial_value"] == WRITING["proposal"]

    slack.thread = [{"ts": "1.0", "text": "머리"}, body["message"]]
    view["state"] = {"values": {"text": {"value": {"value": "## 고친 초안"}}}}
    proposal_slack.handle_edit_submit({"user": {"id": "U1"}, "view": view}, slack)
    assert written == [("page", "챕터 1 적 총정리", "## 고친 초안", "N-U1@game.com")]
    assert synced == [("NEW", "N-U1@game.com")]
    assert backlog.get(record["backlog_id"])["approved_text"] == "## 고친 초안"
    assert slack.updates[0]["ts"] == "5.0"


def test_hold_and_reject_close_without_writing(store, notion_fakes):
    written, _ = notion_fakes
    held, rejected, slack = add(), add(), FakeSlack()
    proposal_slack.handle_hold(button_body(held, "proposal_hold"), slack)
    assert backlog.get(held["backlog_id"])["status"] == backlog.HELD

    body = button_body(rejected, "proposal_reject")
    proposal_slack.handle_reject(body, slack)
    view = slack.views[0]
    view["state"] = {"values": {"text": {"value": {"value": "폐기된 콘텐츠"}}}}
    slack.thread = [body["message"]]
    proposal_slack.handle_reject_submit({"user": {"id": "U1"}, "view": view}, slack)
    saved = backlog.get(rejected["backlog_id"])
    assert (saved["status"], saved["reject_reason"]) == (backlog.REJECTED, "폐기된 콘텐츠")
    assert written == []


def test_only_assignee_can_act_and_only_once(store, notion_fakes):
    written, _ = notion_fakes
    record, slack = add(), FakeSlack()
    proposal_slack.handle_approve(button_body(record, "proposal_approve", user="U2"), slack)
    assert "담당자만" in slack.ephemerals[0]["text"]
    proposal_slack.handle_hold(button_body(record, "proposal_hold"), slack)
    proposal_slack.handle_approve(button_body(record, "proposal_approve"), slack)
    assert "이미 처리된" in slack.ephemerals[1]["text"]
    assert written == []


def test_notion_failure_keeps_proposal_open(store, monkeypatch):
    def fail(*args):
        raise RuntimeError("노션 API 실패 (403)")
    monkeypatch.setattr(notion_writer, "append_decision", fail)
    monkeypatch.setattr(notion_writer, "user_id_by", lambda email, names: None)
    record, slack = add(), FakeSlack()
    proposal_slack.handle_approve(button_body(record, "proposal_approve"), slack)
    assert backlog.get(record["backlog_id"])["status"] == backlog.OPEN
    assert "반영하지 못했어요" in slack.ephemerals[0]["text"]


def test_reminders_show_proposal_buttons_within_block_limit(store):
    records = [add() for _ in range(20)]
    messages = backlog_slack.reminder_messages(records)
    assert all(len(blocks) <= backlog_slack.MAX_BLOCKS_PER_MESSAGE for blocks in messages)
    assert sum(1 for blocks in messages for b in blocks if b["type"] == "actions") == 20
    actions = next(b for b in messages[0] if b["type"] == "actions")
    assert actions["elements"][0]["action_id"] == "proposal_approve"
