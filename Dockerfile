# Option Sentinel Cloud Container
FROM python:3.12-slim

# Avoid buffering for real-time logs
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8765

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Ensure data and Fidelity directories exist
RUN mkdir -p data Fidelity

EXPOSE 8765

CMD ["python", "option_sentinel.py", "--server", "--host", "0.0.0.0"]
