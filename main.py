from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, BackgroundTasks
import threading
import config
import ai_engine
import slack_service

# --- [2] 서버 시작 시 슬랙 봇 비동기 가동, 종료 시 연결 정리 ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 슬랙 스레드와 웹훅이 쓰기 전에 DB 연결/컬렉션 준비를 마친다
    ai_engine.get_qdrant()
    # 슬랙 소켓 모드는 별도 스레드로 실행하여 FastAPI 웹서버 차단을 방지합니다.
    slack_thread = threading.Thread(target=slack_service.start_slack_bot, daemon=True)
    slack_thread.start()
    yield
    slack_service.stop_slack_bot()
    ai_engine.close_clients()

app = FastAPI(lifespan=lifespan)

# --- [3] 노션 웹훅 수신부 ---
@app.post("/webhook/notion")
async def notion_webhook_upsert(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    page_id = payload.get("data", {}).get("id") or payload.get("source", {}).get("id")
    if page_id: 
        background_tasks.add_task(ai_engine.sync_document_to_db, page_id, config.NOTION_INTEGRATION_TOKEN)
    return {"status": "success"}

@app.post("/webhook/notion/delete")
async def notion_webhook_delete(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    page_id = payload.get("data", {}).get("id") or payload.get("source", {}).get("id")
    if page_id: 
        background_tasks.add_task(ai_engine.delete_document_from_db, page_id)
    return {"status": "success"}
