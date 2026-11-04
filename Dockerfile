# For always-on hosting later (a small cloud VM). WebRTC needs UDP or a TURN server (ICE_SERVERS).
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV HTTP_PORT=8080
EXPOSE 8080
CMD ["python", "-m", "saarthi.main"]
