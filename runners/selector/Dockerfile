FROM python:3.13-slim-bookworm@sha256:f0fa218d5b06f9b9235024fa061cde2cb141f726de52134471053d2d9b5324a3

COPY route.py /app/route.py
COPY --chmod=755 scripts/probe.sh /usr/local/bin/probe

LABEL org.opencontainers.image.source="https://github.com/PintjesB/github-runners" \
      org.opencontainers.image.description="GitHub Actions runner fallback selector"

USER 1001:1001
ENTRYPOINT ["python3", "-u", "/app/route.py"]
CMD ["--watch"]
