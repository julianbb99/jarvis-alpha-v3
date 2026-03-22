FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir requests
COPY bot.py .
COPY start.sh .
RUN chmod +x start.sh
STOPSIGNAL SIGTERM
CMD ["bash", "start.sh"]
