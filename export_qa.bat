@echo off
chcp 65001 > nul
rem 질문 기록을 엑셀 파일(CSV)로 내려받아 바로 엽니다. 더블클릭해서 실행하세요.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set LOG_LEVEL=WARNING

echo 질문 기록을 엑셀 파일로 내려받습니다.
echo.
set "DAYS="
set /p DAYS=최근 며칠 기록을 받을까요? (숫자 입력, 그냥 Enter = 전체 기록): 
echo.
rem 빈 입력이면 set /p가 오류 코드 1을 남기므로 초기화한다
ver > nul
if "%DAYS%"=="" (
    python export_qa.py --open
) else (
    python export_qa.py --days %DAYS% --open
)
if errorlevel 1 (
    echo.
    echo 내려받지 못했어요. 위의 오류 내용을 복사해서 알려 주세요.
)
echo.
pause
