FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# u2net 모델(~176MB)을 이미지에 미리 다운로드 — 컨테이너 시작 시 콜드스타트 방지
RUN python -c "from rembg import new_session; new_session('u2net')"

COPY main.py .

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
