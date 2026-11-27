FROM python:3.12-slim-bookworm@sha256:44cf1793efa7c7a476bb0f2cff81443f4a072c90c1cb127e2924933daa3e8c64
WORKDIR /srv/decider
COPY requirements.txt requirements.lock ./
RUN --mount=type=secret,id=pip_ca \
    if [ -f /run/secrets/pip_ca ]; then export PIP_CERT=/run/secrets/pip_ca; fi && \
    python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements.lock -r requirements.txt
COPY --chmod=0444 server.py .
ENV PYTHONPATH=/models/decider/source/src \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_OFFLINE=1 \
    HF_HOME=/tmp/huggingface \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility
USER 65532:65532
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
