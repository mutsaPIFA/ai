"""스파이크 1 — 스캔 표준화: 대충 찍은 옷 사진 → 쇼핑몰 상품컷 (Nano Banana).

사용법:
    set GEMINI_API_KEY=...          (AI Studio 무료 키, 카드 불필요)
    .venv/Scripts/pip install google-genai
    .venv/Scripts/python spike_standardize.py 사진1.jpg 사진2.jpg ...

판정 기준 (research-vision-llm.md §7):
    옷 종류·색 유지 + 상품컷 구도가 3/5 이상이면 채택.
결과는 같은 폴더에 *_std.png 로 저장된다.
"""

import os
import pathlib
import sys
import time

from google import genai
from google.genai import types

MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")

PROMPT = (
    "이 사진 속 '옷 한 벌'만 쇼핑몰 상품 사진으로 다시 만들어 줘. "
    "요구사항: 순백색 배경, 나무 옷걸이에 걸린 정면 샷, 조명은 스튜디오 소프트박스. "
    "옷의 종류(반팔/긴팔/반바지/긴바지 등), 색상, 프린트·로고의 문구와 위치, 소재 질감을 "
    "원본 그대로 유지할 것. 사람, 배경 사물, 손은 모두 제거."
)


def main() -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("GEMINI_API_KEY 환경변수를 설정하세요 (AI Studio에서 무료 발급)")
    if len(sys.argv) < 2:
        sys.exit("사용법: python spike_standardize.py <옷사진.jpg> [...]")

    client = genai.Client(api_key=api_key)

    for arg in sys.argv[1:]:
        path = pathlib.Path(arg)
        data = path.read_bytes()
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"

        t0 = time.time()
        resp = client.models.generate_content(
            model=MODEL,
            contents=[PROMPT, types.Part.from_bytes(data=data, mime_type=mime)],
        )
        elapsed = time.time() - t0

        saved = False
        for part in resp.candidates[0].content.parts:
            if part.inline_data:
                out = path.with_name(path.stem + "_std.png")
                out.write_bytes(part.inline_data.data)
                print(f"✅ {path.name} → {out.name}  ({elapsed:.1f}s, {len(part.inline_data.data)//1024}KB)")
                saved = True
            elif part.text:
                print(f"   [text] {part.text[:120]}")
        if not saved:
            print(f"❌ {path.name}: 이미지가 반환되지 않음 ({elapsed:.1f}s)")


if __name__ == "__main__":
    main()
