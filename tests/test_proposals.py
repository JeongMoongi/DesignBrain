import json
import pytest
from qdrant_client.models import PointStruct
import backlog
import backlog_slack
import clients
import librarian
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


def test_parse_findings_cleans_summary_and_category():
    text = json.dumps({"findings": [
        {**DECISION, "category": "conflict", "summary": "잔불 강타 위치가\n문서마다 달라요: " + "가" * 200},
        {**DECISION, "category": "이상한값"}, {**WRITING}]}, ensure_ascii=False)
    conflict, unknown, writing = review.parse_findings(text)
    assert "\n" not in conflict["summary"] and len(conflict["summary"]) == review.SUMMARY_CHARS
    assert conflict["summary"].startswith("잔불 강타 위치가 문서마다 달라요")
    assert (unknown["category"], unknown["summary"]) == ("", "")
    assert writing["category"] == backlog.TOPIC                                       # 새 문서 제안은 흩어진 주제


def test_item_leads_with_one_line_summary(store):
    record = add({**DECISION, "category": "conflict", "summary": "잔불 강타 위치가 문서마다 달라요: 디고리 파트너 ↔ 전투 요소 리더",
                  "problem": "디고리는 파트너 스킬이라 하나 전투 요소는 리더 스킬이라 해서 구현이 갈린다."})
    head = proposal_slack.item_blocks(record)[0]["text"]["text"]
    assert head.startswith("*🔀 잔불 강타 위치가 문서마다 달라요: 디고리 파트너 ↔ 전투 요소 리더*\n디고리는 파트너 스킬이라")
    assert DECISION["title"] not in head
    old = proposal_slack.item_blocks(add())[0]["text"]["text"]                          # 요약이 없는 예전 제안은 제목 그대로
    assert old.startswith(f"*{DECISION['title']}*\n{DECISION['problem']}")


def test_review_head_lists_every_summary(store):
    slack = FakeSlack()
    records = [add({**DECISION, "category": "conflict", "summary": "잔불 강타 위치가 문서마다 달라요"}),
               add({**DECISION, "category": "missing", "summary": "잔불 강타 연출이 비어 있어요"}), add()]
    proposal_slack.send_review(slack, "U1", TARGET, records)
    head = slack.posts[0]["text"]
    assert "1. 🔀 잔불 강타 위치가 문서마다 달라요\n2. 🕳️ 잔불 강타 연출이 비어 있어요\n3. 결정이 필요함: 잔불 강타 위치\n" in head
    assert [p["text"] for p in slack.posts[1:]] == ["🔀 잔불 강타 위치가 문서마다 달라요", "🕳️ 잔불 강타 연출이 비어 있어요", DECISION["title"]]


def test_earlier_proposals_show_status_and_reason(store):
    record = add()
    backlog.close(record["backlog_id"], "U1", backlog.REJECTED, reject_reason="폐기된 콘텐츠")
    assert review.earlier_proposals("P1") == f"- [반려됨] {DECISION['title']} (사유: 폐기된 콘텐츠)"
    assert review.earlier_proposals("P9") == "(없음)"


# --- 사서 작업: 정리 후 검토 ---
def test_enqueue_keeps_requester_when_resynced_without_one(store):
    librarian.enqueue("P1", "N1")
    librarian.enqueue("P1")
    assert librarian.pending_jobs()[0]["requested_by"] == "N1"


def test_review_runs_once_per_original_and_sends_proposals(store, monkeypatch):
    store.upsert(config.REFINED_COLLECTION, [PointStruct(id=librarian.refined_point_id("P1", 0), vector=VECTOR, payload={
        "page_id": "P1", "title": "디고리", "url": "https://notion/P1", "text": "문서: 디고리\n## 개요", "chunk_idx": 0,
        "source_hash": "h1"})])
    reviews, slack = [], FakeSlack()
    monkeypatch.setattr(review, "review_document", lambda page_id, title: reviews.append(page_id) or [DECISION, WRITING])
    monkeypatch.setattr(review, "related_docs", lambda names: [])
    monkeypatch.setattr(proposal_slack, "web_client", lambda: slack)
    monkeypatch.setattr(notion_writer, "user_info", lambda notion_id: {"email": "u7@game.com", "name": "칠번"})

    records = librarian.review_and_notify("P1", "N7")
    assert [r["assignee_id"] for r in records] == ["U7", "U7"]
    assert [p["channel"] for p in slack.posts] == ["U7", "U7", "U7"]
    assert all(p.get("thread_ts") == "1.0" for p in slack.posts[1:])
    assert librarian.review_and_notify("P1", "N7") == []
    assert reviews == ["P1"]


def test_item_shows_assumptions_before_proposal(store):
    blocks = proposal_slack.item_blocks(add({**DECISION, "assumptions": "쿨타임 3초는 새로 정함"}))
    assert blocks[1]["text"]["text"].startswith("⚠️ *근거 없이 정한 부분:* 쿨타임 3초는 새로 정함\n💡")
    assert "근거 없이" not in proposal_slack.item_blocks(add())[1]["text"]["text"]


def test_unreadable_parts_are_sent_even_without_proposals():
    slack = FakeSlack()
    proposal_slack.send_review(slack, "U1", TARGET, [], unreadable="📎 *사서가 읽지 못한 부분* …")
    assert len(slack.posts) == 1
    assert slack.posts[0]["text"].startswith("🧹 <https://notion/P1|디고리> 을(를) 정리했어요.")
    assert "📎 *사서가 읽지 못한 부분*" in slack.posts[0]["text"]


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
    store.upsert(config.REFINED_COLLECTION, [PointStruct(id=librarian.refined_point_id("P1", 0), vector=VECTOR, payload={
        "page_id": "P1", "title": "디고리", "text": "문서: 디고리\n## 개요", "chunk_idx": 0, "source_hash": "h1"})])
    slack = FakeSlack()
    monkeypatch.setattr(review, "review_document", lambda page_id, title: [DECISION])
    monkeypatch.setattr(review, "related_docs", lambda names: [])
    monkeypatch.setattr(proposal_slack, "web_client", lambda: slack)
    monkeypatch.setattr(notion_writer, "user_info", lambda notion_id: {})
    records = librarian.review_and_notify("P1", "N-unknown")
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
    backlog.close(record["backlog_id"], "U1", backlog.REJECTED)
    proposal_slack.handle_approve(button_body(record, "proposal_approve"), slack)
    assert "이미 처리된" in slack.ephemerals[1]["text"]
    assert written == []


def test_hold_rests_then_comes_back_and_can_still_be_approved(store, notion_fakes):
    from datetime import datetime, timedelta
    written, _ = notion_fakes
    held, approved_later, slack = add(), add(), FakeSlack()
    proposal_slack.handle_hold(button_body(held, "proposal_hold"), slack)
    until = datetime.now(backlog.KST) + timedelta(days=backlog.HOLD_DAYS)
    assert f"{until:%Y-%m-%d}부터 다시 물어볼게요" in slack.updates[-1]["blocks"][-1]["elements"][0]["text"]

    # 보류 중에는 리마인더에서 빠지지만 답변 참고 문구에는 남는다
    assert held["backlog_id"] not in [r["backlog_id"] for rs in backlog.open_by_assignee().values() for r in rs]
    assert held["note"] in backlog.notes_for_answer(VECTOR, ["P1"])
    # 기간이 끝나면 다시 미해결로 돌아온다
    assert backlog.reopen_expired_holds(now=until - timedelta(days=1)) == 0
    assert backlog.reopen_expired_holds(now=until + timedelta(minutes=1)) == 1
    assert backlog.get(held["backlog_id"])["status"] == backlog.OPEN

    # 보류 중이어도 담당자가 마음을 바꿔 통과할 수 있다
    proposal_slack.handle_hold(button_body(approved_later, "proposal_hold"), slack)
    proposal_slack.handle_approve(button_body(approved_later, "proposal_approve"), slack)
    assert backlog.get(approved_later["backlog_id"])["status"] == backlog.RESOLVED and len(written) == 1


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


# --- 대화로 고치기 ---
def chat_event(text, thread_ts, user="U1"):
    return {"channel": "D1", "user": user, "text": text, "thread_ts": thread_ts, "ts": "9.0"}


def test_chat_opens_own_thread_and_revises_proposal(store, notion_fakes, monkeypatch):
    written, _ = notion_fakes
    record, slack = add(WRITING), FakeSlack()
    proposal_slack.handle_chat(button_body(record, "proposal_chat"), slack)
    head, current = slack.posts
    # 가짜 슬랙은 n번째 메세지의 ts를 'n.0'으로 준다
    assert head["text"].startswith("💬 *작성이 필요함: 챕터 1 적 총정리*") and current["thread_ts"] == "1.0"
    assert "대화 스레드에서" in slack.updates[0]["blocks"][-1]["elements"][0]["text"]

    calls = []
    monkeypatch.setattr(proposal_slack, "revise", lambda rec, message: calls.append((rec["proposal"], message)) or
                        {"reply": "보스를 뺐어요.", "proposal": "## 일반몹\n- 석화체", "assumptions": ""})
    assert proposal_slack.handle_chat_message(slack, chat_event("보스는 빼 줘", "1.0")) is True
    assert calls == [(WRITING["proposal"], "보스는 빼 줘")]
    saved = backlog.get(record["backlog_id"])
    assert saved["proposal"] == "## 일반몹\n- 석화체"
    assert saved["chat_history"] == [{"role": "기획자", "text": "보스는 빼 줘"}, {"role": "사서", "text": "보스를 뺐어요."}]
    revised = slack.updates[-1]["blocks"]
    assert revised[0]["text"]["text"] == "✏️ 보스를 뺐어요." and revised[-1]["type"] == "actions"

    # 고친 내용으로 통과된다
    proposal_slack.handle_approve(button_body(saved, "proposal_approve"), slack)
    assert written[0][2] == "## 일반몹\n- 석화체"


def test_chat_question_keeps_proposal(store, monkeypatch):
    record, slack = add(), FakeSlack()
    proposal_slack.handle_chat(button_body(record, "proposal_chat"), slack)
    monkeypatch.setattr(proposal_slack, "revise", lambda rec, message: {"reply": "근거는 전투 요소예요.", "proposal": "", "assumptions": ""})
    proposal_slack.handle_chat_message(slack, chat_event("근거가 뭐야?", "1.0"))
    assert backlog.get(record["backlog_id"])["proposal"] == DECISION["proposal"]
    assert slack.updates[-1]["text"] == "💬 근거는 전투 요소예요."


def test_other_dm_messages_are_not_chat(store):
    slack = FakeSlack()
    assert proposal_slack.handle_chat_message(slack, {"channel": "D1", "user": "U1", "text": "질문", "ts": "1.0"}) is False
    assert proposal_slack.handle_chat_message(slack, chat_event("질문", "777.0")) is False


def test_chat_on_closed_proposal_says_so(store):
    record, slack = add(), FakeSlack()
    proposal_slack.handle_chat(button_body(record, "proposal_chat"), slack)
    backlog.close(record["backlog_id"], "U1", backlog.HELD)
    assert proposal_slack.handle_chat_message(slack, chat_event("고쳐 줘", "1.0")) is True
    assert slack.posts[-1]["text"] == "이미 처리된 제안이에요."


# --- 리마인더: 사서 제안은 쌓인 것의 10%만 ---
def fake_record(no, kind=backlog.PROPOSAL, **fields):
    return {"backlog_id": f"b{no}", "kind": kind, "created_at": f"2026-10-{no:02d}", **fields}


@pytest.mark.parametrize("total, expected", [(1, 1), (9, 1), (25, 2), (80, 5)])
def test_reminder_asks_ten_percent_of_proposals_within_bounds(total, expected):
    people = [fake_record(99, kind=None)]
    chosen, proposal_total = backlog.reminder_selection(people + [fake_record(i) for i in range(1, total + 1)])
    assert proposal_total == total
    assert chosen[0]["backlog_id"] == "b99" and len(chosen) == 1 + expected


def test_reminder_prefers_recently_asked_about_then_rotates():
    from datetime import datetime
    now = datetime(2026, 10, 21, tzinfo=backlog.KST)
    records = [fake_record(1, last_reminded_at="2026-10-20"), fake_record(2), fake_record(3, last_reminded_at="2026-10-19"),
               fake_record(4, last_answer_at="2026-10-20T10:00:00+09:00", last_reminded_at="2026-10-20")] + \
              [fake_record(i, last_reminded_at="2026-10-20") for i in range(5, 21)]
    chosen, _ = backlog.reminder_selection(records, now=now)
    # 20건의 10% = 2건: 최근 답변에 나온 b4, 그다음 한 번도 묻지 않은 b2
    assert [r["backlog_id"] for r in chosen] == ["b4", "b2"]


def test_reminder_header_counts_people_and_proposals():
    records = [fake_record(1, kind=None), fake_record(2)]
    header = backlog_slack.reminder_header(records, proposal_total=12)
    assert "백로그가 *1건*" in header and "사서 제안 *12건* 중 오늘은 *1건*" in header


def test_send_reminders_marks_asked_proposals(store):
    for _ in range(3):
        add()
    slack = FakeSlack()
    backlog_slack.send_backlog_reminders(slack)
    reminded = [r for r in backlog.proposals_for_page("P1") if r.get("last_reminded_at")]
    assert len(reminded) == 1 and len(slack.posts) == 1


def test_answer_notes_remember_when_shown(store):
    record = add()
    backlog.notes_for_answer(VECTOR, ["P1"])
    assert backlog.get(record["backlog_id"]).get("last_answer_at")


def test_review_messages_remember_where_each_proposal_is(store):
    record, slack = add(), FakeSlack()
    proposal_slack.send_review(slack, "U1", TARGET, [record])
    saved = backlog.get(record["backlog_id"])
    assert (saved["slack_channel"], saved["slack_ts"]) == ("U1", "2.0")


# --- 문서가 갱신되면 미해결 제안 대조 ---
def test_recheck_closes_applied_proposals_and_flags_people_backlogs(store, monkeypatch):
    from qdrant_client.models import PointStruct
    from test_synthesis import install_responses
    applied, kept = add(), add({**DECISION, "title": "결정이 필요함: 다른 것"})
    backlog.update(applied["backlog_id"], slack_channel="U1", slack_ts="5.0")
    person = backlog.add("U2", "팀원", "U1", "담당", "디고리 수치 수정 필요", [TARGET], {})
    store.upsert(config.REFINED_COLLECTION, [PointStruct(id=1, vector=VECTOR, payload={
        "page_id": "P1", "chunk_idx": 0, "text": "문서: 디고리\n- 잔불 강타는 파트너 스킬", "title": "디고리"})])
    slack = FakeSlack()
    monkeypatch.setattr(proposal_slack, "web_client", lambda: slack)
    fake = install_responses(monkeypatch, {"results": [{"no": 1, "status": "applied", "reason": "본문에 반영됨"},
                                                       {"no": 2, "status": "keep", "reason": ""}]})
    closed = review.recheck_open("P1")
    assert "[갱신된 문서]\n- 잔불 강타는 파트너 스킬" in fake.requests[0]["input"]
    assert [r["backlog_id"] for r in closed] == [applied["backlog_id"]]
    saved = backlog.get(applied["backlog_id"])
    assert (saved["status"], saved["close_reason"]) == (backlog.APPLIED, "본문에 반영됨")
    assert backlog.get(kept["backlog_id"])["status"] == backlog.OPEN
    assert backlog.get(person["backlog_id"]).get("doc_updated_at")
    assert backlog.get(person["backlog_id"])["status"] == backlog.OPEN
    update = slack.updates[0]
    assert update["ts"] == "5.0" and "반영된 것으로 보고 닫았어요 (본문에 반영됨)" in update["blocks"][-1]["elements"][0]["text"]


def test_people_backlog_reminder_says_document_was_updated():
    record = {"backlog_id": "b1", "content": "수치 수정", "created_at": "2026-10-01", "docs": [], "doc_updated_at": "2026-10-21T10:00"}
    text = backlog_slack.reminder_item_blocks(record)[0]["text"]["text"]
    assert "📝 관련 기획서가 2026-10-21에 갱신됐어요" in text


# --- 토스 ---
def toss_view(slack, to_user, memo=""):
    view = slack.views[-1]
    view["state"] = {"values": {"to": {"value": {"selected_user": to_user}}, "memo": {"value": {"value": memo}}}}
    return view


def test_toss_moves_proposal_to_another_planner(store, notion_fakes):
    record, slack = add(), FakeSlack()
    body = button_body(record, "proposal_chat")
    proposal_slack.handle_chat(body, slack)
    slack.thread = [body["message"]]
    proposal_slack.handle_toss(button_body(record, "proposal_toss"), slack)
    assert slack.views[-1]["callback_id"] == "proposal_toss_submit"

    proposal_slack.handle_toss_submit({"user": {"id": "U1"}, "view": toss_view(slack, "U2", "전투 담당이라 넘겨요")}, slack)
    saved = backlog.get(record["backlog_id"])
    assert (saved["assignee_id"], saved["assignee_name"], saved["chat_ts"]) == ("U2", "이름-U2", "")
    assert saved["toss_history"][0]["memo"] == "전투 담당이라 넘겨요"
    old_chat, head, item = slack.posts[-3:]
    assert old_chat["thread_ts"] == "1.0" and "넘겼어요" in old_chat["text"]
    assert head["channel"] == "U2" and "<@U1> 님이 사서 제안을 넘겼어요.\n> 전투 담당이라 넘겨요" in head["text"]
    assert item["thread_ts"] == f"{len(slack.posts) - 1}.0" and saved["slack_ts"] == f"{len(slack.posts)}.0"
    assert "<@U2> 님에게 넘겼어요" in slack.updates[-1]["blocks"][-1]["elements"][0]["text"]

    # 이제 원래 담당자는 처리할 수 없고 새 담당자가 처리한다
    proposal_slack.handle_hold(button_body(saved, "proposal_hold", user="U1"), slack)
    assert "담당자만" in slack.ephemerals[-1]["text"]
    proposal_slack.handle_hold(button_body(saved, "proposal_hold", user="U2"), slack)
    assert backlog.get(record["backlog_id"])["status"] == backlog.HELD


def test_toss_to_self_does_nothing(store):
    record, slack = add(), FakeSlack()
    proposal_slack.handle_toss(button_body(record, "proposal_toss"), slack)
    proposal_slack.handle_toss_submit({"user": {"id": "U1"}, "view": toss_view(slack, "U1")}, slack)
    assert backlog.get(record["backlog_id"])["assignee_id"] == "U1" and slack.posts == []
