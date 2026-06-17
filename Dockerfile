FROM python:3.11-slim

WORKDIR /app

# Install deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all code
COPY . .

# Make routines importable
RUN touch routines/__init__.py core/__init__.py

EXPOSE 8501

CMD ["streamlit", "run", "examples/daily-market-dashboard/app.py", "--server.port", "8501", "--server.address", "0.0.0.0", "--server.headless", "true"]
