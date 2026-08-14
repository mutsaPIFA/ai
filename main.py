"""MCM MUSE AI 서비스 — 내부 API (backend 전용, 외부 미노출).

구현: POST /cutout (rembg 누끼 — 키 불필요, 로컬 추론)
      POST /vision/tag (Gemini 태깅 — 무료 티어 가능)
      POST /vision/standardize (Gemini 상품컷 재생성 — billing 키 필요, 무료 할당 0 실측)
      POST /outfits/image (Gemini 코디 화보 — 누끼 여러 장 → flat-lay 연출컷 1장)

계약: docs/internal-api.md

핸들러는 전부 동기(def) — FastAPI가 threadpool에서 돌리므로, 블로킹 SDK 콜(Gemini·rembg)이
이벤트 루프를 막지 않는다(화보 생성 20초 동안 스캔 요청이 줄 서는 사고 방지).
"""

import json
import logging
import os
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import Response
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel
from rembg import new_session, remove

load_dotenv()

logger = logging.getLogger("uvicorn")

TAG_MODEL = os.environ.get("GEMINI_TAG_MODEL", "gemini-2.5-flash")
TEXT_MODEL = os.environ.get("GEMINI_TEXT_MODEL", "gemini-2.5-flash")
IMAGE_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")

# 고정 vocabulary — docs/api-v1.md 공통 타입. 여기서 벗어나면 backend가 저장하지 못한다.
CATEGORIES = ["상의", "하의", "아우터", "원피스", "신발", "가방", "악세서리"]
COLORS = ["블랙", "화이트", "네이비", "그레이", "베이지", "브라운", "카멜", "그린", "핑크", "기타"]
MATERIALS = ["면", "니트", "데님", "가죽", "실크", "울", "합성", "기타"]
MOODS = ["미니멀", "캐주얼", "클래식", "스트릿", "페미닌", "럭셔리"]

# 팀 프롬프트 리서치 최종본(2026-08-14) — MCM 상품 20개 category 정량 검증(85%, 오분류는 경계 모호 케이스뿐)
TAG_PROMPT = """첨부된 패션 아이템 이미지 1장을 분석하여 아래 기준에 따라 태깅하세요.

각 항목의 값은 반드시 아래에 제시된 후보 중 하나만 선택하세요.
후보에 명확하게 해당하지 않거나 이미지에서 판단하기 어려운 경우 "기타"를 선택하세요.

[태그 후보]

category:
상의 | 하의 | 아우터 | 원피스 | 신발 | 가방 | 악세서리

color:
블랙 | 화이트 | 네이비 | 그레이 | 베이지 | 브라운 | 카멜 | 그린 | 핑크 | 기타

material:
면 | 니트 | 데님 | 가죽 | 실크 | 울 | 합성 | 기타

mood:
미니멀 | 캐주얼 | 클래식 | 스트릿 | 페미닌 | 럭셔리

[판단 기준]

1. category
- 이미지에 나타난 실제 제품의 종류를 기준으로 가장 적합한 하나를 선택하세요.
- 티셔츠, 셔츠, 블라우스, 니트 등 상체에 착용하는 의류 → "상의"
- 팬츠, 스커트 등 하체에 착용하는 의류 → "하의"
- 재킷, 코트, 점퍼 등 다른 의류 위에 착용하는 겉옷 → "아우터"
- 상·하의가 하나로 연결된 드레스 형태의 의류 → "원피스"
- 스니커즈, 부츠, 로퍼 등 발에 착용하는 제품 → "신발"
- 백팩, 숄더백, 토트백, 크로스백 등 수납을 목적으로 휴대하는 가방류 → "가방"
- 지갑, 카드지갑, 벨트, 모자, 스카프, 주얼리 등 위 카테고리에 해당하지 않는 패션 소품 → "악세서리"

2. color
- 제품 전체에서 시각적으로 가장 지배적인 색상을 선택하세요.
- 여러 색상이 존재하더라도 가장 큰 비중을 차지하는 색상 하나를 선택하세요.

3. material
- 이미지에서 확인되는 표면 질감과 시각적 특징을 기준으로 가장 적합한 소재 하나를 선택하세요.
- 이미지에서 소재를 명확하게 판단하기 어려운 경우 "기타"를 선택하세요.

4. mood
- 색상 하나만으로 판단하지 말고 제품의 실루엣, 디자인, 소재, 패턴 등을 종합적으로 고려하여 가장 적합한 분위기 하나를 선택하세요.

- 이미지에서 명확하게 판단하기 어려운 정보를 임의로 추측하지 마세요.
- 반드시 제공된 후보 중에서만 값을 선택하세요.
- JSON 이외의 설명, 문장, 마크다운 코드블록을 출력하지 마세요.

반드시 아래 형식의 JSON 하나만 출력하세요.

{
"category": "후보 중 하나",
"color": "후보 중 하나",
"material": "후보 중 하나",
"mood": "후보 중 하나"
}"""

# 스키마 레벨에서 vocab을 강제 — 프롬프트 준수에 기대지 않는다.
TAG_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "category": types.Schema(type=types.Type.STRING, enum=CATEGORIES),
        "color": types.Schema(type=types.Type.STRING, enum=COLORS),
        "material": types.Schema(type=types.Type.STRING, enum=MATERIALS),
        "mood": types.Schema(type=types.Type.STRING, enum=MOODS),
    },
    required=["category", "color", "material", "mood"],
)

# 코디 화보 프롬프트 — 17회 실측 후 동결(v4). 장문(제약 나열)은 실행마다 다른 조항이 깨짐
# (의류 변형·소품 환각·가짜 브랜드 로고). 단문 + 상충 조건 한 문장이 안정 통과. 조정은 팀 시연 결과로만.
OUTFIT_PROMPT = (
    "첨부한 옷들을 한 장의 사진으로 연출해 줘.\n"
    "- 패션 매거진 스타일의 flat-lay: 옷들을 바닥에 자연스럽게 배치해 위에서 촬영한 구도\n"
    "- 각 아이템이 서로를 가리지 않고 전체 형태가 모두 보이게, 위아래 방향을 바르게(옆으로 눕히지 말 것) 배치하되, 넓은 여백 없이 화면을 균형 있게 꽉 채울 것\n"
    "- 첨부한 아이템은 하나도 빠짐없이 모두 포함하고, 새로운 옷·신발·가방·시계·안경 등 어떤 것도 추가하지 말 것\n"
    "- 각 아이템의 색상, 프린트, 형태, 카라·단추 같은 세부 디자인을 원본 그대로 유지할 것\n"
    "- 옷에 걸린 옷걸이는 지우고 옷만 표현할 것\n"
    "- 배경은 따뜻한 크림 베이지 톤의 부드러운 단색 배경, 은은한 자연광과 옅은 그림자"
)

# 팀 프롬프트 리서치 최종본(2026-08-14) — 전 카테고리 커버, 명암→배색 오인 방지 조항 포함.
# 알려진 한계: 단색 의류 + 큰 명암 차이는 프롬프트로 완전 제어 불가(촬영 가이드 UI로 보완).
STANDARDIZE_PROMPT = """이 사진 속 패션 아이템 한 개만 쇼핑몰 상품 사진으로 다시 만들어 줘.

- 순백색 배경의 정면 상품 사진으로 표현
- 의류(상의, 하의, 아우터, 원피스)는 나무 옷걸이에 자연스럽게 걸린 형태로 표현
- 신발, 가방, 액세서리는 제품 특성에 맞게 자연스럽게 놓인 정면 상품 사진으로 표현
- 원본의 아이템 종류, 전체 실루엣, 핏, 소매 길이와 기장을 정확히 유지
- 원본 의류의 실제 색상, 배색, 패턴, 프린트/로고의 문구와 위치, 소재 질감, 단추·지퍼·포켓 등 세부 디자인을 그대로 유지
- 조명, 그림자, 주름, 접힘, 반사로 인해 일시적으로 발생한 색상이나 명도 차이를 새로운 배색, 패턴 또는 다른 소재로 해석하지 말 것
- 단, 실제 아이템에 존재하는 배색, 컬러블록, 패턴 및 서로 다른 소재의 조합은 반드시 그대로 유지
- 구김이나 접힘은 자연스럽게 정리하되 원본의 형태와 디자인을 임의로 변경하지 말 것
- 원본 이미지에서 확인되지 않는 디자인이나 세부 요소를 추측하여 추가하지 말 것
- 사람, 손, 주변 사물 및 배경을 제거
- 스튜디오 소프트박스 조명을 사용한 실제 쇼핑몰 상품 사진처럼 표현"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    # u2net 모델 로드 (최초 실행이면 ~176MB 다운로드 후 ~/.u2net 에 캐시)
    t0 = time.time()
    app.state.rembg_session = new_session("u2net")
    logger.info("rembg u2net loaded in %.1fs", time.time() - t0)

    api_key = os.environ.get("GEMINI_API_KEY")
    app.state.gemini = genai.Client(api_key=api_key) if api_key else None
    if app.state.gemini is None:
        logger.warning("GEMINI_API_KEY 없음 — /vision/* 는 503을 반환한다 (/cutout은 정상)")
    yield


app = FastAPI(title="MCM MUSE AI Service", version="0.2.0", lifespan=lifespan)


def _sniff_mime(data: bytes) -> str:
    # backend(WebClient)는 octet-stream으로 보내므로 content-type 헤더는 못 믿는다.
    return "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"


def _read_image(image: UploadFile) -> bytes:
    data = image.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty image")
    return data


def _gemini(app: FastAPI) -> genai.Client:
    client = app.state.gemini
    if client is None:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY not configured")
    return client


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/cutout")
def cutout(image: UploadFile = File(...)):
    """배경 제거 — multipart 이미지 입력, image/png(투명 배경) 바이너리 응답.

    실측(CPU, 2000px 상품컷): 평균 1.7초/장.
    """
    data = _read_image(image)
    try:
        t0 = time.time()
        out = remove(data, session=app.state.rembg_session)
        logger.info("cutout %s: %d -> %d bytes in %.2fs",
                    image.filename, len(data), len(out), time.time() - t0)
    except Exception:  # noqa: BLE001 — 형식 오류 등은 전부 422로
        logger.exception("cutout failed for %s", image.filename)
        raise HTTPException(status_code=422, detail="cannot process image")
    return Response(content=out, media_type="image/png")


@app.post("/vision/tag")
def vision_tag(image: UploadFile = File(...)):
    """태깅 — 옷 사진 1장 → {category, color, material, mood} (vocab 값 보장).

    무료 티어 주의: 첫 콜 스로틀 ~79s 실측 — 호출부 타임아웃 여유 필요.
    """
    data = _read_image(image)
    client = _gemini(app)
    try:
        t0 = time.time()
        resp = client.models.generate_content(
            model=TAG_MODEL,
            contents=[TAG_PROMPT, types.Part.from_bytes(data=data, mime_type=_sniff_mime(data))],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=TAG_SCHEMA,
            ),
        )
        tags = json.loads(resp.text)
        logger.info("tag %s: %s in %.1fs", image.filename, tags, time.time() - t0)
    except genai_errors.APIError as e:
        logger.exception("tag failed for %s", image.filename)
        raise HTTPException(status_code=502, detail=f"gemini: {e.message}")
    except (json.JSONDecodeError, ValueError):
        logger.exception("tag parse failed for %s", image.filename)
        raise HTTPException(status_code=502, detail="gemini returned non-JSON response")
    # 스키마 enum이 강제하지만, 계약 위반은 backend가 아니라 여기서 잡는 게 맞다 — 이중 검증.
    valid = {"category": CATEGORIES, "color": COLORS, "material": MATERIALS, "mood": MOODS}
    for field, candidates in valid.items():
        if tags.get(field) not in candidates:
            raise HTTPException(status_code=502, detail=f"out-of-vocab {field}: {tags.get(field)!r}")
    return tags


@app.post("/vision/standardize")
def vision_standardize(image: UploadFile = File(...)):
    """상품컷 재생성 — 대충 찍은 옷 사진 → 옷걸이 정면 상품컷 (image/png 바이너리).

    billing 키 필요(무료 할당 0 실측). 키가 무료면 upstream 429 → 502로 표면화.
    """
    data = _read_image(image)
    client = _gemini(app)
    try:
        t0 = time.time()
        # 이미지 생성 모델이 간헐적으로 이미지 없이 텍스트만 반환한다(비결정성 실측) — 1회 재시도.
        for attempt in (1, 2):
            resp = client.models.generate_content(
                model=IMAGE_MODEL,
                contents=[STANDARDIZE_PROMPT,
                          types.Part.from_bytes(data=data, mime_type=_sniff_mime(data))],
            )
            parts = resp.candidates[0].content.parts if resp.candidates else []
            for part in parts or []:
                if part.inline_data:
                    logger.info("standardize %s: %d -> %d bytes in %.1fs (attempt %d)",
                                image.filename, len(data), len(part.inline_data.data),
                                time.time() - t0, attempt)
                    return Response(content=part.inline_data.data, media_type="image/png")
            text = " ".join(p.text for p in parts or [] if p.text)
            logger.warning("standardize %s attempt %d: no image, text=%r",
                           image.filename, attempt, text[:200])
        raise HTTPException(status_code=502, detail="gemini returned no image")
    except genai_errors.APIError as e:
        logger.exception("standardize failed for %s", image.filename)
        raise HTTPException(status_code=502, detail=f"gemini: {e.message}")


# ---------------------------------------------------------------------------
# 텍스트 LLM — 스타일 DNA·MCM 추천·코디 조합 (프롬프트 리서치의 Style DNA+Context 설계 반영)
# id는 backend가 DB 재검증하므로 환각 id가 섞여도 앱은 안 깨진다 — 그래도 스키마·프롬프트로 이중 방어.
# ---------------------------------------------------------------------------


class AiItem(BaseModel):
    id: int
    category: str
    color: str
    material: str
    mood: str


class AiProductIn(BaseModel):
    id: int
    name: str
    category: str
    color: str
    material: str


class StyleDnaRequest(BaseModel):
    items: list[AiItem]


class RecommendRequest(BaseModel):
    items: list[AiItem]
    products: list[AiProductIn]


class OutfitsRequest(BaseModel):
    mood: str
    items: list[AiItem]
    products: list[AiProductIn]


# 무드별 사전 정의 Context — LLM이 키워드↔태그의 의미 관계를 해석해 매칭한다
CONTEXTS = {
    "저녁 약속": "세련된, 깔끔한, 적당한 격식, 과하게 포멀하지 않은",
    "출근": "단정한, 프로페셔널한, 깔끔한, 신뢰감 있는",
    "출장": "단정한, 전문적인, 편안한, 이동하기 좋은",
    "데일리": "편안한, 자연스러운, 실용적인, 캐주얼한",
    "주말 산책": "편안한, 활동적인, 자연스러운, 여유로운",
    "파티": "화려한, 눈에 띄는, 개성 있는, 드레시한",
}

DNA_PROMPT = (
    "당신은 패션 스타일 분석가입니다. 사용자 옷장 아이템들의 태그 분포를 해석해 스타일 DNA를 만드세요.\n"
    "- summary: 사용자의 취향을 한국어 한 문장으로 요약 — 색과 무드의 경향을 자연스럽게 서술\n"
    "- dominantColors: 옷장에서 지배적인 색 1~2개 (후보 중에서만)\n"
    "- dominantMoods: 지배적인 무드 1~2개 (후보 중에서만)\n"
    "- keywords: 사용자 스타일을 표현하는 한국어 키워드 3개\n"
    "빈도만 세지 말고 아이템 간 조합과 경향을 해석하세요."
)

RECOMMEND_PROMPT = (
    "당신은 MCM 제품을 추천하는 AI 스타일 큐레이터입니다. "
    "사용자 옷장 아이템의 태그로 취향을 해석하고, 후보 MCM 제품 중 어울리는 것을 골라 추천하세요.\n"
    "1. 단순 태그 일치가 아니라 색상·소재·무드의 조화를 종합 판단하세요.\n"
    "2. 사용자 취향과 이어지는 제품을 우선하되, 취향을 한 단계 확장하는 제품도 포함하세요.\n"
    "3. 제공된 후보 목록의 id만 사용하세요 — 목록에 없는 제품을 만들지 마세요.\n"
    "4. reason은 한국어 한 문장 — 이 옷장과 왜 어울리는지 구체적으로.\n"
    "5. pairsWithItemIds: 함께 입으면 좋은 옷장 아이템 id 최대 2개.\n"
    "가장 추천하는 순서로 최대 5개."
)

OUTFITS_PROMPT = (
    "당신은 사용자의 실제 옷장과 MCM 제품을 함께 활용해 상황에 맞는 개인화 코디를 제안하는 AI 스타일 큐레이터입니다.\n"
    "[상황] {mood} — Context: {context}\n"
    "[스타일링 기준]\n"
    "1. 가장 먼저 상황과 Context에 적합한 코디인지 판단하세요.\n"
    "2. 그 범위 안에서 옷장 태그가 보여주는 사용자 취향을 반영하세요.\n"
    "3. 사용자 보유 아이템을 코디의 중심으로 하고, 각 코디에 MCM 제품을 정확히 1개 포함하세요.\n"
    "4. MCM 제품은 단순히 추가하지 말고 기존 옷과 조화되며 스타일을 확장하는 것으로 선택하세요.\n"
    "5. 색상, 소재, 무드, 격식도와 아이템 간 조화를 종합적으로 고려하세요.\n"
    "6. 카테고리와 착용 역할을 고려해 실제 착용 가능한 완성된 코디를 구성하세요 — 같은 역할의 아이템을 중복 선택하지 마세요.\n"
    "7. 제공된 id만 사용하세요 — 목록에 없는 아이템이나 제품을 만들지 마세요.\n"
    "8. 서로 다른 코디 3개를 제안하세요. 옷 조합이 다르면 같은 MCM 제품을 써도 다른 코디입니다. "
    "재료가 정말 부족할 때만 1~2개로 줄이세요.\n"
    "9. concept: 코디 컨셉명을 영어 2~3단어로 지으세요 (예: Refined Minimal).\n"
    "10. reason: 한국어 1~2문장 — 상황 적합성, 반영한 취향, MCM 제품이 더한 요소를 담으세요."
)

DNA_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "summary": types.Schema(type=types.Type.STRING),
        "dominantColors": types.Schema(
            type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING, enum=COLORS)),
        "dominantMoods": types.Schema(
            type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING, enum=MOODS)),
        "keywords": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING)),
    },
    required=["summary", "dominantColors", "dominantMoods", "keywords"],
)

RECOMMEND_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "picks": types.Schema(
            type=types.Type.ARRAY,
            items=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "productId": types.Schema(type=types.Type.INTEGER),
                    "reason": types.Schema(type=types.Type.STRING),
                    "pairsWithItemIds": types.Schema(
                        type=types.Type.ARRAY, items=types.Schema(type=types.Type.INTEGER)),
                },
                required=["productId", "reason", "pairsWithItemIds"],
            ),
        )
    },
    required=["picks"],
)

OUTFITS_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "looks": types.Schema(
            type=types.Type.ARRAY,
            items=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "concept": types.Schema(type=types.Type.STRING),
                    "closetItemIds": types.Schema(
                        type=types.Type.ARRAY, items=types.Schema(type=types.Type.INTEGER)),
                    "mcmProductId": types.Schema(type=types.Type.INTEGER),
                    "reason": types.Schema(type=types.Type.STRING),
                },
                required=["concept", "closetItemIds", "mcmProductId", "reason"],
            ),
        )
    },
    required=["looks"],
)


def _text_llm(prompt: str, payload: dict, schema: types.Schema) -> dict:
    client = _gemini(app)
    try:
        t0 = time.time()
        resp = client.models.generate_content(
            model=TEXT_MODEL,
            contents=[prompt, json.dumps(payload, ensure_ascii=False)],
            config=types.GenerateContentConfig(
                response_mime_type="application/json", response_schema=schema),
        )
        result = json.loads(resp.text)
        logger.info("text llm ok in %.1fs", time.time() - t0)
        return result
    except genai_errors.APIError as e:
        logger.exception("text llm failed")
        raise HTTPException(status_code=502, detail=f"gemini: {e.message}")
    except (json.JSONDecodeError, ValueError):
        logger.exception("text llm parse failed")
        raise HTTPException(status_code=502, detail="gemini returned non-JSON response")


@app.post("/style-dna")
def style_dna(req: StyleDnaRequest):
    """스타일 DNA — 옷장 태그 분포를 LLM이 해석. 응답 형태는 backend Recommender 포트와 1:1."""
    if not req.items:
        raise HTTPException(status_code=400, detail="empty items")
    return _text_llm(DNA_PROMPT, {"items": [i.model_dump() for i in req.items]}, DNA_SCHEMA)


@app.post("/recommend")
def recommend(req: RecommendRequest):
    """MCM 추천 — 옷장 취향 해석 후 후보 상품 중 최대 5개 (첫 번째가 bestPick)."""
    if not req.items or not req.products:
        raise HTTPException(status_code=400, detail="empty items or products")
    payload = {
        "items": [i.model_dump() for i in req.items],
        "products": [p.model_dump() for p in req.products],
    }
    return _text_llm(RECOMMEND_PROMPT, payload, RECOMMEND_SCHEMA)


@app.post("/outfits")
def outfits(req: OutfitsRequest):
    """코디 조합 — 상황 Context + 옷장 + MCM을 해석해 서로 다른 코디 최대 3개 (concept 영어 작명)."""
    if not req.products:
        raise HTTPException(status_code=400, detail="empty products")
    prompt = OUTFITS_PROMPT.format(
        mood=req.mood, context=CONTEXTS.get(req.mood, "상황에 자연스럽게 어울리는"))
    payload = {
        "items": [i.model_dump() for i in req.items],
        "products": [p.model_dump() for p in req.products],
    }
    return _text_llm(prompt, payload, OUTFITS_SCHEMA)


@app.post("/outfits/image")
def outfit_image(images: list[UploadFile] = File(...)):
    """코디 화보 — 아이템 누끼 여러 장 → flat-lay 연출컷 1장 (image/png 바이너리).

    실측: 4벌 기준 14~31초/장. 이미지 없이 텍스트만 반환하는 비결정성(2연속 실측) 대비 최대 2회 재시도 —
    재시도부터는 "이미지로만 응답" 지시를 덧붙인다(첫 시도 프롬프트는 동결 유지).
    """
    client = _gemini(app)
    parts: list = [OUTFIT_PROMPT]
    for f in images:
        data = _read_image(f)
        parts.append(types.Part.from_bytes(data=data, mime_type=_sniff_mime(data)))
    if len(parts) < 3:  # 프롬프트 + 최소 2벌
        raise HTTPException(status_code=400, detail="need at least 2 images")
    image_only_nudge = "텍스트 설명 없이, 완성된 flat-lay 이미지 한 장으로만 응답하세요."
    try:
        t0 = time.time()
        for attempt in (1, 2, 3):
            contents = parts if attempt == 1 else parts + [image_only_nudge]
            resp = client.models.generate_content(model=IMAGE_MODEL, contents=contents)
            cand_parts = resp.candidates[0].content.parts if resp.candidates else []
            for p in cand_parts or []:
                if p.inline_data:
                    logger.info("outfit image: %d items -> %d bytes in %.1fs (attempt %d)",
                                len(images), len(p.inline_data.data), time.time() - t0, attempt)
                    return Response(content=p.inline_data.data, media_type="image/png")
            text = " ".join(p.text for p in cand_parts or [] if p.text)
            logger.warning("outfit image attempt %d: no image, text=%r", attempt, text[:200])
        raise HTTPException(status_code=502, detail="gemini returned no image")
    except genai_errors.APIError as e:
        logger.exception("outfit image failed")
        raise HTTPException(status_code=502, detail=f"gemini: {e.message}")
