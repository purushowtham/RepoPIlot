FROM docker:27-cli AS dockercli
FROM python:3.11.14-slim
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
COPY demo_repo/ /demo_repo/
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
