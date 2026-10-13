import os
from dotenv import load_dotenv

# .env를 가장 먼저 읽어 두면 다른 모듈의 import 순서와 무관하게 설정값을 쓸 수 있다
load_dotenv()

NOTION_INTEGRATION_TOKEN = os.environ.get("NOTION_INTEGRATION_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")

# QDRANT_URL/QDRANT_API_KEY가 없으면 로컬 디스크 DB(QDRANT_LOCAL_PATH)를 사용
QDRANT_URL = os.environ.get("QDRANT_URL")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY")
QDRANT_LOCAL_PATH = os.path.abspath("./qdrant_db")
COLLECTION_NAME = "game_docs"

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536
CHAT_MODEL = "gpt-4o-mini"
