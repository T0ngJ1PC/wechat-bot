FROM mcr.microsoft.com/playwright/python:1.38-focal

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY . /app

RUN python -m pip install --upgrade pip setuptools wheel
RUN if [ -f requirements.txt ]; then pip install -r requirements.txt; fi

EXPOSE 5000

CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5000", "app:app"]
