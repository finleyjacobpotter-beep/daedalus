# The project's own tasks. Every recipe is wrapped in $(DD_RUN): from the host
# it runs inside the daedalus container (`daedalus run --`); inside the
# container (daedalus shell / daedalus agent) DD_RUN is empty, so the same
# targets run directly. One Makefile, both places.
#
# Container settings (image pin, agent limits, agent tools) live in
# .daedalus/config.toml and are used by the `daedalus` command.

DAEDALUS ?= daedalus
ifdef DAEDALUS_INSIDE
DD_RUN :=
else
DD_RUN := $(DAEDALUS) run --
endif

ROLE     ?= motd
BOXES    ?= ubuntu debian rocky
# Vault ids are passed per target rather than in ansible.cfg so that a
# missing OpenBao login or GPG key doesn't break lint/molecule/agent runs.
VAULT_ID ?= dev@/usr/local/bin/gpg-vault-client

.DEFAULT_GOAL := help
.PHONY: help deps lint fmt test molecule vm-up vm-test vm-provision vm-destroy vault-edit deploy

help:
	@echo "host:    daedalus pull | shell | agent | agent-check | ask | hyperplan | ultrawork"
	@echo "project: make deps lint fmt test molecule vm-up vm-test vm-provision vm-destroy vault-edit deploy"

deps:
	$(DD_RUN) bash -lc 'uv sync && ansible-galaxy collection install -r requirements.yml -p .collections'

lint:
	$(DD_RUN) bash -lc 'yamllint . && ansible-lint && uv run ruff check src tests && uv run ruff format --check src tests'

fmt:
	$(DD_RUN) uv run ruff format src tests

test:
	$(DD_RUN) uv run pytest -q

# Role test: converge + idempotence + verify on bento boxes via vagrant-libvirt.
molecule:
	$(DD_RUN) bash -lc 'cd roles/$(ROLE) && molecule test'

# Python script test across distros (Vagrantfile).
vm-up:
	$(DD_RUN) vagrant up $(BOXES)

vm-test:
	$(DD_RUN) bash -lc 'vagrant rsync $(BOXES) && for b in $(BOXES); do \
		echo "== $$b"; vagrant ssh $$b -c "cd /vagrant && PYTHONPATH=src python3 -m hostreport" || exit 1; done'

vm-provision:
	$(DD_RUN) vagrant provision $(BOXES)

vm-destroy:
	$(DD_RUN) vagrant destroy -f

vault-edit:
	$(DD_RUN) ansible-vault edit --vault-id $(VAULT_ID) group_vars/all/vault.yml

deploy:
	$(DD_RUN) ansible-playbook --vault-id $(VAULT_ID) playbooks/site.yml
