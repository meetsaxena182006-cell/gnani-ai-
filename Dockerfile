# Cloud Run image:  gcloud run deploy saarthi-ai --source . --region asia-south1 --allow-unauthenticated \
#                     --set-env-vars USE_VERTEX=1,GOOGLE_CLOUD_PROJECT=saarthi-ai-os,GOOGLE_CLOUD_LOCATION=global
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8080 PYTHONUNBUFFERED=1
CMD ["python", "run.py"]
