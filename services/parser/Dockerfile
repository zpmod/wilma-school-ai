FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir fastapi==0.115.* uvicorn[standard]==0.32.* httpx==0.28.* pydantic==2.*

COPY app ./app

EXPOSE 8090

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
