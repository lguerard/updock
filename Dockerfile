# docker:cli = Docker client + compose plugin (updock runs `docker compose
# pull/up` on the service being updated), on Alpine.
FROM docker:29-cli

# tzdata: without it TZ is ignored and UPDOCK_CHECK_AT would be read as UTC.
RUN apk add --no-cache python3 tzdata

ARG VERSION=dev
LABEL org.opencontainers.image.title="updock" \
      org.opencontainers.image.description="Real releases for your Docker containers, one tap to update" \
      org.opencontainers.image.source="https://github.com/lguerard/updock" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      updock.enable="false"

WORKDIR /app
COPY updock.py ui.html ./
ENV UPDOCK_STATE_FILE=/data/state.json PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8099
HEALTHCHECK --interval=1m --timeout=5s CMD wget -q -O /dev/null http://127.0.0.1:8099/health || exit 1

CMD ["python3", "/app/updock.py"]
