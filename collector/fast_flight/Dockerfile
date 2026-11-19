# the fast-flights collector, with collector/common/. build from the repo root: docker build -f collector/fast_flight/Dockerfile .
FROM python:3.12-slim

WORKDIR /app
COPY collector/fast_flight/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY collector/__init__.py ./collector/
COPY collector/common ./collector/common
COPY collector/fast_flight ./collector/fast_flight

# no root needed, it only talks to Google and the database
USER 65532:65532
ENTRYPOINT ["python", "-u", "-m", "collector.fast_flight"]
