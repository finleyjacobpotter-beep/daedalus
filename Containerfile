FROM alpine:latest

RUN apk add --no-cache \
    neovim git podman nix shadow tmux

RUN addgroup -S podman && \
    adduser -S podman -G podman && \
    mkdir -p /home/podman && \
    chown -R podman /home/podman

WORKDIR /home/podman
USER podman

VOLUME ["/tmp"]
ENTRYPOINT ["/bin/sh", "-i"]

LABEL \
    app.name="alpine-dev" \
    app.version="1.0" \
    app.description="Secure Alpine dev container" \
    app.managed="true"
