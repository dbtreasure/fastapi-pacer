FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml .
COPY src/ ./src/
COPY examples/ ./examples/

# Install Python dependencies
RUN pip install -e ".[dev]"

# Expose port
EXPOSE 8000

# Default command
CMD ["uvicorn", "examples.simple_app:app", "--host", "0.0.0.0", "--port", "8000"]