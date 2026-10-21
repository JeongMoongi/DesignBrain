from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, BackgroundTasks, Depends, Header, HTTPException
import hmac
import logging
import threading
import config
import clients
import indexing
import slack_bot

logger = logging.getLogger(__name__)

# --- [2] 서버 시작 시 슬랙 봇 비동기 가동, 종료 시 연결 정리 ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.NOTION_WEBHOOK_SECRET:
        logger.warning("⚠️ NOTION_WEBHOOK_SECRET 미설정: 웹훅을 인증 없이 받습니다.")
    # 슬랙 스레드와 웹훅이 쓰기 전에 DB 연결/컬렉션 준비를 마친다
    clients.get_qdrant()
    # 슬랙 소켓 모드는 별도 스레드로 실행하여 FastAPI 웹서버 차단을 방지합니다.
    slack_thread = threading.Thread(target=slack_bot.start_slack_bot, daemon=True)
    slack_thread.start()
    yield
    slack_bot.stop_slack_bot()
    clients.close_clients()

app = FastAPI(lifespan=lifespan)

# --- [3] 노션 웹훅 수신부 ---
def verify_webhook_secret(x_webhook_secret: str = Header(default="")):
    """노션 자동화 웹훅은 서명을 제공하지 않으므로, 커스텀 헤더에 넣은 공유 시크릿으로 호출자를 확인한다."""
    if not config.NOTION_WEBHOOK_SECRET:
        return
    if not hmac.compare_digest(x_webhook_secret.encode(), config.NOTION_WEBHOOK_SECRET.encode()):
        logger.warning("🚫 웹훅 시크릿 불일치로 요청을 거부했습니다.")
        raise HTTPException(status_code=401, detail="invalid webhook secret")

def webhook_page_id(payload):
    return payload.get("data", {}).get("id") or payload.get("source", {}).get("id")

def webhook_requester(payload):
    """'완료'를 누른 사람의 노션 ID. 자동화가 보낸 사람을 알려 주지 않으면 페이지를 마지막으로 고친 사람으로 본다"""
    source, data = payload.get("source", {}), payload.get("data", {})
    return source.get("user_id") or data.get("last_edited_by", {}).get("id")

@app.post("/webhook/notion", dependencies=[Depends(verify_webhook_secret)])
async def notion_webhook_upsert(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    page_id = webhook_page_id(payload)
    if page_id:
        requested_by = webhook_requester(payload)
        logger.info("📥 [노션 웹훅] page_id: %s, 요청자: %s (source 키: %s)", page_id, requested_by, sorted(payload.get("source", {})))
        background_tasks.add_task(indexing.sync_document_to_db, page_id, config.NOTION_INTEGRATION_TOKEN, requested_by=requested_by)
    return {"status": "success"}

@app.post("/webhook/notion/delete", dependencies=[Depends(verify_webhook_secret)])
async def notion_webhook_delete(request: Request, background_tasks: BackgroundTasks):
    page_id = webhook_page_id(await request.json())
    if page_id: 
        background_tasks.add_task(indexing.delete_document_from_db, page_id)
    return {"status": "success"}
