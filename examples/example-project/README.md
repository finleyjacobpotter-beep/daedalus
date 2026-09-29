# example-project

Ansible roles + Python scripts, worked on entirely inside the pinned operator
daedalus. Host requirements: podman, make, python3 (3.11+) with the `daedalus`
module installed, and (for VM tests) libvirtd.

```sh
daedalus pull          # once per daedalus bump
daedalus shell         # operator shell: your ssh-agent, gpg-agent, libvirt, OpenBao
make deps lint test    # the project's own tasks, each run inside the container
make molecule          # role test on bento boxes via vagrant-libvirt
make vm-up vm-test     # run src/ on ubuntu/debian/rocky
daedalus agent         # fenced shell for an AI agent (see .daedalus/agent-allowlist.txt)
daedalus ask 'why does molecule fail on rocky?'   # daedalus-agent in the fence
```

Layout

```
Makefile                      project targets; each wrapped in $(DD_RUN)
.daedalus/config.toml          daedalus settings: image pin, agent limits and tools
.daedalus/agent-allowlist.txt  egress allowlist for `daedalus agent`
ansible.cfg requirements.yml  ansible config + collections
inventory/ playbooks/ group_vars/
roles/<role>/molecule/        molecule scenario (vagrant driver, libvirt provider)
pyproject.toml src/ tests/    python, managed with uv
Vagrantfile                   distro matrix for python scripts / ad-hoc role runs
.vault-pass/*.gpg             gpg-encrypted vault passwords (see its README)
```
