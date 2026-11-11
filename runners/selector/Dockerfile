FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72

COPY route.py /app/route.py
COPY --chmod=755 scripts/probe.sh /usr/local/bin/probe

LABEL org.opencontainers.image.source="https://github.com/PintjesB/github-runners" \
      org.opencontainers.image.description="GitHub Actions runner fallback selector"

USER 1001:1001
ENTRYPOINT ["python3", "-u", "/app/route.py"]
CMD ["--watch"]
