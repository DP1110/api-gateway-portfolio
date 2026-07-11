# ---------------------------------------------------------------------------
# Dockerfile for the API Gateway
# ---------------------------------------------------------------------------
# We use a multi-stage-friendly slim image. All steps use the same image;
# behaviour is controlled purely through environment variables.

FROM python:3.11-slim

# Don't write .pyc files; don't buffer stdout/stderr (essential for logs)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (layer-cached separately from source)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY gateway/ ./gateway/
COPY mock_backends/ ./mock_backends/

# Expose gateway port
EXPOSE 8000

# Default entrypoint runs the gateway; mock backends use CMD override
CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
