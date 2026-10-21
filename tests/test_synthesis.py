import json
import pytest
import agent
import backlog
import clients
import config
import librarian
import proposal_slack
import review
import synthesis
from test_proposals import FakeSlack, VECTOR

DOCS = [{"page_id": "P1", "title": "챕터 1 몹", "url": "https://notion/P1"},
        {"page_id": "P2", "title": "변종 분쇄자", "url": "https://notion/P2"}]
DRAFT = {"new_doc_title": "챕터 1 적 구성", "problem": "몹, 미니보스, 보스가 따로 있음",
         "proposal": "## 일반몹\n- 석화체 (참고: 챕터 1 몹)", "assumptions": "", "related_docs": ["챕터 1 몹", "변종 분쇄자"]}


@pytest.fixture
def store(local_db, monkeypatch):
    monkeypatch.setattr(clients, "get_embedding", lambda text: VECTOR)
    return local_db


@pytest.fixture
def librarian_fakes(store, monkeypatch):
    drafts, slack = [], FakeSlack()
    monkeypatch.setattr(review, "run_with_tools", lambda instructions, request, label: drafts.append(request) or json.dumps(DRAFT, ensure_ascii=False))
    monkeypatch.setattr(review, "related_docs", lambda names: [d for d in DOCS if d["title"] in names])
    monkeypatch.setattr(synthesis, "page_creator", lambda page_id: f"N-{page_id}")
    monkeypatch.setattr(proposal_slack, "web_client", lambda: slack)
    monkeypatch.setattr(proposal_slack, "slack_user_for_notion", lambda client, notion_id: {"N-P1": "UAUTHOR"}.get(notion_id))
    return drafts, slack


def test_agent_tool_queues_one_request_per_question(store):
    state = agent.AgentState(None)
    state.query = "챕터 1 적 알려줘"
    assert "요청했다" in agent.tool_request_summary(state, "챕터 1 적 구성", "흩어져 있음", ["챕터 1 몹"])
    assert "이미 요청" in agent.tool_request_summary(state, "다른 주제", "", [])
    job, = librarian.pending_jobs()
    assert (job["kind"], job["topic"], job["question"], job["doc_names"]) == ("synthesis", "챕터 1 적 구성", "챕터 1 적 알려줘", ["챕터 1 몹"])


def test_agent_tool_only_records_when_requests_are_off(store, monkeypatch):
    monkeypatch.setattr(config, "SYNTHESIS_REQUESTS", False)
    state = agent.AgentState(None)
    agent.tool_request_summary(state, "챕터 1 적 구성", "", [])
    assert state.summary_requests == ["챕터 1 적 구성"]
    assert librarian.pending_jobs() == []


def test_same_topic_waiting_is_merged(store):
    librarian.enqueue_synthesis("챕터 1 적 구성", "", "질문 1", [])
    librarian.enqueue_synthesis("챕터1 적 구성", "", "질문 2", [])
    assert [j["question"] for j in librarian.pending_jobs()] == ["질문 2"]


def test_request_becomes_writing_proposal_for_document_creator(librarian_fakes):
    drafts, slack = librarian_fakes
    librarian.enqueue_synthesis("챕터 1 적 구성", "몹·보스가 따로 있음", "챕터 1 적 알려줘", ["챕터 1 몹"])
    librarian.process_jobs()
    assert librarian.pending_jobs() == []
    assert "[요청 주제] 챕터 1 적 구성" in drafts[0] and "[기획자의 질문] 챕터 1 적 알려줘" in drafts[0]

    record, = backlog.proposals_for_page("P1")
    assert (record["proposal_type"], record["content"], record["new_doc_title"]) == (
        backlog.WRITING, "작성이 필요함: 챕터 1 적 구성", "챕터 1 적 구성")
    assert record["assignee_id"] == "UAUTHOR"
    assert [d["title"] for d in record["docs"]] == ["챕터 1 몹", "변종 분쇄자"]
    head, item = slack.posts
    assert head["channel"] == "UAUTHOR" and head["text"].startswith("📚 @Brain이 질문에 답하면서 *챕터 1 적 구성*")
    assert "> 챕터 1 적 알려줘" in head["text"] and item["thread_ts"] == "1.0"


def test_same_topic_judged_by_model_is_not_drafted_again(librarian_fakes, monkeypatch):
    from fakes import fake_openai
    drafts, _ = librarian_fakes
    synthesis.handle_request({"topic": "챕터 1 적 구성", "doc_names": []})

    monkeypatch.setattr(clients, "get_openai", lambda: fake_openai("1"))
    assert synthesis.handle_request({"topic": "챕터 1 적 정리", "doc_names": []}) is None
    monkeypatch.setattr(clients, "get_openai", lambda: fake_openai("0"))
    assert synthesis.handle_request({"topic": "챕터 2 적 구성", "doc_names": []}) is not None
    assert len(drafts) == 2


def test_no_candidates_skips_the_model(store):
    assert synthesis.similar_proposal("챕터 1 적 구성", VECTOR) is None


def test_skip_answer_makes_no_proposal(librarian_fakes, monkeypatch):
    monkeypatch.setattr(review, "run_with_tools", lambda *args: '{"skip": "이미 한 문서에 있음"}')
    assert synthesis.handle_request({"topic": "챕터 1 적 구성", "doc_names": []}) is None
    assert backlog.proposals_for_page("P1") == []


def test_unknown_creator_goes_to_fallback_user(librarian_fakes, monkeypatch):
    _, slack = librarian_fakes
    monkeypatch.setattr(config, "DIAGNOSIS_REPORT_USER_ID", "UADMIN")
    monkeypatch.setattr(synthesis, "page_creator", lambda page_id: None)
    record = synthesis.handle_request({"topic": "챕터 1 적 구성", "doc_names": []})
    assert record["assignee_id"] == "UADMIN"
    assert "찾지 못해" in slack.posts[0]["text"]


# --- 원본이 바뀌면 지도 갱신 제안 ---
class FakeResponsesAPI:
    def __init__(self, text):
        self.text, self.requests = text, []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return type("Response", (), {"output_text": self.text})()


@pytest.fixture
def approved_map(store, monkeypatch):
    """P1·P2로 만든 지도 S1이 승인되어 있고, 정리본이 있는 상태"""
    from qdrant_client.models import PointStruct
    record = backlog.add_proposal({"type": "writing", "title": "작성이 필요함: 챕터 1 적 구성", "proposal": "- 석화체",
                                   "new_doc_title": "챕터 1 적 구성"}, DOCS[0], DOCS[1:], "UAUTHOR", "작성자", "r")
    backlog.close(record["backlog_id"], "UAPPROVER", backlog.RESOLVED, notion_page_id="S1", notion_url="https://notion/S1")
    store.upsert(config.REFINED_COLLECTION, [
        PointStruct(id=i, vector=VECTOR, payload={"page_id": pid, "chunk_idx": 0, "text": f"문서: x\n{text}", "title": pid})
        for i, (pid, text) in enumerate([("S1", "- 석화체 — 슈퍼아머 (참고: 챕터 1 몹)"), ("P1", "## 항목: 석화체\n- 유형: 잡몹")], 1)])
    slack = FakeSlack()
    monkeypatch.setattr(proposal_slack, "web_client", lambda: slack)
    monkeypatch.setattr(backlog, "linked_docs", lambda page_ids: [d for d in DOCS if d["page_id"] in page_ids])
    return slack


def install_responses(monkeypatch, payload):
    fake = FakeResponsesAPI(json.dumps(payload, ensure_ascii=False))
    monkeypatch.setattr(clients, "get_openai", lambda: type("OpenAI", (), {"responses": fake})())
    return fake


def test_changed_source_sends_map_update_to_approver(approved_map, monkeypatch):
    slack = approved_map
    fake = install_responses(monkeypatch, {"needs_update": True, "title": "결정이 필요함: 석화체 분류 변경 반영",
                                           "problem": "잡몹으로 바뀜", "proposal": "- 석화체 — 잡몹 (참고: 챕터 1 몹)"})
    sent = synthesis.check_dependents("P1")
    assert "[지도 문서] 챕터 1 적 구성\n- 석화체 — 슈퍼아머" in fake.requests[0]["input"]
    assert "[바뀐 문서]\n## 항목: 석화체" in fake.requests[0]["input"]
    record, = sent
    assert (record["proposal_type"], record["page_ids"], record["assignee_id"]) == (backlog.DECISION, ["S1"], "UAPPROVER")
    assert slack.posts[0]["channel"] == "UAPPROVER" and slack.posts[0]["text"].startswith("🗺️ <https://notion/P1|챕터 1 몹>")
    # 지도의 근거가 아닌 문서가 바뀌면 아무것도 하지 않는다
    assert synthesis.check_dependents("P9") == []


def test_unchanged_map_sends_nothing(approved_map, monkeypatch):
    install_responses(monkeypatch, {"needs_update": False})
    assert synthesis.check_dependents("P2") == []
    assert approved_map.posts == []


def test_only_changed_originals_trigger_map_check(store, monkeypatch):
    checked = []
    monkeypatch.setattr(synthesis, "check_dependents", checked.append)
    for changed in (3, None):
        monkeypatch.setattr(librarian, "refine_document", lambda page_id, changed=changed: changed)
        librarian.enqueue("P1")
        librarian.process_jobs()
    assert checked == ["P1"]
