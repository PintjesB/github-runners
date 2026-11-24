FROM python:3.15-slim-bookworm@sha256:498829c717b1e59fc42f0e7865eafe4e04ca0f70a3769d4ae70ba218df54507c

COPY route.py /app/route.py
COPY --chmod=755 scripts/probe.sh /usr/local/bin/probe

LABEL org.opencontainers.image.source="https://github.com/PintjesB/github-runners" \
      org.opencontainers.image.description="GitHub Actions runner fallback selector"

USER 1001:1001
ENTRYPOINT ["python3", "-u", "/app/route.py"]
CMD ["--watch"]
