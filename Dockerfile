FROM python:3.9-slim

# Set environment variables using proper format
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libssl-dev \
    libffi-dev \
    python3-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip uninstall -y tensorflow tensorflow-cpu && \
    pip install --no-cache-dir tensorflow-cpu==2.15.0

# Copy application files
COPY . .

# Pre-load ML models
RUN python -c "import pickle; \
    from keras.models import load_model; \
    load_model('./cnn_lstm_appliance_classification_model.h5'); \
    pickle.load(open('./scaler.pkl', 'rb')); \
    pickle.load(open('./label_encoder.pkl', 'rb'))"

# Create non-root user and set permissions
RUN adduser --disabled-password --gecos '' api-user && \
    chown -R api-user:api-user /app
USER api-user



EXPOSE 8000

# Run with keep-alive timeout
CMD ["uvicorn", "lastlast:app", "--host", "0.0.0.0", "--port", "8000"]