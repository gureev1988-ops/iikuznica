FROM python:3.12-slim
WORKDIR /app
COPY requirements-backend.txt .
RUN pip install --no-cache-dir -r requirements-backend.txt && useradd --create-home --uid 10001 ii
RUN install -d -m 700 -o ii -g ii /app/.ii-local
COPY --chown=ii:ii backend/ backend/
USER ii
ENTRYPOINT ["python", "-m", "backend"]
CMD ["api", "--host", "0.0.0.0"]
