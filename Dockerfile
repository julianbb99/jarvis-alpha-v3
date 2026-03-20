FROM python:3.11-slim
WORKDIR /app
RUN pip install requests
COPY bot.py .
CMD ["python", "-u", "bot.py"]
