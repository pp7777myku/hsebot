# Use official lightweight Python image
FROM python:3.11-slim

# Ensure Python prints output directly (no buffering), useful for Cloud Run logs
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Default port value; Cloud Run will override this with the PORT environment variable
ENV PORT=8080

# Set working directory inside the container
WORKDIR /app

# Copy dependency list first to leverage Docker layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Expose port (for documentation; Cloud Run does not rely solely on this)
EXPOSE 8080

# Run the Telegram bot application
CMD ["python", "main.py"]
