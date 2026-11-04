FROM python:3.12-slim

LABEL org.opencontainers.image.title="sonos-flip" \
      org.opencontainers.image.description="Switch two Sonos speakers between a soundbar's rear surrounds and a standalone stereo pair" \
      org.opencontainers.image.source="https://github.com/andreykats/sonos-flip" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_FILE=/config/config.toml

WORKDIR /app
COPY config.example.toml ./
COPY sonos_flip/ ./sonos_flip/

# config.toml (settings) and state.json (what the service remembers) live here.
VOLUME /config
EXPOSE 8080

# Healthy once the speakers have been found and the API answers.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
  CMD ["python", "-m", "sonos_flip", "healthcheck"]

CMD ["python", "-m", "sonos_flip"]
