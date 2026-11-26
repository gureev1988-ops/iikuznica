FROM python:3.12-slim
WORKDIR /app
COPY requirements-backend.txt .
RUN pip install --no-cache-dir -r requirements-backend.txt && useradd --create-home --uid 10001 ii
COPY backend/ backend/
USER ii
ENTRYPOINT ["python", "-m", "backend"]
CMD ["api", "--host", "0.0.0.0"]
