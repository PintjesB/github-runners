FROM python:3.14.8-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/evidence
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && groupadd --gid 10001 evidence \
 && useradd --uid 10001 --gid 10001 --no-create-home evidence
COPY server.py retention.py ./
USER 10001:10001
ENTRYPOINT ["python", "/opt/evidence/server.py"]
