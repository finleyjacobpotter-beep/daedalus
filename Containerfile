FROM alpine:latest

RUN apk add --no-cache \
    bash neovim git openssh-client podman nix shadow tmux

RUN addgroup -S podman && \
    adduser -S podman -G podman && \
    mkdir -p /home/podman && \
    chown -R podman /home/podman

WORKDIR /home/podman
USER podman

RUN git config --global user.name "daedalus" && \
    git config --global user.email "daedalus@localhost.local"

COPY --chown=podman:podman container/bashrc /home/podman/.bashrc
COPY --chown=podman:podman container/bash_profile /home/podman/.bash_profile

VOLUME ["/tmp"]
ENTRYPOINT ["/bin/bash", "-l"]

LABEL \
    app.name="alpine-dev" \
    app.version="1.0" \
    app.description="Secure Alpine dev container" \
    app.managed="true"
