# Build, test and publish Daedalus images.
#   make build test          build both images locally and smoke-test
#   make lock                re-pin requirements-tools.txt and agent/requirements.txt
#   make agent-test          run the daedalus-agent unit tests (uv, no container needed)
#   make push                push :$(TAG) (and :latest) to the registry
#   make shell / agent       try it on this repo (daedalus.mk is included below)

REGISTRY        ?= git.example.com/ops
TAG             ?= $(shell date +%Y.%m).1
FEDORA_VERSION  ?= 44
OPENBAO_VERSION ?= 2.7.0
FJ_VERSION      ?= 0.6.0
FJ_SHA256       ?=
PODMAN          ?= podman

DAEDALUS_IMAGE   := $(REGISTRY)/daedalus
EGRESS_IMAGE    := $(REGISTRY)/ariadne
DAEDALUS_TAG     := $(TAG)
AGENT_ALLOWLIST := $(CURDIR)/ariadne/allowlist.txt

include daedalus.mk

.PHONY: build build-daedalus build-ariadne test lock push agent-test

build: build-daedalus build-ariadne

build-daedalus:
	$(PODMAN) build -f Containerfile \
		--build-arg FEDORA_VERSION=$(FEDORA_VERSION) \
		--build-arg OPENBAO_VERSION=$(OPENBAO_VERSION) \
		--build-arg FJ_VERSION=$(FJ_VERSION) \
		--build-arg FJ_SHA256=$(FJ_SHA256) \
		-t $(DAEDALUS_IMAGE):$(TAG) -t $(DAEDALUS_IMAGE):latest .

build-ariadne:
	$(PODMAN) build -f ariadne/Containerfile --build-arg FEDORA_VERSION=$(FEDORA_VERSION) \
		-t $(EGRESS_IMAGE):$(TAG) -t $(EGRESS_IMAGE):latest ariadne

test:
	$(PODMAN) run --rm --userns=keep-id:uid=1000,gid=1000 --security-opt label=disable \
		--device /dev/fuse -e DAEDALUS_SELFTEST_BUILD=1 \
		$(DAEDALUS_IMAGE):$(TAG) daedalus-selftest

# Resolve in the same Fedora release the image uses so the Python version matches.
lock:
	$(PODMAN) run --rm --security-opt label=disable -v "$(CURDIR):/src" -w /src \
		registry.fedoraproject.org/fedora:$(FEDORA_VERSION) bash -c '\
		dnf -y -q install uv python3 >/dev/null && \
		uv pip compile --universal --generate-hashes --python /usr/bin/python3 \
			requirements-tools.in -o requirements-tools.txt && \
		uv pip compile --universal --generate-hashes --python /usr/bin/python3 \
			agent/pyproject.toml -o agent/requirements.txt'

agent-test:
	cd agent && uv run --python 3.12 --extra test pytest -q
	cd agent && uvx ruff check src tests && uvx ruff format --check src tests

push:
	$(PODMAN) push $(DAEDALUS_IMAGE):$(TAG)
	$(PODMAN) push $(DAEDALUS_IMAGE):latest
	$(PODMAN) push $(EGRESS_IMAGE):$(TAG)
	$(PODMAN) push $(EGRESS_IMAGE):latest
	@echo "pin in projects with: DAEDALUS_DIGEST := $$($(PODMAN) image inspect --format '{{.Digest}}' $(DAEDALUS_IMAGE):$(TAG))"
