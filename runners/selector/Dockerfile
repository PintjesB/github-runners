FROM python:3.15-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9

COPY route.py /app/route.py
COPY --chmod=755 scripts/probe.sh /usr/local/bin/probe

LABEL org.opencontainers.image.source="https://github.com/PintjesB/github-runners" \
      org.opencontainers.image.description="GitHub Actions runner fallback selector"

USER 1001:1001
ENTRYPOINT ["python3", "-u", "/app/route.py"]
CMD ["--watch"]
