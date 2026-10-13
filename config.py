import logging
import os
from dotenv import load_dotenv

# .env를 가장 먼저 읽어 두면 다른 모듈의 import 순서와 무관하게 설정값을 쓸 수 있다
load_dotenv()

# 동기화 시 원문/청크 전체 덤프 같은 진단 로그는 LOG_LEVEL=DEBUG 일 때만 출력된다
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
# OpenAI/Qdrant 클라이언트가 HTTP 요청마다 남기는 INFO 로그는 경고 이상만 남긴다
logging.getLogger("httpx").setLevel(max(logging.WARNING, logging.getLogger().level))

NOTION_INTEGRATION_TOKEN = os.environ.get("NOTION_INTEGRATION_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
# 노션 자동화 웹훅에 커스텀 헤더 X-Webhook-Secret 으로 같은 값을 넣는다. 비어 있으면 검증하지 않는다.
NOTION_WEBHOOK_SECRET = os.environ.get("NOTION_WEBHOOK_SECRET")

# QDRANT_URL/QDRANT_API_KEY가 없으면 로컬 디스크 DB(QDRANT_LOCAL_PATH)를 사용
QDRANT_URL = os.environ.get("QDRANT_URL")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY")
QDRANT_LOCAL_PATH = os.path.abspath("./qdrant_db")
COLLECTION_NAME = "game_docs"
# 백로그는 기획서와 별도 컬렉션에 둔다 (기획서 재동기화/삭제가 백로그를 건드리지 않도록)
BACKLOG_COLLECTION = "backlogs"
# 매일 이 시각(한국 시간)에 담당자에게 미해결 백로그 확인 DM을 보낸다
BACKLOG_REMINDER_HOUR = 10

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536
CHAT_MODEL = "gpt-4o-mini"
