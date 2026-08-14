# MCM MUSE — ai

AI 서비스 (Python FastAPI). **backend만 호출하는 내부 API** — 외부 노출 없음.

| 기능 | 상태 | 기술 |
|------|------|------|
| 누끼 (배경 제거) `POST /cutout` | ✅ 구현·검증됨 | rembg (로컬 추론, **API 키 불필요**) |
| 비전 태깅 `POST /vision/tag` | ✅ 구현·검증됨 | Gemini 2.5 Flash (**무료 티어 가능**, vocab을 응답 스키마 enum으로 강제) |
| 스캔 표준화 `POST /vision/standardize` | ✅ 구현됨 — **billing 키 대기** | Gemini 이미지 생성 (무료 할당 0) |
| 코디 화보 `POST /outfits/image` | ✅ 구현·검증됨 | Gemini 이미지 생성 (동결 프롬프트, 14~31s/장) |
| 스타일 DNA·추천 `POST /style-dna` `POST /recommend` | ✅ 구현·검증됨 | Gemini 텍스트 (무료 티어 가능, 스키마 강제) |
| 코디 조합 `POST /outfits` | ✅ 구현·검증됨 | Gemini 텍스트 (무드→Context 매핑, concept 영어 작명) |

API 정의 → [`docs/internal-api.md`](docs/internal-api.md)

## 실행

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt     # (mac/linux: .venv/bin/pip)
cp .env.example .env                              # GEMINI_API_KEY 채우기 (누끼만 쓰면 생략 가능)
.venv/Scripts/python -m uvicorn main:app --port 8000
```

- 최초 실행 시 u2net 모델(~176MB)을 자동 다운로드해 `~/.u2net`에 캐시한다.
- 확인: `curl http://localhost:8000/health` → `{"status":"ok"}`
- 누끼: `curl -F "image=@photo.jpg" -o cutout.png http://localhost:8000/cutout`
- 태깅: `curl -F "image=@photo.jpg" http://localhost:8000/vision/tag` → `{"category":"상의", ...}`
- 키 없이 띄우면 `/vision/*`만 503, `/cutout`은 정상 동작.

Docker:

```bash
docker build -t mcmmuse-ai .
docker run -p 8000:8000 mcmmuse-ai
```

## 실측 성능 (2026-08-13, CPU)

MCM 공식 상품컷(2000px) 5장 — 가방/지갑/악세서리/신발/의류 각 1장:

| 항목 | 값 |
|------|-----|
| 이미지당 처리 | **평균 1.74초** (1.4~2.3초) |
| 모델 로드 | 서비스 시작 시 1회 ~11초 (Docker는 이미지에 미리 포함) |
| 품질 | 가방 체인 디테일·셔츠 실루엣까지 깨끗한 투명 배경 PNG |

⚠️ 위는 흰 배경 스튜디오 컷 기준. 사용자 폰 사진(복잡한 배경)은 별도 확인 필요.
