#!/usr/bin/env bash
# 배포 서버에서 실행: main 최신 코드 받기 → 패키지 설치 → DB 마이그레이션 → 서버 재시작 → 상태 확인.
# 서버에는 홈 폴더의 jjo-deploy.sh 로 복사해 두고 쓴다. 이 파일을 고치면 서버 복사본도 직접 바꿔야 한다.
# GitHub Actions 의 배포 전용 키는 서버에서 그 복사본만 실행할 수 있게 막혀 있다(authorized_keys 의 command=).
# 재시작하는 순간 진행 중이던 분석은 끊겨 FAILED 가 된다. 시연 중에는 main 머지를 피한다.
#
# 재시작 방법은 서버마다 다르다.
# - GPU 서버(도커 컨테이너, systemd 없음): 홈의 jjo-start.sh 가 패키지·설정을 되살리고 tmux 로 다시 띄운다
# - AWS 서버(systemd): systemctl restart jjo
set -euo pipefail

APP_DIR="$HOME/jjo-server"
cd "$APP_DIR"

echo "== 코드 받기"
git fetch -q origin main
git merge -q --ff-only origin/main  # 서버에서 코드를 직접 고쳐 갈라졌으면 여기서 멈춘다
git log --oneline -1

echo "== 패키지 설치"
.venv/bin/pip install -q -e .

echo "== DB 마이그레이션"
.venv/bin/alembic upgrade head

echo "== 서버 재시작"
if [ -x "$HOME/jjo-start.sh" ]; then
    "$HOME/jjo-start.sh"
else
    sudo systemctl restart jjo
fi

echo "== 상태 확인"
for _ in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:8000/health 2>/dev/null; then  # 재시작 직후 몇 초는 연결이 안 되는 게 정상
        echo
        echo "배포 완료"
        exit 0
    fi
    sleep 2
done
echo "서버가 응답하지 않음. 최근 로그:"
if [ -f "$HOME/jjo.log" ]; then tail -40 "$HOME/jjo.log"; else sudo journalctl -u jjo -n 40 --no-pager; fi
exit 1
