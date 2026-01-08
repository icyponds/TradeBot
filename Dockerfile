# Use official Python runtime as a parent image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Set work directory
WORKDIR /app

# Install system dependencies (needed for some python packages like pandas/numpy/TA-Lib if used)
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files (Note: Bind mount in compose will override this for 'src', 
# but copying ensures image is standalone if needed)
COPY . .

# Create volume mount points to ensure permissions exist
RUN mkdir -p /app/data /app/logs

# Run the application
CMD ["python", "src/main.py"]
