import pytest
import clients
import config


@pytest.fixture
def local_db(tmp_path, monkeypatch):
    """테스트마다 새 로컬 Qdrant를 쓰고, 끝나면 닫는다 (실제 클라우드 DB에는 절대 연결하지 않음)"""
    clients.close_clients()
    monkeypatch.setattr(config, "QDRANT_URL", None)
    monkeypatch.setattr(config, "QDRANT_LOCAL_PATH", str(tmp_path / "qdrant"))
    client = clients.get_qdrant()
    yield client
    clients.close_clients()


@pytest.fixture
def no_network(monkeypatch):
    """노션/OpenAI 호출을 막아, 패치하지 않은 외부 호출이 있으면 바로 실패하게 한다"""
    import requests

    def blocked(*args, **kwargs):
        raise AssertionError("테스트에서 외부 네트워크 호출이 발생했습니다")

    monkeypatch.setattr(requests, "get", blocked)
    monkeypatch.setattr(requests, "post", blocked)
    monkeypatch.setattr(clients, "get_embeddings", blocked)
