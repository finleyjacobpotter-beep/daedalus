# example-project

Ansible roles + Python scripts, worked on entirely inside the pinned operator
daedalus. Host requirements: podman, make, and (for VM tests) libvirtd.

```sh
make pull          # once per daedalus bump
make shell         # operator shell: your ssh-agent, gpg-agent, libvirt, OpenBao
make deps lint test
make molecule      # role test on bento boxes via vagrant-libvirt
make vm-up vm-test # run src/ on ubuntu/debian/rocky
make agent         # fenced shell for an AI agent (see .daedalus/agent-allowlist.txt)
```

Layout

```
Makefile                      targets; each wrapped in $(DD_RUN)
daedalus.mk                    vendored from ops/daedalus - don't edit here
.daedalus/agent-allowlist.txt  egress allowlist for `make agent`
ansible.cfg requirements.yml  ansible config + collections
inventory/ playbooks/ group_vars/
roles/<role>/molecule/        molecule scenario (vagrant driver, libvirt provider)
pyproject.toml src/ tests/    python, managed with uv
Vagrantfile                   distro matrix for python scripts / ad-hoc role runs
.vault-pass/*.gpg             gpg-encrypted vault passwords (see its README)
```
