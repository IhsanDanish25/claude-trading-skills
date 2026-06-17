FROM python:3.11-slim

WORKDIR /app

# Install deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all code
COPY . .

# Make routines importable
RUN touch routines/__init__.py core/__init__.py

# Default: scheduler (Railway cron triggers this)
CMD ["python3", "scheduler.py"]
