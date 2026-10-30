"""지금 질문한 사람 (그 질문을 처리하는 동안만). 에이전트의 도구가 '질문한 사람 본인'의 정보를 읽을 때 쓴다 (예: 본인의 Jira 카드).
함수 인자로 넘기지 않고 contextvars에 두는 이유: 질문 처리의 여러 단계를 거쳐야 해서 모든 함수의 인자를 바꾸게 되기 때문이다.
도구는 사람을 인자로 받지 않으므로 모델이 다른 사람의 정보를 요청할 방법이 없다."""
import contextvars
from typing import Callable, Dict, Optional

_current = contextvars.ContextVar("asker", default=None)


class Asker:
    def __init__(self, slack_id: str, load_profile: Callable[[], Dict]):
        """load_profile은 슬랙 프로필({"name", "email"})을 읽는 함수. 실제로 필요할 때만 불러 한 번만 읽는다"""
        self.slack_id = slack_id
        self._load = load_profile
        self._profile = None

    def profile(self) -> Dict:
        if self._profile is None:
            self._profile = self._load() or {}
        return self._profile


def set_asker(asker: Asker):
    return _current.set(asker)


def reset(token) -> None:
    _current.reset(token)


def get_asker() -> Optional[Asker]:
    return _current.get()
