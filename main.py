from fastapi import FastAPI, Request, BackgroundTasks
import os
import threading
from dotenv import load_dotenv

# .env 파일의 환경변수를 읽어와 os.environ에 자동 등록
load_dotenv()

# 그 이후 모듈들 import
import ai_engine
import slack_service

app = FastAPI()

# --- [2] 서버 시작 시 슬랙 봇 비동기 가동 ---
@app.on_event("startup")
def startup_event():
    # 슬랙 소켓 모드는 별도 스레드로 실행하여 FastAPI 웹서버 차단을 방지합니다.
    slack_thread = threading.Thread(target=slack_service.start_slack_bot, daemon=True)
    slack_thread.start()

# --- [3] 노션 웹훅 수신부 ---
@app.post("/webhook/notion")
async def notion_webhook_upsert(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    page_id = payload.get("data", {}).get("id") or payload.get("source", {}).get("id")
    if page_id: 
        token = os.environ["NOTION_INTEGRATION_TOKEN"]
        background_tasks.add_task(ai_engine.sync_document_to_db, page_id, token)
    return {"status": "success"}

@app.post("/webhook/notion/delete")
async def notion_webhook_delete(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    page_id = payload.get("data", {}).get("id") or payload.get("source", {}).get("id")
    if page_id: 
        background_tasks.add_task(ai_engine.delete_document_from_db, page_id)
    return {"status": "success"}
