# migrate_to_firestore.py
import os
import json
from google.oauth2 import service_account
from google.cloud import firestore

# SERVICE_ACCOUNT_JSON 환경변수에서 서비스 계정 키 파일 경로를 가져옵니다.
SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON", "service_account.json")

def load_json(path, default):
    """로컬 JSON 파일을 읽어서 dict로 돌려줍니다. 없으면 default를 돌려줍니다."""
    if not os.path.exists(path):
        print(f"[경고] {path} 파일이 없어서 기본값을 사용합니다.")
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[에러] {path} 로드 실패: {e}")
        return default

def main():
    # 1) 서비스 계정으로 인증
    print("[1] 서비스 계정으로 Firestore 클라이언트 만들기...")
    creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_JSON)
    client = firestore.Client(credentials=creds, project=creds.project_id)
    print(f"    프로젝트: {creds.project_id}")

    # 2) 로컬 JSON 파일 읽기
    print("[2] 로컬 JSON 파일 읽는 중...")
    overrides_data = load_json("overrides.json", {})
    attendance_data = load_json("attendance.json", {})
    homework_data = load_json("homework.json", {})

    # 3) Firestore에 업로드
    print("[3] Firestore에 업로드하는 중... (컬렉션: persist)")
    client.collection("persist").document("overrides").set(overrides_data)
    print("    overrides 업로드 완료")

    client.collection("persist").document("attendance").set(attendance_data)
    print("    attendance 업로드 완료")

    client.collection("persist").document("homework").set(homework_data)
    print("    homework 업로드 완료")

    print("[완료] Migration done ✅")

if __name__ == "__main__":
    main()
