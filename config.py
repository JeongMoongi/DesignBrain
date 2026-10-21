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
# 질문/답변 기록 (서비스 개선용). 벡터 없이 payload만 저장한다
QA_LOG_COLLECTION = "qa_logs"
# 매주 이 요일(월=0)·시각(한국 시간)에 지난 7일 질문 기록 요약을 이 슬랙 사용자에게 DM으로 보낸다 (비어 있으면 보내지 않음)
QA_REPORT_USER_ID = os.environ.get("QA_REPORT_USER_ID")
QA_REPORT_WEEKDAY = 0
QA_REPORT_HOUR = 10
# 문서 진단 결과 (문서마다 최신 하나, 벡터 없음)
DIAGNOSIS_COLLECTION = "doc_diagnoses"
# '완료'한 사람을 슬랙에서 찾지 못했을 때 사서 제안을 대신 받는 사람 (기본: 질문 기록 요약 받는 사람)
DIAGNOSIS_REPORT_USER_ID = os.environ.get("DIAGNOSIS_REPORT_USER_ID") or QA_REPORT_USER_ID
# 사서(librarian.py)가 원본을 정리한 정리본과, 정리할 문서 작업 목록 (벡터 없음)
REFINED_COLLECTION = "refined_docs"
LIBRARIAN_JOB_COLLECTION = "librarian_jobs"
REFINE_MODEL = os.environ.get("REFINE_MODEL", "gpt-6.1-sol")
REFINE_REASONING_EFFORT = os.environ.get("REFINE_REASONING_EFFORT", "low")
# 기획자가 '완료'한 문서를 검토해 충돌/누락/새 주제를 찾을 때의 추론 강도 (모델은 REFINE_MODEL)
REVIEW_REASONING_EFFORT = os.environ.get("REVIEW_REASONING_EFFORT", "medium")
# 승인된 '작성이 필요함' 제안을 새 문서로 만드는 노션 DB (자동 작성 기획)
PROPOSAL_DATABASE_ID = os.environ.get("PROPOSAL_DATABASE_ID", "400107d6982c809ab39bc6bea4a9a2f5")

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536
CHAT_MODEL = "gpt-4o-mini"

# 답변 방식: classic = 검색 1회 후 CHAT_MODEL로 답변, agent = AGENT_MODEL이 도구로 기획서를 직접 찾아가며 답변 (agent.py)
ANSWER_MODE = os.environ.get("ANSWER_MODE", "classic")
AGENT_MODEL = os.environ.get("AGENT_MODEL", "gpt-6-luna")
AGENT_REASONING_EFFORT = os.environ.get("AGENT_REASONING_EFFORT", "medium")
# 에이전트가 답하기 전에 도구를 부를 수 있는 최대 횟수 (넘으면 모은 자료로 바로 답하게 한다)
AGENT_MAX_TOOL_ROUNDS = 8
# 해석자가 읽는 자료: original = 원본 청크, refined = 사서의 정리본 (원본은 read_original 도구로 연다)
AGENT_SOURCE = os.environ.get("AGENT_SOURCE", "original")
