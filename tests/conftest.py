from types import SimpleNamespace
from unittest import mock

import pytest

import daedalus


def make_container(
    id="0123456789abcdef",
    names=("dev",),
    labels=None,
    status="running",
):
    """A stand-in for podman's Container with the attributes daedalus reads."""
    return mock.Mock(
        id=id,
        names=list(names),
        labels=labels,
        status=status,
        image=SimpleNamespace(short_id="sha256:abc"),
    )


def managed_labels(app="alpine-dev", **extra):
    return {"app.name": app, "app.managed": "true", **extra}


@pytest.fixture
def client(monkeypatch):
    """Replace the podman client every LabelBasedContainerManager gets."""
    fake = mock.MagicMock(name="PodmanClient")
    monkeypatch.setattr(daedalus.podman.PodmanClient, "from_env", lambda: fake)
    return fake


@pytest.fixture
def manager(client):
    return daedalus.LabelBasedContainerManager()


@pytest.fixture
def cli(monkeypatch):
    """Run `daedalus <argv>` and return its exit code."""

    def run(*argv):
        monkeypatch.setattr("sys.argv", ["daedalus", *argv])
        with pytest.raises(SystemExit) as exit:
            daedalus.main()
        return exit.value.code

    return run
