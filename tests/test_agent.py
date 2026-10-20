import json
import pytest
from qdrant_client.models import PointStruct
import agent
import ai_engine
import backlog
import clients
import config
import search


def call(name, call_id="c1", **args):
    return type("Call", (), {"type": "function_call", "name": name, "arguments": json.dumps(args, ensure_ascii=False), "call_id": call_id})()


def response(output=(), text="", id="r"):
    usage = type("Usage", (), {"input_tokens": 10, "output_tokens": 2})()
    return type("Response", (), {"id": id, "output": list(output), "output_text": text, "usage": usage})()


class FakeResponses:
    """미리 정한 응답을 차례로 돌려주고, 받은 요청을 기록한다"""
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


@pytest.fixture
def fake_responses(monkeypatch):
    def install(*responses):
        fake = FakeResponses(*responses)
        monkeypatch.setattr(clients, "get_openai", lambda: type("OpenAI", (), {"responses": fake})())
        return fake
    return install


VECTOR = [1.0] + [0.0] * (config.EMBEDDING_DIM - 1)


@pytest.fixture
def docs(local_db, monkeypatch):
    """'아이템' DB 문서(항목 2개)와, 제목이 같은 작은 '아이템' 페이지가 있는 인덱스"""
    monkeypatch.setattr(clients, "get_embedding", lambda text: VECTOR)
    monkeypatch.setattr(backlog, "notes_for_answer", lambda vector, page_ids: [])
    chunks = [("P1", 0, "문서: 아이템\n# 아이템", ""),
              ("P1", 2, "문서: 아이템\n- 항목: [겹겹이 양말] ➔ 각인 A='투지'", "겹겹이 양말"),
              ("P1", 1, "문서: 아이템\n- 항목: [맹세의 서] ➔ 각인 A='협공'", "맹세의 서"),
              ("P2", 0, "문서: 아이템\n아이콘 경로", "")]
    local_db.upsert(config.COLLECTION_NAME, [
        PointStruct(id=i + 1, vector=VECTOR, payload={"page_id": page, "chunk_idx": idx, "text": text, "title": "아이템",
                                                      "url": f"https://notion/{page}", "images": [{"block_id": f"img{i}"}],
                                                      **({"item": item} if item else {})})
        for i, (page, idx, text, item) in enumerate(chunks)])
    search.invalidate_name_catalog()
    return local_db


def test_read_document_merges_same_title_pages_in_order(docs):
    state = agent.AgentState(None)
    text = agent.tool_read_document(state, "아이템")
    assert text.split("\n") == ["[문서: 아이템]", "# 아이템", "- 항목: [맹세의 서] ➔ 각인 A='협공'",
                                "- 항목: [겹겹이 양말] ➔ 각인 A='투지'", "아이콘 경로"]
    assert len(state.hits) == 4


def test_read_document_item_and_unknown_title(docs):
    state = agent.AgentState(None)
    assert agent.tool_read_document(state, "아이템", item="맹세") == "[문서: 아이템 > 맹세]\n- 항목: [맹세의 서] ➔ 각인 A='협공'"
    assert "찾을 수 없음" in agent.tool_read_document(state, "없는 문서")
    assert "항목이 없음" in agent.tool_read_document(state, "아이템", item="석화체")


def test_read_document_splits_long_documents(docs, monkeypatch):
    monkeypatch.setattr(agent, "READ_PART_CHARS", 30)
    state = agent.AgentState(None)
    first = agent.tool_read_document(state, "아이템")
    assert first.startswith("[문서: 아이템 | 1/")
    assert "아이콘 경로" in agent.tool_read_document(state, "아이템", part=99)


def test_list_docs_filters_by_title_or_item(docs):
    state = agent.AgentState(None)
    assert agent.tool_list_docs(state) == "- 아이템 (항목: 맹세의 서, 겹겹이 양말)"
    assert agent.tool_list_docs(state, "양말") == "- 아이템 (항목: 겹겹이 양말)"
    assert "없음" in agent.tool_list_docs(state, "보스")


def test_agent_answers_after_tools(docs, fake_responses):
    fake = fake_responses(
        response([call("read_document", title="아이템", item="맹세의 서")], id="r1"),
        response(text="맹세의 서는 협공 각인을 가집니다.\n사용한 문서: 아이템", id="r2"))
    seen = []
    result = agent.ask_agent("맹세의 서 효과는?", [{"role": "user", "content": "앞 질문"}], {}, seen.append)

    assert result["AI_답변"].startswith("맹세의 서는 협공 각인을 가집니다.")
    assert "[아이템](https://notion/P1)" in result["AI_답변"]
    assert result["used_docs"] == ["아이템"]
    # 답변에 이름이 나온 항목의 도판만 쓴다
    assert [img["block_id"] for img in result["images"]] == ["img2"]
    assert result["search_query"] == "read_document(아이템, 맹세의 서)"
    assert seen == ["📖 '아이템' 읽는 중…"]
    # 대화 기록이 함께 넘어가고, 도구 결과는 이전 응답에 이어서 보낸다
    assert fake.requests[0]["input"][0] == {"role": "user", "content": "앞 질문"}
    assert fake.requests[1]["previous_response_id"] == "r1"
    assert fake.requests[1]["input"][0]["call_id"] == "c1"


def test_agent_stops_tools_after_max_rounds(docs, fake_responses, monkeypatch):
    monkeypatch.setattr(config, "AGENT_MAX_TOOL_ROUNDS", 2)
    fake = fake_responses(response([call("list_docs")]), response([call("list_docs")]),
                          response(text="정리한 답변\n사용한 문서: 없음"))
    result = agent.ask_agent("질문", [], {}, None)
    assert result["AI_답변"] == "정리한 답변"
    assert "tool_choice" not in fake.requests[1]
    assert fake.requests[2]["tool_choice"] == "none"


def test_bad_tool_arguments_go_back_to_model(docs):
    state = agent.AgentState(None)
    assert "인자" in agent.run_tool(state, "read_document", "{not json")
    assert "인자" in agent.run_tool(state, "read_document", json.dumps({"wrong": 1}))
    assert "알 수 없는 도구" in agent.run_tool(state, "delete_all", "{}")


def test_answer_mode_switches_to_agent(monkeypatch):
    monkeypatch.setattr(config, "ANSWER_MODE", "agent")
    monkeypatch.setattr(agent, "ask_agent", lambda query, history, progress, on_progress: {"status": "success", "AI_답변": "에이전트"})
    assert ai_engine.ask_ai_with_history("질문", [])["AI_답변"] == "에이전트"


@pytest.fixture
def hero_docs(local_db, monkeypatch):
    """세 영웅 아래에 같은 제목 '전투 요소' 문서가 있는 인덱스"""
    monkeypatch.setattr(clients, "get_embedding", lambda text: VECTOR)
    monkeypatch.setattr(backlog, "notes_for_answer", lambda vector, page_ids: [])
    heroes = ["디고리", "카일린"]
    local_db.upsert(config.COLLECTION_NAME, [
        PointStruct(id=i + 1, vector=VECTOR, payload={
            "page_id": f"H{i}", "chunk_idx": 0, "title": "전투 요소", "path": f"영웅 인증 페이지 > 영웅 목록 > {hero}",
            "text": f"문서: 전투 요소 (위치: 영웅 인증 페이지 > 영웅 목록 > {hero})\n{hero}의 스킬", "url": f"https://notion/H{i}", "images": []})
        for i, hero in enumerate(heroes)])
    search.invalidate_name_catalog()
    return local_db


def test_same_title_in_different_places_is_named_by_parent(hero_docs):
    state = agent.AgentState(None)
    assert agent.tool_list_docs(state) == "- 디고리 > 전투 요소\n- 카일린 > 전투 요소"
    assert agent.tool_read_document(state, "카일린 > 전투 요소") == "[문서: 카일린 > 전투 요소]\n카일린의 스킬"
    # 검색 결과에도 읽을 때 쓰는 이름이 붙고, 머리줄(문서/위치)은 빠진다
    results = agent.tool_search_docs(state, "스킬").split("\n\n")
    assert sorted(results) == ["[디고리 > 전투 요소]\n디고리의 스킬", "[카일린 > 전투 요소]\n카일린의 스킬"]


def test_links_and_citations_use_parent_for_same_titles(hero_docs, fake_responses):
    fake_responses(response([call("search_docs", query="스킬")], id="r1"),
                   response(text="두 영웅의 스킬\n사용한 문서: 디고리 > 전투 요소 | 카일린 > 전투 요소", id="r2"))
    result = agent.ask_agent("스킬 알려줘", [], {}, None)
    assert "[디고리 > 전투 요소](https://notion/H0)" in result["AI_답변"]
    assert "[카일린 > 전투 요소](https://notion/H1)" in result["AI_답변"]
