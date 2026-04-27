FROM python:3.12-alpine

LABEL org.opencontainers.image.source="https://github.com/Alpasyon007/crafty-proton-port-updater"
LABEL org.opencontainers.image.description="Port-updater service: Crafty + ProtonVPN + Gluetun"
LABEL org.opencontainers.image.licenses="MIT"

# Create a non-root user
RUN adduser -D -u 1000 app

WORKDIR /app

COPY port_updater.py .

USER app

ENTRYPOINT ["python", "/app/port_updater.py"]
