FROM python:3.11-slim

RUN pip install --no-cache-dir uv

WORKDIR /code

# Pre-install dependencies to cache this layer
RUN uv pip install --system \
    "fastapi>=0.110.0" \
    "uvicorn>=0.29.0" \
    "google-adk[gcp]>=2.0.0,<3.0.0" \
    "google-cloud-aiplatform>=1.156.0" \
    "httpx>=0.27.0"

COPY ./main.py ./

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
