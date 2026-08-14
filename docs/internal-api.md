# AI 서비스 내부 API — 초안

> 🚧 **초안입니다.** backend 쪽에서 연동 지점을 먼저 잡아두려고 작성했습니다.
> **AI 담당(시은)이 이어받아 확정**해 주세요. 아래 스펙은 제안일 뿐이고, 실제 구현에 맞게 자유롭게 바꿔도 됩니다.
> 바뀌면 backend의 `shared/aiclient` 구현만 고치면 되므로 **공개 계약(`docs/api-v1.md`)에는 영향이 없습니다.**

## 성격

- 공개 API(`/api/v1`)와 **완전히 별개**. **backend만 호출**한다(내부망, 외부 노출 없음).
- 그래서 인증·CORS·에러 형식을 공개 계약과 맞출 필요가 없다. 편한 대로.
- 스택: FastAPI + uvicorn · `rembg`(누끼) · `google-genai`(Gemini). Dockerfile 포함.

## backend가 기대하는 연동 방식

backend는 AI 기능을 **포트(인터페이스)** 로 두고 `mock` / `http` 두 구현을 스위치한다
(`app.ai.mode=mock|http`). 그래서 **AI 서비스나 Gemini 키가 없어도 backend·프론트 개발이 멈추지 않는다.**

| backend 포트 | 하는 일 | 대응 엔드포인트 |
|---|---|---|
| `VisionTagger` | 사진 → 태그 | `POST /vision/tag` |
| `BackgroundRemover` | 누끼 | `POST /cutout` |
| `Recommender` | DNA·추천 | `POST /style-dna`, `POST /recommend` |
| `OutfitComposer` | 코디 후보 | `POST /outfits` |
| `OutfitImageGenerator` | 코디 화보 1장 | `POST /outfits/image` |

## 엔드포인트 초안

```
POST /cutout         (multipart image)        → image/png 바이너리 (투명 배경)   # rembg ✅ 구현됨
POST /vision/standardize (image)              → image/png 바이너리 (상품컷)      # ✅ 구현됨 — billing 키 대기 ★스캔 품질의 핵심
POST /vision/tag     (image)                  → {category, color, material, mood} # ✅ 구현됨 — vocab을 응답 스키마 enum으로 강제
POST /outfits/image  (multipart images[])     → image/png 바이너리 (flat-lay 화보) # ✅ 구현됨 — 코디 후보마다 1장, 실측 14~31s
POST /style-dna      {items}                  → {summary, dominantColors, dominantMoods, keywords} # ✅ 구현됨 — 텍스트 LLM, enum 스키마 강제
POST /recommend      {items, products}        → {picks:[{productId, reason, pairsWithItemIds}]}    # ✅ 구현됨 — 최대 5, 첫 번째가 bestPick
POST /outfits        {mood, items, products}  → {looks:[{concept, closetItemIds, mcmProductId, reason}]} # ✅ 구현됨 — 상황 Context 매핑 내장, concept=영어 작명
```

- 텍스트 LLM 3종은 프롬프트 리서치의 **Style DNA + Context 설계**를 반영: 무드 라벨→Context 키워드 매핑을 서비스가 보유(6종), LLM이 키워드↔태그의 의미 관계를 해석해 조합. 태그 체계 확장(Style/Silhouette 등 5축)은 v2 백로그.
- id는 backend가 DB 재검증하고, LLM 실패 시 backend가 룰베이스로 런타임 폴백한다 — 이 서비스는 품질만 책임지면 된다.

### 스캔 파이프라인 — `/standardize`가 품질의 핵심

사용자는 옷을 **대충 찍는다**(입은 채로, 침대 위에서). 참고앱(Dress AI)은 이런 사진을 **쇼핑몰 상품컷으로 재생성**한다 — 배경 제거가 아니라 생성형 이미지 모델의 몫이다.

```
사용자 사진 → ① /standardize (Gemini 재생성: 옷걸이에 걸린 정면 상품컷)
           → ② /cutout (rembg: 흰 배경 제거 → 투명 PNG)
           → ③ /vision/tag
```

- ①이 없으면 ②만으로 폴백 가능(구겨진 채 누끼) — 동작은 하지만 품질이 확 떨어진다.
- 재생성 특성상 디테일 오류(반바지→긴바지, 프린트 글자 변형)가 생길 수 있다 — UX상 재스캔으로 흡수(계약에 이미 있음).
- **Gemini 키 확보 후 1순위 스파이크가 이 프롬프트다** (룩 이미지 생성보다 먼저 — 스캔은 모든 옷이 거치는 관문).

### `/cutout` — 구현·검증 완료 (2026-08-13)

- 입력: multipart `image` (jpg/png) · 출력: **`image/png` 바이너리**(투명 배경) · 실패 시 `422`
- URL 반환이 아니라 바이너리로 확정 — 저장은 backend(StorageService)가 담당하므로 AI 서비스는 무상태.
- 실측(CPU, 2000px 상품컷): **평균 1.74초/장**, 모델 로드는 시작 시 1회(~11초). Gemini와 무관, API 키 불필요.

### 태그 값은 고정 vocabulary를 지켜야 함

`POST /vision/tag` 응답은 아래 값 중 하나여야 한다. **여기서 벗어난 값이 오면 backend가 저장하지 못한다.**
구현은 프롬프트 준수에 기대지 않고 **Gemini structured output의 응답 스키마에 vocab을 enum으로 강제**한다
(+ 서비스에서 이중 검증 — 벗어나면 502). Gemini 키가 없으면 `/vision/*`만 503, `/cutout`은 정상.

```
category : 상의 | 하의 | 아우터 | 원피스 | 신발 | 가방 | 악세서리
color    : 블랙 | 화이트 | 네이비 | 그레이 | 베이지 | 브라운 | 카멜 | 그린 | 핑크 | 기타
material : 면 | 니트 | 데님 | 가죽 | 실크 | 울 | 합성 | 기타
mood     : 미니멀 | 캐주얼 | 클래식 | 스트릿 | 페미닌 | 럭셔리
```

(출처: 팀 공용 [`docs/api-v1.md`](https://github.com/mutsaPIFA/docs/blob/main/api-v1.md) 공통 타입)

### id는 backend가 재검증한다 — 환각 걱정 안 해도 됨

`/recommend`·`/outfits`가 돌려주는 `mcmProductId`·`closetItemId`는 **backend가 DB로 재조회해서 실재하는 것만** 공개 응답에 싣는다. 존재하지 않는 id가 섞여도 앱이 깨지지는 않는다(해당 항목이 빠질 뿐).

다만 **추천 품질을 위해** 프롬프트에는 실재 데이터만 넣는 게 좋다.

### `/outfits` 제약

- 각 코디는 **MCM 제품을 정확히 1개** 포함해야 한다. MCM 없는 코디는 이 서비스의 코디가 아니다.
- 후보는 **최대 3개**. 재료가 부족하면 1~2개여도 된다.
- `seed`(= `seedMcmProductId`)가 오면 그 제품을 고정한 채 조합한다.

### `/outfits/image` — 코디 화보 (후보마다 1장)

- 입력: multipart `images` 여러 장(아이템 누끼들, MCM 포함) · 출력: **image/png 바이너리**(flat-lay 연출컷)
- backend가 **코디 후보(§공개계약 4-4)마다 병렬 호출**한다 — 실측 14~31초/장. 저장(4-5)은 후보 화보를 재사용하므로 재호출 없음.
- 프롬프트는 17회 실측 후 동결(장문은 환각·변형 실패, 단문 채택) — 조정은 팀 시연 결과 기반으로만.
- 이미지 없이 텍스트만 반환하는 비결정성 대비 1회 재시도 내장. 그래도 실패면 502 — backend는 해당 후보만 imageUrl=null 처리.

## 열린 항목

- Gemini API 키·모델명·쿼터 확정
- ~~`/cutout` 응답 바이너리 vs URL~~ → **바이너리로 확정** (backend가 저장 담당, AI 서비스는 무상태)
- 이미지 입력 형식·최대 크기 (jpg/png, iPhone HEIC 변환은 프론트가 처리)
- 사용자 폰 사진(복잡한 배경) 누끼 품질 확인 — 현재 검증은 흰 배경 상품컷 기준
- MCM 상품 누끼는 **backend 시드 적재 시** 이 `/cutout`을 호출해 처리
