FROM python:3.11-slim

WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY *.py ./

# Create .env file placeholder (will be mounted or provided at runtime)
RUN touch .env

# Run the application
CMD ["python", "main.py"]
