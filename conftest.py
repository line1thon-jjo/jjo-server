"""pytest 전체 공통 설정. app 을 불러오기 전에 읽힌다."""
import os

# 테스트가 서버를 띄울 때(with TestClient(app)) 음성 분석 모델을 내려받거나 올리지 않게 한다.
os.environ["SPEECH_WARM_UP"] = "false"
