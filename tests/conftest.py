import pytest
import clients
import config


@pytest.fixture(autouse=True)
def isolate_external_services(tmp_path, monkeypatch):
    """모든 테스트는 실제 Qdrant Cloud/OpenAI에 닿지 않는다.
    Qdrant는 테스트마다 새 로컬 DB를 쓰고(처음 쓸 때 만든다), OpenAI는 테스트가 직접 바꿔치기하지 않으면 호출 즉시 실패한다.
    (기록 기능처럼 흐름 깊숙이 DB를 쓰는 코드가 늘어도 운영 데이터에 테스트 기록이 섞이지 않도록)"""
    clients.close_clients()
    monkeypatch.setattr(config, "QDRANT_URL", None)
    monkeypatch.setattr(config, "QDRANT_API_KEY", None)
    monkeypatch.setattr(config, "QDRANT_LOCAL_PATH", str(tmp_path / "qdrant"))

    def blocked_openai():
        raise AssertionError("테스트에서 실제 OpenAI 호출이 발생했습니다 (clients.get_openai를 바꿔치기하세요)")

    monkeypatch.setattr(clients, "get_openai", blocked_openai)
    yield
    clients.close_clients()


@pytest.fixture
def local_db():
    """테스트용 로컬 Qdrant 클라이언트 (isolate_external_services가 테스트마다 새로 만든다)"""
    return clients.get_qdrant()


@pytest.fixture
def no_network(monkeypatch):
    """노션/OpenAI 호출을 막아, 패치하지 않은 외부 호출이 있으면 바로 실패하게 한다"""
    import requests

    def blocked(*args, **kwargs):
        raise AssertionError("테스트에서 외부 네트워크 호출이 발생했습니다")

    monkeypatch.setattr(requests, "get", blocked)
    monkeypatch.setattr(requests, "post", blocked)
    monkeypatch.setattr(clients, "get_embeddings", blocked)
