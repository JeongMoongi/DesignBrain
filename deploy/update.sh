#!/usr/bin/env bash
# GitHub의 최신 코드로 서버를 갱신한다. VM에서 실행:  bash /opt/designbrain/deploy/update.sh
set -euo pipefail
APP_DIR=/opt/designbrain

cd "$APP_DIR"
git pull --ff-only
"$APP_DIR/.venv/bin/pip" install -q -r requirements.txt
sudo systemctl restart designbrain
echo "갱신 완료: $(git log --oneline -1)"
echo "로그 확인: journalctl -u designbrain -f"
