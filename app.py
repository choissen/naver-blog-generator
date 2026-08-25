"""
네이버 블로그용 AI 제목 + 본문 초안 생성기
- Google Gemini API(무료 티어)로 제목/본문 생성
- Pixabay API(무료)로 저작권 프리 이미지 검색
- 생성 결과를 로컬에 저장하고 다시 불러보는 히스토리 기능
- 생성된 글은 반드시 사람이 검토/수정 후 직접 게시할 것을 권장 (자동 게시 기능 없음)
"""
import json
import os
import re
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "").strip()

client = None
if GEMINI_API_KEY:
    from google import genai  # 지연 임포트: 키 없이도 서버는 뜨게 함

    client = genai.Client(api_key=GEMINI_API_KEY)

app = Flask(__name__)

MODEL_NAME = "gemini-3.6-flash"

OUTPUTS_DIR = Path(__file__).parent / "outputs"
OUTPUTS_DIR.mkdir(exist_ok=True)

TITLE_PROMPT = """당신은 네이버 블로그 제목을 잘 뽑는 카피라이터입니다.
아래 주제로 클릭을 유도하는(후킹) 블로그 제목 후보 5개를 만들어주세요.

조건:
- 각 제목은 25자 내외
- 과장된 허위 정보나 낚시성 거짓 주장은 넣지 말 것
- 숫자, 궁금증 유발, 공감 표현 중 1~2가지를 활용
- 번호를 매겨 한 줄에 하나씩 출력

주제: {topic}
톤/느낌: {tone}
"""

BODY_PROMPT = """당신은 네이버 블로그에 경험담 스타일의 글을 쓰는 블로거입니다.
아래 주제로 사람이 직접 겪은 것처럼 자연스러운 블로그 본문 초안을 작성해주세요.

조건:
- 분량은 800~1200자 내외
- 도입(공감/문제 제기) - 본론(경험/정보) - 마무리(요약/제안) 구조
- 과장되거나 검증되지 않은 수치·효능은 쓰지 말 것
- 실제로 겪은 것처럼 구체적인 디테일(장소, 상황, 감정)을 1~2개 포함
- 문단 사이 줄바꿈으로 가독성 확보
- 이 글은 초안이며, 사실관계는 작성자가 직접 확인 후 게시해야 함을 전제로 작성

주제: {topic}
톤/느낌: {tone}
"""

KEYWORD_PROMPT = """아래 한국어 블로그 주제를 이미지 스톡 사이트에서 검색하기 좋은
영어 키워드 2~3개로 변환해주세요. 쉼표로만 구분하고, 다른 설명 없이 키워드만 출력하세요.

주제: {topic}
"""


def generate_with_retry(prompt, retries=3, delay=2):
    """Gemini 호출 시 일시적인 과부하(503) 오류에 대해 짧게 재시도한다."""
    last_err = None
    for attempt in range(retries):
        try:
            return client.models.generate_content(model=MODEL_NAME, contents=prompt)
        except Exception as e:  # noqa: BLE001 - SDK 예외를 폭넓게 잡아 재시도 판단
            last_err = e
            msg = str(e)
            if "503" in msg or "UNAVAILABLE" in msg or "overloaded" in msg.lower():
                time.sleep(delay * (attempt + 1))
                continue
            raise
    raise last_err


def slugify_topic(topic: str, max_len: int = 20) -> str:
    """파일명으로 쓸 수 있도록 주제 문자열을 정리한다."""
    cleaned = re.sub(r"[^\w\s가-힣-]", "", topic).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned[:max_len] or "untitled"


@app.route("/")
def index():
    return render_template(
        "index.html",
        has_key=bool(GEMINI_API_KEY),
        has_image_key=bool(PIXABAY_API_KEY),
    )


@app.route("/generate", methods=["POST"])
def generate():
    if not GEMINI_API_KEY or client is None:
        return (
            jsonify(
                {
                    "error": "GEMINI_API_KEY가 설정되지 않았습니다. "
                    ".env 파일에 키를 넣고 서버를 재시작해주세요."
                }
            ),
            500,
        )

    data = request.get_json(silent=True) or {}
    topic = (data.get("topic") or "").strip()
    tone = (data.get("tone") or "친근하고 담백한 경험담 느낌").strip()

    if not topic:
        return jsonify({"error": "주제를 입력해주세요."}), 400

    try:
        titles_resp = generate_with_retry(TITLE_PROMPT.format(topic=topic, tone=tone))
        body_resp = generate_with_retry(BODY_PROMPT.format(topic=topic, tone=tone))
    except Exception as e:  # Gemini SDK가 던지는 다양한 예외를 사용자에게 그대로 안내
        return jsonify({"error": f"생성 중 오류가 발생했습니다: {e}"}), 500

    return jsonify(
        {
            "titles": (titles_resp.text or "").strip(),
            "body": (body_resp.text or "").strip(),
        }
    )


@app.route("/search-images", methods=["POST"])
def search_images():
    if not PIXABAY_API_KEY:
        return (
            jsonify(
                {
                    "error": "PIXABAY_API_KEY가 설정되지 않았습니다. "
                    ".env 파일에 키를 넣고 서버를 재시작해주세요."
                }
            ),
            500,
        )

    data = request.get_json(silent=True) or {}
    topic = (data.get("topic") or "").strip()
    if not topic:
        return jsonify({"error": "주제를 입력해주세요."}), 400

    # 한국어 주제를 영어 키워드로 변환해서 검색 정확도를 높인다 (Gemini 키가 있을 때만)
    query = topic
    if client is not None:
        try:
            kw_resp = generate_with_retry(KEYWORD_PROMPT.format(topic=topic))
            keywords = (kw_resp.text or "").strip()
            if keywords:
                query = keywords
        except Exception:
            pass  # 키워드 변환 실패해도 원문 주제로 검색 계속 진행

    try:
        res = requests.get(
            "https://pixabay.com/api/",
            params={
                "key": PIXABAY_API_KEY,
                "q": query,
                "image_type": "photo",
                "safesearch": "true",
                "per_page": 12,
            },
            timeout=10,
        )
        res.raise_for_status()
        payload = res.json()
    except Exception as e:
        return jsonify({"error": f"이미지 검색 중 오류가 발생했습니다: {e}"}), 500

    images = [
        {
            "thumb": hit["webformatURL"],
            "full": hit["largeImageURL"],
            "page": hit["pageURL"],
            "user": hit["user"],
        }
        for hit in payload.get("hits", [])
    ]

    return jsonify({"query": query, "images": images})


@app.route("/save", methods=["POST"])
def save_draft():
    data = request.get_json(silent=True) or {}
    topic = (data.get("topic") or "").strip()
    tone = (data.get("tone") or "").strip()
    titles = (data.get("titles") or "").strip()
    body = (data.get("body") or "").strip()

    if not topic or not (titles or body):
        return jsonify({"error": "저장할 내용이 없습니다."}), 400

    timestamp = time.strftime("%Y-%m-%d_%H%M%S")
    draft_id = f"{timestamp}_{slugify_topic(topic)}"
    record = {
        "id": draft_id,
        "topic": topic,
        "tone": tone,
        "titles": titles,
        "body": body,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    path = OUTPUTS_DIR / f"{draft_id}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    return jsonify({"ok": True, "id": draft_id})


@app.route("/history", methods=["GET"])
def list_history():
    records = []
    for path in OUTPUTS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        records.append(
            {
                "id": data.get("id", path.stem),
                "topic": data.get("topic", ""),
                "created_at": data.get("created_at", ""),
            }
        )
    records.sort(key=lambda r: r["created_at"], reverse=True)
    return jsonify({"items": records})


@app.route("/history/<draft_id>", methods=["GET"])
def get_history_item(draft_id):
    # 경로 조작 방지: 슬래시/역슬래시가 섞인 id는 거부
    if "/" in draft_id or "\\" in draft_id or ".." in draft_id:
        return jsonify({"error": "잘못된 요청입니다."}), 400

    path = OUTPUTS_DIR / f"{draft_id}.json"
    if not path.exists():
        return jsonify({"error": "해당 초안을 찾을 수 없습니다."}), 404

    data = json.loads(path.read_text(encoding="utf-8"))
    return jsonify(data)


@app.route("/history/<draft_id>", methods=["DELETE"])
def delete_history_item(draft_id):
    if "/" in draft_id or "\\" in draft_id or ".." in draft_id:
        return jsonify({"error": "잘못된 요청입니다."}), 400

    path = OUTPUTS_DIR / f"{draft_id}.json"
    if path.exists():
        path.unlink()
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
