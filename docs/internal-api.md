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
| `LookImageGenerator` | 룩 이미지 1장 | `POST /looks/image` |

## 엔드포인트 초안

```
POST /vision/tag     (image)                  → {category, color, material, mood}
POST /cutout         (image)                  → {cutoutImage | url}          # rembg
POST /style-dna      {items:[...]}            → {summary, dominantColors, dominantMoods, keywords}
POST /recommend      {items:[...]}            → {bestPick, more:[...]}       # 후보 id 제안
POST /outfits        {moodId, closet, seed?}  → [{closetItemIds, mcmProductId, reason}, ...]
POST /looks/image    {items, mcm}             → {imageUrl}                   # Gemini, 저장 시 1장만
```

### 태그 값은 고정 vocabulary를 지켜야 함

`POST /vision/tag` 응답은 아래 값 중 하나여야 한다. **여기서 벗어난 값이 오면 backend가 저장하지 못한다.**

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

### `/looks/image` 는 느려도 된다

backend가 **비동기로 호출**한다. 사용자는 저장 완료 화면을 먼저 보고, 이미지는 준비되면 교체된다.
수십 초 걸려도 UX가 깨지지 않으니 **속도보다 품질** 우선.

## 열린 항목

- Gemini API 키·모델명·쿼터 확정
- `/cutout` 응답을 **바이너리로 줄지 URL로 줄지** — backend가 저장을 담당하므로 바이너리가 단순할 수 있음
- 이미지 입력 형식·최대 크기 (jpg/png, iPhone HEIC 변환은 프론트가 처리)
- MCM 상품 누끼는 **backend 시드 적재 시** 처리 예정 — AI 서비스의 `/cutout`을 재사용할지 별도로 돌릴지
