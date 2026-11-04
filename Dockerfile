FROM python:3.11-slim

# Install super-lightweight Tesseract OCR engine & clean apt cache immediately to save RAM
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends tesseract-ocr && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render automatically injects PORT environment variable
ENV PORT=8080
EXPOSE 8080

CMD ["python", "bot.py"]
