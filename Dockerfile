# One image, two modes (set SAARTHI_MODE):
#   review (default) : no API keys needed. Synthetic demo fleet, a truck driving NH48, review page at /review?k=REVIEW_KEY
#   full             : the real system (needs GNANI_API_KEY, LLM_API_KEY; optional DRIVER_BOT_TOKEN, ICE_SERVERS for voice)
# Live voice uses WebRTC: on hosts without UDP (Render, Railway) set ICE_SERVERS with a TURN server.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONUNBUFFERED=1 SAARTHI_MODE=review
EXPOSE 8080
CMD ["sh", "scripts/docker_start.sh"]
