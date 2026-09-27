# Daedalus: everything an operator (or a fenced-in AI agent) needs to work
# on Ansible roles and Python projects without installing anything on the host.
#
# Build:  make build            (see Makefile in this directory)
# Use:    include daedalus.mk from a project Makefile, then `make shell` / `make agent`

ARG FEDORA_VERSION=44
FROM registry.fedoraproject.org/fedora:${FEDORA_VERSION}

ARG OPENBAO_VERSION=2.7.0
ARG FJ_VERSION=0.6.0
# Optional: sha256 of forgejo-cli-<arch>-linux.tar.gz. Codeberg publishes no
# checksum file, so pin it yourself after the first download.
ARG FJ_SHA256=""
ARG OP_UID=1000

LABEL org.opencontainers.image.title="daedalus" \
      org.opencontainers.image.description="Ansible/Python Daedalus: buildah, vagrant-libvirt, openbao, fj, gpg, neovim"

# ---------------------------------------------------------------------------
# OS packages. Fedora is used because it packages buildah, vagrant AND
# vagrant-libvirt natively, so there is no gem/plugin compile step.
# ---------------------------------------------------------------------------
RUN dnf -y install --setopt=install_weak_deps=False \
        bash-completion ca-certificates curl diffutils file findutils git git-lfs \
        glibc-langpack-en gzip iproute iputils jq less make man-db patch procps-ng \
        rsync tar tmux tree unzip which xz \
        openssh-clients \
        vim-minimal vim-enhanced neovim \
        gnupg2 pinentry \
        python3 python3-devel python3-pip gcc uv \
        buildah podman skopeo fuse-overlayfs shadow-utils crun passt \
        vagrant vagrant-libvirt libvirt-client \
    && dnf clean all \
    # newuidmap/newgidmap lose their file capabilities in image layers; restore them
    # so rootless buildah can create nested user namespaces (same as quay.io/buildah/stable)
    && rpm --restore shadow-utils

# ---------------------------------------------------------------------------
# OpenBao CLI (bao), checksum-verified against the release's checksums.txt
# ---------------------------------------------------------------------------
RUN set -eux; \
    arch="$(uname -m | sed -e 's/x86_64/amd64/' -e 's/aarch64/arm64/')"; \
    tmp="$(mktemp -d)"; cd "$tmp"; \
    base="https://github.com/openbao/openbao/releases/download/v${OPENBAO_VERSION}"; \
    curl -fsSLO "${base}/openbao_${OPENBAO_VERSION}_linux_${arch}.tar.gz"; \
    curl -fsSLO "${base}/checksums.txt"; \
    sha256sum --check --ignore-missing checksums.txt; \
    tar -xzf "openbao_${OPENBAO_VERSION}_linux_${arch}.tar.gz" bao; \
    install -m 0755 bao /usr/local/bin/bao; \
    cd /; rm -rf "$tmp"; \
    bao version

# ---------------------------------------------------------------------------
# forgejo-cli (fj)
# ---------------------------------------------------------------------------
RUN set -eux; \
    tmp="$(mktemp -d)"; cd "$tmp"; \
    f="forgejo-cli-$(uname -m)-linux.tar.gz"; \
    curl -fsSLO "https://codeberg.org/forgejo-contrib/forgejo-cli/releases/download/v${FJ_VERSION}/${f}"; \
    if [ -n "${FJ_SHA256}" ]; then echo "${FJ_SHA256}  ${f}" | sha256sum -c -; fi; \
    tar -xzf "$f"; \
    install -m 0755 "$(find . -type f -name fj | head -n1)" /usr/local/bin/fj; \
    cd /; rm -rf "$tmp"; \
    fj --version

# ---------------------------------------------------------------------------
# Ansible toolchain in its own venv so the system python3 stays clean for
# project venvs (uv). Entry points are symlinked onto PATH.
# requirements-tools.txt is generated from requirements-tools.in by `make lock`.
# ---------------------------------------------------------------------------
COPY requirements-tools.txt /tmp/requirements-tools.txt
RUN set -eux; \
    uv venv --python /usr/bin/python3 /opt/ansible; \
    uv pip install --python /opt/ansible/bin/python --no-cache -r /tmp/requirements-tools.txt; \
    for b in /opt/ansible/bin/ansible* /opt/ansible/bin/molecule /opt/ansible/bin/yamllint; do \
        ln -sf "$b" /usr/local/bin/; \
    done; \
    rm /tmp/requirements-tools.txt; \
    ansible --version

# ---------------------------------------------------------------------------
# Config, helper scripts, unprivileged user
# ---------------------------------------------------------------------------
COPY rootfs/ /

RUN set -eux; \
    chmod 0755 /usr/local/bin/*; \
    useradd -u "${OP_UID}" -m -s /bin/bash op; \
    # sub-ID ranges for nested rootless buildah/podman. They fit inside the
    # 65536 IDs that `--userns=keep-id` gives the outer container.
    printf 'op:1:%s\nop:%s:%s\n' "$((OP_UID-1))" "$((OP_UID+1))" "$((65536-OP_UID-1))" > /etc/subuid; \
    cp /etc/subuid /etc/subgid; \
    # Mount points that must exist with the right owner BEFORE podman bind-mounts
    # sockets/volumes into them, otherwise they get created root-owned.
    install -d -o op -g op -m 0700 /home/op/.gnupg /home/op/.ssh; \
    install -d -o op -g op -m 0755 /home/op/.local/share/containers /home/op/.vagrant.d \
        /home/op/.cache /home/op/.config /home/op/.history; \
    install -d -m 0755 /run/user /run/host; \
    install -d -o op -g op -m 0700 "/run/user/${OP_UID}" "/run/user/${OP_UID}/gnupg"; \
    chown -R op:op /home/op

ENV DAEDALUS_INSIDE=1 \
    LANG=en_US.UTF-8 \
    EDITOR=nvim \
    BUILDAH_ISOLATION=chroot \
    _BUILDAH_STARTED_IN_USERNS="" \
    VAGRANT_DEFAULT_PROVIDER=libvirt \
    ANSIBLE_FORCE_COLOR=1 \
    UV_LINK_MODE=copy \
    HISTFILE=/home/op/.history/bash_history

USER op
WORKDIR /home/op
ENTRYPOINT ["/usr/local/bin/daedalus-entrypoint"]
CMD ["bash", "-l"]
