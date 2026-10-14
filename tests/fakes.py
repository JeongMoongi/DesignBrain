"""여러 테스트에서 쓰는 가짜 OpenAI/Qdrant 객체"""


def raiser(error):
    def raise_it(*args, **kwargs):
        raise error
    return raise_it


def fake_openai(reply=None, error=None):
    """chat.completions.create가 reply를 돌려주거나 error를 던지는 OpenAI 클라이언트"""
    def create(**kwargs):
        if error:
            raise error
        message = type("Message", (), {"content": reply})()
        return type("Completion", (), {"choices": [type("Choice", (), {"message": message})()]})()
    completions = type("Completions", (), {"create": staticmethod(create)})
    return type("OpenAI", (), {"chat": type("Chat", (), {"completions": completions})})()


def hit(payload, id="hit", score=0.9):
    """Qdrant 검색 결과 한 건"""
    return type("Hit", (), {"id": id, "score": score, "payload": payload})()


class FakeQdrant:
    """query_points가 항상 points를 돌려주거나 error를 던진다"""
    def __init__(self, points=(), error=None):
        self.points, self.error = list(points), error

    def query_points(self, **kwargs):
        if self.error:
            raise self.error
        return type("Response", (), {"points": self.points})()
