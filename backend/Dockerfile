FROM python:3.12-slim
WORKDIR /app
COPY requirements-backend.txt .
RUN --mount=type=secret,id=pip_ca \
    if [ -f /run/secrets/pip_ca ]; then export PIP_CERT=/run/secrets/pip_ca; fi && \
    pip install --no-cache-dir -r requirements-backend.txt && useradd --create-home --uid 10001 ii
RUN install -d -m 700 -o ii -g ii /app/.ii-local
COPY --chown=ii:ii backend/ backend/
USER ii
ENTRYPOINT ["python", "-m", "backend"]
CMD ["api", "--host", "0.0.0.0"]
