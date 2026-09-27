# Daedalus

The master craftsman's workshop, portable. A pinned container image with everything needed to work on Ansible roles and
Python projects, driven from each project's Makefile. The host only needs
podman and make, plus libvirtd if you want VM tests.

| Name | What it is |
|---|---|
| **daedalus** | the workshop image (`Containerfile`) and `daedalus.mk`, which projects include |
| **labyrinth** | the internal network an AI agent session is confined to (`make agent` / `make labyrinth`) |
| **ariadne** | the egress proxy (`ariadne/`): the one thread out of the labyrinth, allowlisted domains only |

| Need | Provided by |
|---|---|
| Editors | `vi` (vim-minimal), `vim`, `nvim` |
| SSH | host ssh-agent socket forwarded; a private agent is started if none |
| Secrets | `bao` (OpenBao CLI); `BAO_ADDR`/`BAO_TOKEN`/`BAO_CACERT`/`~/.vault-token` forwarded |
| Encrypted files | `gpg` using the forwarded host gpg-agent; `ansible-vault` with `bao-vault-client` / `gpg-vault-client` vault-id scripts |
| Forge | `fj` (forgejo-cli); `~/.local/share/forgejo-cli` forwarded if present |
| Image builds | rootless `buildah` (plus `podman`/`skopeo`) nested in the container, fuse-overlayfs |
| VM tests | `vagrant` + `vagrant-libvirt` (Fedora packages) talking to the host libvirtd socket; bento boxes |
| Ansible | ansible-core, ansible-lint, molecule + vagrant driver, yamllint, hvac in `/opt/ansible` |
| Python | system python3 + `uv` for per-project venvs |

## Build and publish

```sh
make lock              # pin requirements-tools.txt (hashes) from requirements-tools.in
make build test        # build both images, run daedalus-selftest (incl. a nested buildah build)
make push REGISTRY=git.example.com/ops TAG=2026.09.1
```

`.forgejo/workflows/build.yml` does the same on tags and weekly.

## Use in a project

Copy `daedalus.mk` into the project, set `DAEDALUS_IMAGE`/`DAEDALUS_TAG` (or
`DAEDALUS_DIGEST`) in its Makefile, `include daedalus.mk`, and wrap recipes in
`$(DD_RUN)`. See `examples/example-project`.

## Two modes

**`make shell` (operator).** Host network, `/dev/fuse`, and your ssh-agent,
gpg-agent, OpenBao env, `~/.gitconfig`, `~/.ssh/config`, and libvirt socket.
The repo is mounted at the same absolute path as on the host so
vagrant/libvirt/molecule state stays valid on both sides. Your host uid is
mapped to `op` (uid 1000) with `--userns=keep-id`, so files you create are
owned by you.

**`make agent` (AI agents).**
- The container is attached only to an `--internal` podman network. It has
  no DNS (`--dns none`) and no route out.
- The only egress is a squid proxy (`ariadne/`) that forwards HTTP(S) to the
  domains in `.daedalus/agent-allowlist.txt` and refuses raw IPs.
- None of the host sockets or credentials are mounted.
- It runs with `--cap-drop ALL`, `no-new-privileges`, and memory/CPU/pid limits.
- It gets its own cache and history volumes, so it can't poison the operator's
  image store or box cache.
- `.git` is mounted read-only. The agent edits the working tree and you
  review and commit. This also keeps it from planting git hooks that would
  later run on your host.

Run `make agent-check` to prove the fence. To find blocked hosts, run
`podman logs ariadne-<project> | grep DENIED`.

## Caveats

- **The libvirt socket is root-equivalent on the host.** A guest definition
  can mount host disks. That's why only operator mode gets it.
- **Buildah doesn't work in agent mode.** `no-new-privileges` blocks
  `newuidmap`. If you want agents building images, drop that flag for a
  dedicated profile, knowing what that costs.
- **Tools must honour `HTTPS_PROXY` in agent mode.** SSH to the forge doesn't
  work there; use HTTPS.
- **Adding an AI CLI.** Layer it: `FROM daedalus`, install the CLI,
  set `AGENT_CMD`, and add its API host to the allowlist.
- **Libvirt socket permissions.** If the socket is group-restricted rather
  than polkit-managed, set `LIBVIRT_KEEP_GROUPS=1`.
- **forgejo-cli checksum.** Codeberg publishes no checksum file for it. Set
  `FJ_SHA256` after your first build.
