import json
import pytest
import clients
import config
import indexing
import notion_parser
import notion_writer
import property_check
import property_slack
import proposal_slack

PAGE_ID = "11111111-1111-1111-1111-111111111111"
URL = "https://notion/P"

# 기획서 DB의 속성 정의 (노션 API가 돌려주는 모양)
SCHEMA = {
    "이름": {"type": "title"},
    "기획서 개요": {"type": "rich_text"},
    "상태": {"type": "status", "status": {"options": [{"name": "작성 중"}, {"name": "검토 중"}, {"name": "완료"}]}},
    "담당자": {"type": "people"},
    "마지막 수정": {"type": "date"},
}


def page(title="디고리", summary="", status=None, people=(), date=None, parent_type="database_id"):
    return {"id": PAGE_ID, "url": URL, "in_trash": False, "archived": False,
            "last_edited_time": "2026-10-23T16:30:00.000Z",  # 한국 시간으로는 10월 24일 01:30
            "created_by": {"id": "N-CREATOR"},
            "parent": {"type": parent_type, parent_type: "DB1"},
            "properties": {
                "이름": {"type": "title", "title": [{"plain_text": title}] if title else []},
                "기획서 개요": {"type": "rich_text", "rich_text": [{"plain_text": summary}] if summary else []},
                "상태": {"type": "status", "status": {"name": status} if status else None},
                "담당자": {"type": "people", "people": [{"id": p} for p in people]},
                "마지막 수정": {"type": "date", "date": {"start": date} if date else None}}}


def doc_data(title="디고리"):
    return {"page_id": PAGE_ID, "title": title, "path": ["영웅 목록"], "full_text": f"# {title}\n디고리는 근접 영웅이다."}


class FakeSlack:
    def __init__(self):
        self.posts, self.updates, self.ephemerals, self.views = [], [], [], []

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"channel": "D-" + kwargs["channel"], "ts": f"{len(self.posts)}.0"}

    def chat_update(self, **kwargs):
        self.updates.append(kwargs)

    def chat_postEphemeral(self, **kwargs):
        self.ephemerals.append(kwargs)

    def views_open(self, **kwargs):
        self.views.append(kwargs["view"])


class Notion:
    """가짜 노션: 지금 페이지/DB 속성 정의를 돌려주고, 속성 쓰기를 기록한다"""
    def __init__(self, current, schema=SCHEMA):
        self.current, self.schema, self.writes = current, schema, []


@pytest.fixture
def env(monkeypatch, local_db):
    """노션/슬랙/OpenAI를 가짜로 바꾼 속성 검사 환경"""
    notion = Notion(page())
    slack = FakeSlack()
    ai = {"reply": json.dumps({"name": "디고리 영웅", "summary": "디고리는 근접 영웅이다.", "status": "작성 중"}), "calls": 0}

    def create(**kwargs):
        ai["calls"] += 1
        ai["input"] = kwargs["input"]
        return type("R", (), {"output_text": ai["reply"]})()

    monkeypatch.setattr(notion_parser, "fetch_page", lambda page_id, token: notion.current)
    monkeypatch.setattr(notion_parser, "fetch_object", lambda kind, obj_id, token: {"properties": notion.schema})
    monkeypatch.setattr(notion_writer, "update_page_properties", lambda page_id, props: notion.writes.append((page_id, props)))
    monkeypatch.setattr(notion_writer, "user_info", lambda notion_id: {"name": "정문기", "email": "u1@game.com"})
    monkeypatch.setattr(proposal_slack, "web_client", lambda: slack)
    monkeypatch.setattr(proposal_slack, "slack_user_for_notion", lambda client, notion_id: "U1" if notion_id == "N-REQ" else None)
    monkeypatch.setattr(config, "DIAGNOSIS_REPORT_USER_ID", None)
    monkeypatch.setattr(config, "NOTION_INTEGRATION_TOKEN", "tok")
    monkeypatch.setattr(clients, "get_openai", lambda: type("O", (), {"responses": type("Rs", (), {"create": staticmethod(create)})})())
    return type("Env", (), {"notion": notion, "slack": slack, "ai": ai})


def run(requested_by="N-REQ", title="디고리"):
    return property_check.run(doc_data(title), "tok", requested_by)


def button(record, action_id, user="U1"):
    return {"user": {"id": user}, "channel": {"id": "D-U1"}, "trigger_id": "T", "message": {"ts": "1.0"},
            "actions": [{"action_id": action_id, "value": record["page_id"]}]}


# --- 검사 ---
def test_inspect_finds_every_empty_property():
    missing = property_check.inspect(page(title=""), SCHEMA)
    assert [m["key"] for m in missing] == ["name", "summary", "status", "owner", "modified"]
    assert {m["reason"] for m in missing} == {"empty"}


def test_inspect_passes_a_complete_page():
    complete = page(summary="개요", status="완료", people=["N1"], date="2026-10-01")
    assert property_check.inspect(complete, SCHEMA) == []


def test_inspect_reports_columns_the_database_lacks_as_unsupported():
    schema = {k: v for k, v in SCHEMA.items() if k != "담당자"}
    missing = {m["key"]: m["reason"] for m in property_check.inspect(page(), schema)}
    assert missing["owner"] == "unsupported" and missing["summary"] == "empty"


def test_inspect_ignores_auto_filled_last_edited_time_column():
    schema = {**SCHEMA, "마지막 수정": {"type": "last_edited_time"}}
    assert "modified" not in {m["key"] for m in property_check.inspect(page(), schema)}


def test_property_names_can_be_changed_by_config(monkeypatch):
    monkeypatch.setattr(config, "PROPERTY_SUMMARY", "설명")
    schema = {**{k: v for k, v in SCHEMA.items() if k != "기획서 개요"}, "설명": {"type": "rich_text"}}
    current = page(summary="")
    current["properties"]["설명"] = {"type": "rich_text", "rich_text": []}
    assert "summary" in {m["key"] for m in property_check.inspect(current, schema) if m["reason"] == "empty"}


# --- 제안값 ---
def test_proposal_uses_ai_for_text_and_facts_for_owner_and_date(env):
    missing = property_check.inspect(page(title=""), SCHEMA)
    proposal = property_check.build_proposal(page(title=""), doc_data(), missing, SCHEMA, "N-REQ")
    assert proposal["name"]["value"] == "디고리 영웅"
    assert proposal["summary"]["value"] == "디고리는 근접 영웅이다."
    assert proposal["status"]["value"] == "작성 중"
    assert proposal["owner"] == {"text": "정문기", "value": "N-REQ"}
    # 노션의 UTC 시각을 한국 날짜로
    assert proposal["modified"]["value"] == "2026-10-24"
    assert "작성 중, 검토 중, 완료" in env.ai["input"]


def test_status_outside_the_options_is_dropped(env):
    env.ai["reply"] = json.dumps({"summary": "개요", "status": "아무 값"})
    missing = property_check.inspect(page(), SCHEMA)
    proposal = property_check.build_proposal(page(), doc_data(), missing, SCHEMA, "N-REQ")
    assert "status" not in proposal and "summary" in proposal


def test_ai_is_not_called_when_only_facts_are_missing(env):
    current = page(summary="개요", status="완료")
    missing = property_check.inspect(current, SCHEMA)
    proposal = property_check.build_proposal(current, doc_data(), missing, SCHEMA, "N-REQ")
    assert set(proposal) == {"owner", "modified"} and env.ai["calls"] == 0


# --- 슬랙으로 묻기 ---
def test_run_warns_the_requester_with_proposal_and_buttons(env):
    record = run()
    assert record["status"] == "open" and record["assignee_id"] == "U1"
    sent = env.slack.posts[0]
    assert sent["channel"] == "U1"
    text = json.dumps(sent["blocks"], ensure_ascii=False)
    assert "⚠️" in text and "기획서 개요, 상태, 담당자, 마지막 수정" in text and "디고리는 근접 영웅이다." in text
    actions = [b for b in sent["blocks"] if b["type"] == "actions"][0]
    assert [e["action_id"] for e in actions["elements"]] == ["property_apply", "property_edit", "property_skip"]
    assert property_check.get_record(PAGE_ID)["slack_ts"] == "1.0"


def test_same_missing_properties_are_asked_only_once(env):
    assert run() is not None
    assert run() is None
    assert len(env.slack.posts) == 1


def test_asks_again_when_a_different_property_becomes_missing(env):
    run()
    env.notion.current = page(summary="개요", status="완료", people=["N1"])  # 마지막 수정만 비어 있다
    assert run() is not None
    assert len(env.slack.posts) == 2


def test_complete_page_sends_nothing_and_closes_a_pending_question(env):
    run()
    env.notion.current = page(summary="개요", status="완료", people=["N1"], date="2026-10-01")
    assert run() is None
    assert property_check.get_record(PAGE_ID)["status"] == "done"
    assert "이미 채워져 있어요" in env.slack.updates[0]["text"]


def test_pages_outside_a_planning_database_are_skipped(env):
    env.notion.current = page(parent_type="page_id")
    assert run() is None
    env.notion.current = page()
    env.notion.schema = {"이름": {"type": "title"}, "유형": {"type": "select"}}  # 몹/아이템 같은 다른 DB
    assert run() is None
    assert env.slack.posts == []


def test_requester_not_found_in_slack_goes_to_the_fallback_assignee(env, monkeypatch):
    monkeypatch.setattr(config, "DIAGNOSIS_REPORT_USER_ID", "UFALLBACK")
    record = run(requested_by="N-UNKNOWN")
    assert record["assignee_id"] == "UFALLBACK"
    assert "대신 보내요" in json.dumps(env.slack.posts[0]["blocks"], ensure_ascii=False)


def test_nobody_to_ask_leaves_no_record_so_next_time_retries(env):
    assert run(requested_by="N-UNKNOWN") is None
    assert property_check.get_record(PAGE_ID) is None


def test_only_columns_missing_from_the_database_do_not_ask(env):
    env.notion.schema = {"이름": {"type": "title"}, "상태": SCHEMA["상태"]}
    env.notion.current = page(status="완료")
    env.notion.current["properties"] = {k: v for k, v in env.notion.current["properties"].items() if k in ("이름", "상태")}
    assert run() is None


def test_failures_never_break_the_sync(env, monkeypatch):
    monkeypatch.setattr(notion_parser, "fetch_page", lambda *a: (_ for _ in ()).throw(RuntimeError("노션 장애")))
    assert run() is None


# --- 버튼 ---
def test_apply_writes_the_proposal_to_notion(env):
    record = run()
    property_slack.handle_apply(button(record, "property_apply"), env.slack)
    page_id, props = env.notion.writes[0]
    assert page_id == PAGE_ID
    assert props["기획서 개요"] == {"rich_text": [{"type": "text", "text": {"content": "디고리는 근접 영웅이다."},
                                           "annotations": {"bold": False, "color": "default"}}]}
    assert props["상태"] == {"status": {"name": "작성 중"}}
    assert props["담당자"] == {"people": [{"id": "N-REQ"}]}
    assert props["마지막 수정"] == {"date": {"start": "2026-10-24"}}
    assert "이름" not in props  # 제목은 이미 있었다
    assert property_check.get_record(PAGE_ID)["status"] == "applied"
    assert "✅ 적용했어요" in env.slack.updates[0]["text"]
    assert not any(b["type"] == "actions" for b in env.slack.updates[0]["blocks"])


def test_apply_does_not_overwrite_what_someone_filled_in_meanwhile(env):
    record = run()
    env.notion.current = page(summary="누가 먼저 쓴 개요")
    property_slack.handle_apply(button(record, "property_apply"), env.slack)
    assert "기획서 개요" not in env.notion.writes[0][1]


def test_only_the_assignee_can_press_buttons(env):
    record = run()
    property_slack.handle_apply(button(record, "property_apply", user="U2"), env.slack)
    assert env.notion.writes == [] and "담당자만" in env.slack.ephemerals[0]["text"]
    assert property_check.get_record(PAGE_ID)["status"] == "open"


def test_a_handled_request_cannot_be_applied_twice(env):
    record = run()
    property_slack.handle_apply(button(record, "property_apply"), env.slack)
    property_slack.handle_apply(button(record, "property_apply"), env.slack)
    assert len(env.notion.writes) == 1 and "이미 처리" in env.slack.ephemerals[0]["text"]


def test_skip_stops_asking_until_something_else_is_missing(env):
    record = run()
    property_slack.handle_skip(button(record, "property_skip"), env.slack)
    assert env.notion.writes == [] and property_check.get_record(PAGE_ID)["status"] == "skipped"
    assert run() is None
    assert len(env.slack.posts) == 1


def test_notion_failure_keeps_the_request_open(env, monkeypatch):
    record = run()
    monkeypatch.setattr(notion_writer, "update_page_properties", lambda *a: (_ for _ in ()).throw(RuntimeError("권한 없음")))
    property_slack.handle_apply(button(record, "property_apply"), env.slack)
    assert "입력하지 못했어요" in env.slack.ephemerals[0]["text"]
    assert property_check.get_record(PAGE_ID)["status"] == "open"


def test_edit_modal_is_prefilled_and_submit_applies_the_edit(env):
    record = run()
    property_slack.handle_edit(button(record, "property_edit"), env.slack)
    view = env.slack.views[0]
    inputs = {b["block_id"]: b["element"] for b in view["blocks"] if b["type"] == "input"}
    assert set(inputs) == {"summary", "status"}
    assert inputs["summary"]["initial_value"] == "디고리는 근접 영웅이다."
    assert inputs["status"]["initial_option"]["value"] == "작성 중"
    submitted = {"user": {"id": "U1"}, "view": {"private_metadata": view["private_metadata"], "state": {"values": {
        "summary": {"value": {"value": "근접 딜러 영웅이다."}},
        "status": {"value": {"selected_option": {"value": "검토 중"}}}}}}}
    property_slack.handle_edit_submit(submitted, env.slack)
    props = env.notion.writes[0][1]
    assert props["기획서 개요"]["rich_text"][0]["text"]["content"] == "근접 딜러 영웅이다."
    assert props["상태"] == {"status": {"name": "검토 중"}}
    assert props["담당자"] == {"people": [{"id": "N-REQ"}]}  # 고치지 않는 항목은 정한 대로


def test_blank_field_in_the_edit_modal_is_not_written(env):
    record = run()
    property_slack.handle_edit(button(record, "property_edit"), env.slack)
    submitted = {"user": {"id": "U1"}, "view": {"private_metadata": env.slack.views[0]["private_metadata"], "state": {"values": {
        "summary": {"value": {"value": "  "}}, "status": {"value": {"selected_option": None}}}}}}
    property_slack.handle_edit_submit(submitted, env.slack)
    assert set(env.notion.writes[0][1]) == {"담당자", "마지막 수정"}


def test_select_type_status_column_is_written_as_select(env):
    env.notion.schema = {**SCHEMA, "상태": {"type": "select", "select": {"options": [{"name": "작성 중"}]}}}
    env.notion.current = page()
    env.notion.current["properties"]["상태"] = {"type": "select", "select": None}
    record = run()
    property_slack.handle_apply(button(record, "property_apply"), env.slack)
    assert env.notion.writes[0][1]["상태"] == {"select": {"name": "작성 중"}}


def test_renaming_a_document_resyncs_it(env, monkeypatch):
    env.notion.current = page(title="")
    record = run(title="제목 없음")
    synced = []

    class Immediate:
        def __init__(self, target, args, **kwargs):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(property_slack.threading, "Thread", Immediate)
    monkeypatch.setattr(indexing, "sync_document_to_db", lambda page_id, token, requested_by=None: synced.append((page_id, requested_by)))
    property_slack.handle_apply(button(record, "property_apply"), env.slack)
    assert env.notion.writes[0][1]["이름"]["title"][0]["text"]["content"] == "디고리 영웅"
    assert synced == [(PAGE_ID, None)]


# --- 동기화 연결 ---
@pytest.fixture
def sync_env(local_db, monkeypatch, no_network):
    calls = []
    monkeypatch.setattr(property_check, "run", lambda doc, token, requested_by: calls.append(requested_by))
    monkeypatch.setattr(notion_parser, "fetch_page", lambda page_id, token: {
        "in_trash": False, "archived": False, "url": URL, "properties": {"n": {"type": "title", "title": [{"plain_text": "문서"}]}}})
    monkeypatch.setattr(notion_parser, "extract_blocks_recursive", lambda page_id, token, out, images, *a, **k: out.extend(["본문 한 줄", "본문 두 줄"]))
    monkeypatch.setattr(clients, "get_embeddings", lambda texts: [[0.1] * 1536 for _ in texts])
    return calls


def test_sync_checks_properties_only_for_the_db_register_button(sync_env):
    indexing.sync_document_to_db(PAGE_ID, "tok")                                           # 전체 재동기화
    indexing.sync_document_to_db(PAGE_ID, "tok", requested_by="N-REQ")                     # 사서 제안 승인 뒤 재동기화
    assert sync_env == []
    indexing.sync_document_to_db(PAGE_ID, "tok", requested_by="N-REQ", check_properties=True)  # 'DB 등록' 버튼
    assert sync_env == ["N-REQ"]


# --- 실제 기획서 DB 구조 (니사리엘 문서 화면 기준) ---
REAL_SCHEMA = {
    "이름": {"type": "title"},
    "담당자": {"type": "people"},
    "분류": {"type": "select", "select": {"options": [{"name": "영웅"}]}},
    "상태": {"type": "select", "select": {"options": [{"name": "작성 중"}, {"name": "완료"}]}},
    "마지막 수정": {"type": "date"},
    "기획서 개요": {"type": "rich_text"},
    "참여자": {"type": "people"},
}


def real_page(summary="리더/파트너 니사리엘의 혈기 능력과 폭주 상태를 정리한다."):
    return {"id": PAGE_ID, "url": URL, "in_trash": False, "archived": False, "last_edited_time": "2026-07-04T03:00:00.000Z",
            "parent": {"type": "database_id", "database_id": "DB1"},
            "properties": {
                "이름": {"type": "title", "title": [{"plain_text": "니사리엘"}]},
                "담당자": {"type": "people", "people": [{"id": "N-SANGWOO"}]},
                "분류": {"type": "select", "select": {"name": "영웅"}},
                "상태": {"type": "select", "select": {"name": "완료"}},
                "마지막 수정": {"type": "date", "date": {"start": "2026-07-04"}},
                "기획서 개요": {"type": "rich_text", "rich_text": [{"plain_text": summary}] if summary else []},
                "참여자": {"type": "people", "people": []}}}


def test_real_database_complete_page_is_not_flagged_even_with_empty_participants(env):
    env.notion.schema, env.notion.current = REAL_SCHEMA, real_page()
    assert run(title="니사리엘") is None
    assert env.slack.posts == []


def test_real_database_empty_summary_is_the_only_thing_asked(env):
    env.notion.schema, env.notion.current = REAL_SCHEMA, real_page(summary="")
    env.ai["reply"] = json.dumps({"summary": "니사리엘의 혈기 능력을 정리한다."})
    record = run(title="니사리엘")
    assert record["missing"] == ["summary"]
    text = json.dumps(env.slack.posts[0]["blocks"], ensure_ascii=False)
    assert "*기획서 개요*" in text and "참여자" not in text
    property_slack.handle_apply(button(record, "property_apply"), env.slack)
    assert list(env.notion.writes[0][1]) == ["기획서 개요"]
