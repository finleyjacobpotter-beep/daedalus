# daedalus.mk - run this repo's work inside Daedalus container.
#
# Vendored from the daedalus repo. Don't edit it per project: set the
# variables below in your project Makefile *before* `include daedalus.mk`.
#
#   make pull            pull the pinned daedalus + ariadne (egress proxy) images
#   make shell           operator shell: ssh-agent, gpg-agent, libvirt, OpenBao forwarded
#   make run CMD='...'   run one command in an operator container
#   make agent           (alias: make labyrinth) fenced shell for AI agents: allowlisted HTTPS egress only, no host secrets
#   make agent-check     prove the fence works (allowed host OK, others blocked)
#   make agent-down      stop the egress proxy and remove the agent network
#
# Project Makefiles can use $(DD_RUN) to run a target inside the daedalus, e.g.
#   test: ; $(DD_RUN) make test        (see the example project)

ifdef DAEDALUS_INSIDE
# Already inside the container: the targets below would try to nest podman.
DD_RUN :=
else

PODMAN          ?= podman
DAEDALUS_IMAGE   ?= git.example.com/ops/daedalus
DAEDALUS_TAG     ?= 2026.09.1
# Set to sha256:... to pin immutably (make daedalus-digest prints it).
DAEDALUS_DIGEST  ?=
EGRESS_IMAGE    ?= git.example.com/ops/ariadne
EGRESS_TAG      ?= $(DAEDALUS_TAG)

PROJECT         ?= $(notdir $(CURDIR))
# Container-side uid of the `op` user; your host uid is mapped onto it.
DAEDALUS_UID     ?= 1000
# host = simplest way for vagrant-libvirt to reach VMs on virbr* networks.
DAEDALUS_NET     ?= host
DAEDALUS_EXTRA_ARGS ?=

LIBVIRT_SOCK    ?= /var/run/libvirt/libvirt-sock
# Set to 1 if your libvirt socket is group-restricted (no polkit) so the
# container keeps your supplementary groups (needs crun).
LIBVIRT_KEEP_GROUPS ?= 0

AGENT_ALLOWLIST ?= $(CURDIR)/.daedalus/agent-allowlist.txt
AGENT_GIT_RO    ?= 1
AGENT_MEMORY    ?= 8g
AGENT_CPUS      ?= 4
AGENT_PIDS      ?= 4096
# Host env vars passed into the agent container by name (values never hit the command line).
AGENT_ENV       ?= ANTHROPIC_API_KEY
AGENT_CMD       ?= bash -l

# --------------------------------------------------------------------------
_ref      = $(if $(DAEDALUS_DIGEST),$(DAEDALUS_IMAGE)@$(DAEDALUS_DIGEST),$(DAEDALUS_IMAGE):$(DAEDALUS_TAG))
_egress_ref = $(EGRESS_IMAGE):$(EGRESS_TAG)
_home     = /home/op
_run_user = /run/user/$(DAEDALUS_UID)
_gpg_sock := $(shell gpgconf --list-dirs agent-socket 2>/dev/null)

_agent_net    = labyrinth-$(PROJECT)
_egress_net   = ariadne-net
_egress_ctr   = ariadne-$(PROJECT)

# Allocate a TTY only when make itself has one (so it also works in CI/pipes).
_tty = -i $$(test -t 0 && echo -t)

# Shared by both modes. The repo is mounted at the SAME path as on the host so
# paths in libvirt/vagrant/molecule state files stay valid on both sides.
_common = --rm --init $(_tty) \
	--userns=keep-id:uid=$(DAEDALUS_UID),gid=$(DAEDALUS_UID) \
	--security-opt label=disable \
	--hostname daedalus-$(PROJECT) \
	-e DAEDALUS_PROJECT=$(PROJECT) -e TERM -e COLORTERM -e TZ \
	-v "$(CURDIR):$(CURDIR)" -w "$(CURDIR)"

# Operator: forwards your agents and credentials, full network.
_operator = -e DAEDALUS_MODE=operator \
	--network $(DAEDALUS_NET) \
	--device /dev/fuse \
	-v daedalus-containers:$(_home)/.local/share/containers \
	-v daedalus-vagrant:$(_home)/.vagrant.d \
	-v daedalus-cache:$(_home)/.cache \
	-v daedalus-history:$(_home)/.history \
	$(if $(wildcard $(SSH_AUTH_SOCK)),-v "$(SSH_AUTH_SOCK):/run/host/ssh-agent.sock" -e SSH_AUTH_SOCK=/run/host/ssh-agent.sock) \
	$(if $(wildcard $(HOME)/.ssh/config),-v "$(HOME)/.ssh/config:$(_home)/.ssh/config:ro") \
	$(if $(wildcard $(HOME)/.ssh/known_hosts),-v "$(HOME)/.ssh/known_hosts:$(_home)/.ssh/known_hosts") \
	$(if $(wildcard $(HOME)/.gitconfig),-v "$(HOME)/.gitconfig:$(_home)/.gitconfig:ro") \
	$(if $(wildcard $(HOME)/.gnupg),-v "$(HOME)/.gnupg:$(_home)/.gnupg") \
	$(if $(wildcard $(_gpg_sock)),-v "$(_gpg_sock):$(_run_user)/gnupg/S.gpg-agent") \
	$(if $(wildcard $(LIBVIRT_SOCK)),-v "$(LIBVIRT_SOCK):/var/run/libvirt/libvirt-sock" -e LIBVIRT_DEFAULT_URI=qemu:///system) \
	$(if $(filter 1,$(LIBVIRT_KEEP_GROUPS)),--group-add keep-groups) \
	-e BAO_ADDR -e BAO_NAMESPACE -e BAO_TOKEN \
	$(if $(BAO_CACERT),-v "$(BAO_CACERT):/run/host/bao-ca.pem:ro" -e BAO_CACERT=/run/host/bao-ca.pem) \
	$(if $(wildcard $(HOME)/.vault-token),-v "$(HOME)/.vault-token:$(_home)/.vault-token") \
	$(if $(wildcard $(HOME)/.local/share/forgejo-cli),-v "$(HOME)/.local/share/forgejo-cli:$(_home)/.local/share/forgejo-cli") \
	$(DAEDALUS_EXTRA_ARGS)

# Agent: no host sockets, no credentials, own volumes (so it cannot poison the
# operator's image store, box cache or pip cache), internal network only.
_agent = -e DAEDALUS_MODE=agent \
	--network $(_agent_net) --dns none \
	--cap-drop ALL --security-opt no-new-privileges \
	--memory $(AGENT_MEMORY) --cpus $(AGENT_CPUS) --pids-limit $(AGENT_PIDS) \
	-v labyrinth-$(PROJECT)-cache:$(_home)/.cache \
	-v labyrinth-$(PROJECT)-history:$(_home)/.history \
	$(if $(filter 1,$(AGENT_GIT_RO)),$(if $(wildcard $(CURDIR)/.git),-v "$(CURDIR)/.git:$(CURDIR)/.git:ro")) \
	$(foreach v,$(AGENT_ENV),-e $(v))

# Use in project recipes: $(DD_RUN) <command>
DD_RUN = $(PODMAN) run $(_common) $(_operator) $(_ref)

.PHONY: pull shell run agent labyrinth agent-up agent-down agent-check daedalus-digest daedalus-clean

pull:
	$(PODMAN) pull $(_ref)
	$(PODMAN) pull $(_egress_ref)

shell:
	$(DD_RUN)

run:
	@test -n "$(CMD)" || { echo "usage: make run CMD='ansible --version'"; exit 2; }
	$(DD_RUN) bash -lc '$(CMD)'

# --- agent fence ------------------------------------------------------------
agent-up:
	@test -f "$(AGENT_ALLOWLIST)" || { echo "missing $(AGENT_ALLOWLIST)"; exit 2; }
	@$(PODMAN) network exists $(_egress_net) || $(PODMAN) network create $(_egress_net) >/dev/null
	@$(PODMAN) network exists $(_agent_net)  || $(PODMAN) network create --internal $(_agent_net) >/dev/null
	@$(PODMAN) rm -f -i $(_egress_ctr) >/dev/null
	@$(PODMAN) run -d --name $(_egress_ctr) --network $(_egress_net) \
		--security-opt label=disable --read-only --tmpfs /var/spool/squid --tmpfs /run/squid --tmpfs /var/log/squid \
		-v "$(AGENT_ALLOWLIST):/etc/squid/allowlist.txt:ro" \
		$(_egress_ref) >/dev/null
	@$(PODMAN) network connect $(_agent_net) $(_egress_ctr)
	@echo "egress proxy $(_egress_ctr) up; allowlist: $(AGENT_ALLOWLIST)"

agent-down:
	-@$(PODMAN) rm -f -i $(_egress_ctr) >/dev/null
	-@$(PODMAN) network rm -f $(_agent_net) >/dev/null 2>&1 || true

# Proxy is addressed by IP + --add-host because the agent has no DNS at all
# (--dns none), which also closes DNS as an exfiltration channel.
define _agent_run
	ip=$$($(PODMAN) inspect -f '{{ (index .NetworkSettings.Networks "$(_agent_net)").IPAddress }}' $(_egress_ctr)); \
	p=http://ariadne:3128; \
	$(PODMAN) run $(_common) $(_agent) --add-host ariadne:$$ip \
		-e HTTP_PROXY=$$p -e HTTPS_PROXY=$$p -e http_proxy=$$p -e https_proxy=$$p \
		-e NO_PROXY=localhost,127.0.0.1 -e no_proxy=localhost,127.0.0.1 \
		$(_ref)
endef

agent labyrinth: agent-up
	@$(_agent_run) $(AGENT_CMD)

agent-check: agent-up
	@allowed=$$(grep -v '^\s*#' "$(AGENT_ALLOWLIST)" | grep -v '^\s*$$' | head -n1 | sed 's/^\.//'); \
	echo "allowed : https://$$allowed"; \
	$(_agent_run) bash -c "curl -sS -o /dev/null -w '  -> %{http_code}\n' https://$$allowed/ || true; \
		echo 'blocked : https://example.org'; \
		curl -sS -o /dev/null -w '  -> %{http_code}\n' https://example.org/ 2>&1 | sed 's/^/  -> /'; \
		echo 'direct  : no proxy'; \
		curl -sS --noproxy '*' -m 5 -o /dev/null https://1.1.1.1/ 2>&1 | sed 's/^/  -> /' || true"

daedalus-digest:
	@$(PODMAN) image inspect --format '{{.Digest}}' $(DAEDALUS_IMAGE):$(DAEDALUS_TAG)

# Removes the shared operator volumes (image store, vagrant boxes, caches).
daedalus-clean: agent-down
	-$(PODMAN) volume rm daedalus-containers daedalus-vagrant daedalus-cache daedalus-history \
		labyrinth-$(PROJECT)-cache labyrinth-$(PROJECT)-history

endif
