# Daedalus

The master craftsman's workshop, portable. A pinned container image with everything needed to work on Ansible roles and
Python projects, driven by the `daedalus` command (a Python module, standard
library only). The host only needs podman and python3 3.11+, plus libvirtd if
you want VM tests.

| Name | What it is |
|---|---|
| **daedalus** | the workshop image (`Containerfile`) and the `daedalus` command (`daedalus/`) that runs it |
| **labyrinth** | the internal network an AI agent session is confined to (`daedalus agent` / `daedalus labyrinth`) |
| **ariadne** | the egress proxy (`ariadne/`): the one thread out of the labyrinth, allowlisted domains only |
| **daedalus-agent** | the AI agent (`agent/`), built on [tau](https://github.com/huggingface/tau): no tools unless named, plus hyper planning and ultrawork agent teams |

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
| AI agent | `daedalus-agent` (tau) in `/opt/daedalus-agent`; see [agent/README.md](agent/README.md) |
| This tool | `daedalus` / `python3 -m daedalus` on the system python path (inside, `daedalus run -- CMD` just runs CMD) |

## The `daedalus` command

```sh
python3 -m pip install --user .     # or: pipx install .   (adds `daedalus` to PATH)
python3 -m daedalus --help          # works from a checkout without installing
```

| Command | What it does |
|---|---|
| `daedalus pull` | pull the pinned daedalus and ariadne images |
| `daedalus shell` | operator shell (see below) |
| `daedalus run -- CMD ...` / `daedalus run -c 'CMD && CMD'` | one command in an operator container |
| `daedalus agent [CMD ...]` | fenced shell for an AI agent (alias `labyrinth`) |
| `daedalus agent-up` / `agent-down` / `agent-check` | manage and prove the fence |
| `daedalus ask` / `hyperplan` / `ultrawork GOAL` | daedalus-agent inside the fence |
| `daedalus digest` / `clean` / `config` | print the pinned digest, drop volumes, show settings |
| `daedalus build` / `test` / `lock` / `push` / `unit-test` | build and publish the images (this repo) |

Global flags: `-C DIR` (project directory), `-n` (dry run: print the podman
commands), `-v` (echo commands), `--image/--tag/--digest/--registry/--project`,
and `-o name=value` for any setting.

**Settings** come from `.daedalus/config.toml` in the project, then
`DAEDALUS_<NAME>` environment variables, then `-o name=value`. `daedalus
config` lists every one with its effective value: the image pin (`image`,
`tag`, `digest`), `uid`, `net`, `extra_args`, `libvirt_keep_groups`, the agent
limits (`agent_memory`, `agent_cpus`, `agent_pids`, `agent_env`,
`agent_allowlist`, `agent_git_ro`, `agent_cmd`) and daedalus-agent's
`agent_tools`, `agent_model` and `agent_args`.

**Inside the container** the module is on the path too. `daedalus run -- CMD`
runs CMD directly, and `daedalus ask/hyperplan/ultrawork` run daedalus-agent
directly; commands that would start containers refuse.

## Build and publish

```sh
daedalus lock              # pin requirements-tools.txt and agent/requirements.txt (hashes)
daedalus unit-test         # unit tests for this module and daedalus-agent (no container)
daedalus build && daedalus test    # build both images, run daedalus-selftest (incl. nested buildah)
daedalus --registry git.example.com/ops --tag 2026.09.1 push
```

`.forgejo/workflows/build.yml` does the same on tags and weekly.

## Use in a project

Pin the image in the project's `.daedalus/config.toml` (`image`, `tag`, or
`digest`), add `.daedalus/agent-allowlist.txt`, and run `daedalus shell`.
Project task runners wrap their commands in `daedalus run --`; a Makefile can
set `DD_RUN := daedalus run --` outside the container and leave it empty
inside (`ifdef DAEDALUS_INSIDE`). See `examples/example-project`.

## Two modes

**`daedalus shell` (operator).** Host network, `/dev/fuse`, and your ssh-agent,
gpg-agent, OpenBao env, `~/.gitconfig`, `~/.ssh/config`, and libvirt socket.
The repo is mounted at the same absolute path as on the host so
vagrant/libvirt/molecule state stays valid on both sides. Your host uid is
mapped to `op` (uid 1000) with `--userns=keep-id`, so files you create are
owned by you.

**`daedalus agent` (AI agents).**
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

### The agent

`daedalus-agent` is baked into the image. It starts with **no tools**; you
enable each one by name. Add your model API host to the allowlist, then:

```sh
daedalus -o agent_tools=read_file,list_dir,grep ask 'explain the motd role'
daedalus -o agent_tools=read_file,list_dir,glob,grep hyperplan 'add a role for chrony'
daedalus -o agent_tools=read_file,list_dir,glob,grep,write_file,edit_file,run_bash \
    ultrawork 'add a role for chrony, with molecule tests' -- --max-retries 3
```

Or set `agent_tools = [...]` once in `.daedalus/config.toml`.

**Hyper planning** has parallel researchers, competing planners, critics
and reviewers produce one dependency-ordered plan. **Ultrawork** executes
that plan with a team of workers, runs independent tasks in parallel, has a
verifier check each one (rejected work is retried with feedback) and ends
with an integration pass. Details: [agent/README.md](agent/README.md).

Run `daedalus agent-check` to prove the fence. To find blocked hosts, run
`podman logs ariadne-<project> | grep DENIED`.

## Caveats

- **The libvirt socket is root-equivalent on the host.** A guest definition
  can mount host disks. That's why only operator mode gets it.
- **Buildah doesn't work in agent mode.** `no-new-privileges` blocks
  `newuidmap`. If you want agents building images, drop that flag for a
  dedicated profile, knowing what that costs.
- **Tools must honour `HTTPS_PROXY` in agent mode.** SSH to the forge doesn't
  work there; use HTTPS.
- **Adding another AI CLI.** Layer it: `FROM daedalus`, install the CLI,
  set `agent_cmd`, and add its API host to the allowlist.
- **Libvirt socket permissions.** If the socket is group-restricted rather
  than polkit-managed, set `libvirt_keep_groups = true`.
- **forgejo-cli checksum.** Codeberg publishes no checksum file for it. Set
  `fj_sha256` (config or `DAEDALUS_FJ_SHA256`) after your first build.
