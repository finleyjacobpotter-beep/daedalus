import os
from unittest import mock

import pytest

import daedalus
from conftest import make_container, managed_labels


class TestRun:
    @pytest.fixture
    def subprocess_run(self, monkeypatch):
        run = mock.Mock()
        monkeypatch.setattr(daedalus.subprocess, "run", run)
        monkeypatch.setattr(daedalus.shutil, "which", lambda name: "/usr/bin/podman")
        monkeypatch.delenv("CONTAINER_HOST", raising=False)
        monkeypatch.delenv("DOCKER_HOST", raising=False)
        monkeypatch.setenv("TERM", "xterm-256color")
        return run

    @pytest.fixture
    def container(self, client):
        container = make_container(id="c0ffee")
        client.containers.run.return_value = container
        return container

    def test_starts_a_locked_down_labelled_container(self, manager, client, container, subprocess_run, tmp_path):
        assert manager.run(pwd=str(tmp_path), hostname="me@box", name="dev") is True

        kwargs = client.containers.run.call_args.kwargs
        assert kwargs["image"] == "alpine-dev:latest"
        assert kwargs["name"] == "dev"
        assert kwargs["entrypoint"] == ["sleep", "infinity"]
        assert kwargs["detach"] is True
        assert kwargs["init"] is True
        assert kwargs["read_only"] is True
        assert kwargs["cap_drop"] == ["all"]
        assert kwargs["security_opt"] == ["no-new-privileges=true"]
        assert kwargs["userns_mode"] == "keep-id"
        assert kwargs["user"] == f"{os.getuid()}:{os.getgid()}"
        assert kwargs["environment"] == {"HOME": "/home/podman"}
        assert kwargs["mounts"] == [
            {"type": "tmpfs", "source": "tmpfs", "target": "/home/podman", "chown": True}
        ]
        assert kwargs["working_dir"] == str(tmp_path)
        assert kwargs["volumes"] == {str(tmp_path): {"bind": str(tmp_path), "mode": "rw"}}

        labels = kwargs["labels"]
        assert labels["app.name"] == "alpine-dev"
        assert labels["app.managed"] == "true"
        assert labels["app.pwd"] == str(tmp_path)
        assert labels["app.hostname"] == "me@box"
        assert "app.created" in labels

    def test_attaches_a_bash_login_shell(self, manager, container, subprocess_run, tmp_path):
        manager.run(pwd=str(tmp_path))

        subprocess_run.assert_called_once_with(
            [
                "/usr/bin/podman", "exec", "-it", "--workdir", str(tmp_path),
                "--env", "TERM=xterm-256color", "c0ffee", "/bin/bash", "-l",
            ],
            check=False,
        )

    @pytest.mark.parametrize("variable", ["CONTAINER_HOST", "DOCKER_HOST"])
    def test_exec_talks_to_the_same_service(self, manager, container, subprocess_run, monkeypatch, tmp_path, variable):
        monkeypatch.setenv(variable, "unix:///run/podman.sock")

        manager.run(pwd=str(tmp_path))

        assert subprocess_run.call_args.args[0][:3] == ["/usr/bin/podman", "--url", "unix:///run/podman.sock"]

    def test_omits_term_when_unset(self, manager, container, subprocess_run, monkeypatch, tmp_path):
        monkeypatch.delenv("TERM")

        manager.run(pwd=str(tmp_path))

        assert "--env" not in subprocess_run.call_args.args[0]

    def test_relative_pwd_is_made_absolute(self, manager, client, container, subprocess_run, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "src").mkdir()

        manager.run(pwd="src")

        assert client.containers.run.call_args.kwargs["labels"]["app.pwd"] == str(tmp_path / "src")

    def test_defaults_to_cwd_and_user_at_host(self, manager, client, container, subprocess_run, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("USER", "finley")
        monkeypatch.setenv("HOSTNAME", "laptop")

        manager.run()

        labels = client.containers.run.call_args.kwargs["labels"]
        assert labels["app.pwd"] == str(tmp_path)
        assert labels["app.hostname"] == "finley@laptop"

    def test_stops_but_keeps_the_container_by_default(self, manager, container, subprocess_run, tmp_path):
        manager.run(pwd=str(tmp_path))

        container.stop.assert_called_once_with(timeout=1)
        container.remove.assert_not_called()

    def test_remove_deletes_the_container(self, manager, container, subprocess_run, tmp_path):
        manager.run(pwd=str(tmp_path), remove=True)

        container.remove.assert_called_once_with(force=True)

    def test_container_is_stopped_even_if_the_shell_fails(self, manager, container, subprocess_run, tmp_path, capsys):
        subprocess_run.side_effect = OSError("exec failed")

        assert manager.run(pwd=str(tmp_path), remove=True) is False
        container.stop.assert_called_once()
        container.remove.assert_called_once()
        assert "Error in container shell: exec failed" in capsys.readouterr().err

    def test_requires_the_podman_cli(self, manager, client, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(daedalus.shutil, "which", lambda name: None)

        assert manager.run(pwd=str(tmp_path)) is False
        client.containers.run.assert_not_called()
        assert "the podman CLI is required" in capsys.readouterr().err

    def test_start_failure_is_reported(self, manager, client, subprocess_run, capsys, tmp_path):
        client.containers.run.side_effect = RuntimeError("no image")

        assert manager.run(pwd=str(tmp_path)) is False
        subprocess_run.assert_not_called()
        assert "Error running container: no image" in capsys.readouterr().err


class TestList:
    def test_queries_managed_containers_of_this_app(self, client):
        client.containers.list.return_value = []

        daedalus.LabelBasedContainerManager(app_name="other").list_containers()

        client.containers.list.assert_called_once_with(
            all=True, filters={"label": ["app.managed=true", "app.name=other"]}
        )

    def test_reports_no_containers(self, manager, client, capsys):
        client.containers.list.return_value = []

        assert manager.list_containers() is True
        assert "No managed containers found" in capsys.readouterr().out

    def test_prints_a_row_per_container(self, manager, client, capsys):
        client.containers.list.return_value = [
            make_container(id="aaaaaaaaaaaaaaaa", names=["one"], labels=managed_labels(**{"app.pwd": "/src/one", "app.hostname": "me@box"})),
            make_container(id="bbbbbbbbbbbbbbbb", names=[], labels=None, status="exited"),
        ]

        assert manager.list_containers() is True
        lines = capsys.readouterr().out.splitlines()
        assert lines[1].split() == ["CONTAINER", "ID", "NAME", "PWD", "HOSTNAME", "STATUS"]
        assert lines[2] == "-" * 95
        assert lines[3].split() == ["aaaaaaaaaaaa", "one", "/src/one", "me@box", "running"]
        assert lines[4].split() == ["bbbbbbbbbbbb", "N/A", "N/A", "N/A", "exited"]

    def test_truncates_columns(self, manager, client, capsys):
        client.containers.list.return_value = [
            make_container(names=["n" * 40], labels={"app.pwd": "/" + "p" * 40, "app.hostname": "h" * 40}, status="s" * 20),
        ]

        manager.list_containers()

        row = capsys.readouterr().out.splitlines()[3]
        assert row == f"{'0123456789ab':<12} {'n' * 20} {('/' + 'p' * 29)} {'h' * 20} {'s' * 10}"

    def test_filters_by_hostname(self, manager, client, capsys):
        client.containers.list.return_value = [
            make_container(names=["mine"], labels={"app.hostname": "me@box"}),
            make_container(names=["theirs"], labels={"app.hostname": "you@box"}),
        ]

        manager.list_containers(filter_by_hostname="me@box")

        out = capsys.readouterr().out
        assert "mine" in out
        assert "theirs" not in out

    def test_errors_are_reported(self, manager, client, capsys):
        client.containers.list.side_effect = RuntimeError("down")

        assert manager.list_containers() is False
        assert "Error listing containers: down" in capsys.readouterr().err


class TestInspect:
    def test_prints_management_labels(self, manager, client, capsys):
        client.containers.get.return_value = make_container(
            labels=managed_labels(**{"app.pwd": "/src", "app.hostname": "me@box", "app.created": "2026-10-05", "app.version": "1.0"})
        )

        assert manager.inspect("0123") is True
        client.containers.get.assert_called_once_with("0123")
        out = capsys.readouterr().out
        assert "Container: 0123456789ab (dev)" in out
        assert "Status:    running" in out
        assert "Image:     sha256:abc" in out
        assert "PWD:      /src" in out
        assert "Hostname: me@box" in out
        assert "Created:  2026-10-05" in out
        assert "Version:  1.0" in out

    def test_unnamed_container_without_image(self, manager, client, capsys):
        container = make_container(names=[], labels=managed_labels())
        container.image = None
        client.containers.get.return_value = container

        assert manager.inspect("0123") is True
        out = capsys.readouterr().out
        assert "(N/A)" in out
        assert "Image:     N/A" in out

    @pytest.mark.parametrize(
        "labels",
        [None, {}, managed_labels(app="other"), {"app.name": "alpine-dev", "app.managed": "false"}],
    )
    def test_refuses_unmanaged_containers(self, manager, client, capsys, labels):
        client.containers.get.return_value = make_container(labels=labels)

        assert manager.inspect("0123") is False
        assert "is not managed by alpine-dev" in capsys.readouterr().out

    def test_errors_are_reported(self, manager, client, capsys):
        client.containers.get.side_effect = RuntimeError("missing")

        assert manager.inspect("0123") is False
        assert "Error inspecting container: missing" in capsys.readouterr().err


class TestStopAndRemove:
    def test_stop_matching_containers(self, manager, client, capsys):
        containers = [make_container(id="a" * 16), make_container(id="b" * 16)]
        client.containers.list.return_value = containers

        assert manager.stop_by_label("app.pwd", "/src") is True
        client.containers.list.assert_called_once_with(
            all=True, filters={"label": ["app.pwd=/src", "app.name=alpine-dev"]}
        )
        for container in containers:
            container.stop.assert_called_once_with()
        assert "Stopped 2 container(s)" in capsys.readouterr().out

    def test_remove_matching_containers(self, manager, client, capsys):
        containers = [make_container()]
        client.containers.list.return_value = containers

        assert manager.remove_by_label("app.pwd", "/src", force=True) is True
        client.containers.list.assert_called_once_with(
            all=True, filters={"label": ["app.pwd=/src", "app.name=alpine-dev"]}
        )
        containers[0].remove.assert_called_once_with(force=True)
        assert "Removed 1 container(s)" in capsys.readouterr().out

    def test_remove_does_not_force_by_default(self, manager, client):
        container = make_container()
        client.containers.list.return_value = [container]

        manager.remove_by_label("k", "v")

        container.remove.assert_called_once_with(force=False)

    @pytest.mark.parametrize("method", ["stop_by_label", "remove_by_label"])
    def test_nothing_matching_is_fine(self, manager, client, capsys, method):
        client.containers.list.return_value = []

        assert getattr(manager, method)("k", "v") is True
        assert "No containers found with label k=v" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "method, message",
        [("stop_by_label", "Error stopping containers: nope"), ("remove_by_label", "Error removing containers: nope")],
    )
    def test_errors_are_reported(self, manager, client, capsys, method, message):
        container = make_container()
        container.stop.side_effect = container.remove.side_effect = RuntimeError("nope")
        client.containers.list.return_value = [container]

        assert getattr(manager, method)("k", "v") is False
        assert message in capsys.readouterr().err


class TestCleanImage:
    def test_removes_the_tagged_image(self, manager, client, capsys):
        image = mock.Mock()
        client.images.list.return_value = [image]

        assert manager.clean_image("v2") is True
        client.images.list.assert_called_once_with(filters={"reference": "alpine-dev:v2"})
        image.remove.assert_called_once_with(force=True)
        assert "Image removed" in capsys.readouterr().out

    def test_missing_image_is_fine(self, manager, client, capsys):
        client.images.list.return_value = []

        assert manager.clean_image() is True
        assert "Image not found: alpine-dev:latest" in capsys.readouterr().out

    def test_errors_are_reported(self, manager, client, capsys):
        client.images.list.side_effect = RuntimeError("busy")

        assert manager.clean_image() is False
        assert "Error removing image: busy" in capsys.readouterr().err


class TestLogs:
    def test_prints_decoded_logs(self, manager, client, capsys):
        container = make_container()
        container.logs.return_value = b"hello\n"
        client.containers.get.return_value = container

        assert manager.logs("0123") is True
        container.logs.assert_called_once_with(stdout=True, stderr=True)
        assert "hello" in capsys.readouterr().out

    def test_errors_are_reported(self, manager, client, capsys):
        client.containers.get.side_effect = RuntimeError("gone")

        assert manager.logs("0123") is False
        assert "Error retrieving logs: gone" in capsys.readouterr().err
