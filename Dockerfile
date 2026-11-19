FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY collector ./collector

# no root needed, it only talks to Google and the database
USER 65532:65532
ENTRYPOINT ["python", "-u", "-m", "collector"]
