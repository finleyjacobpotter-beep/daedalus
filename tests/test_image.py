import json
import shutil
import subprocess
from pathlib import Path

import pytest
from podman.errors import BuildError

import daedalus


class TestContainerfile:
    def test_installs_the_shell_and_tools(self):
        packages = daedalus.CONTAINERFILE.split("apk add --no-cache")[1].split("\n\n")[0].split()
        for package in ["bash", "neovim", "git", "openssh-client", "podman", "nix", "shadow", "tmux"]:
            assert package in packages

    def test_sets_a_system_git_identity(self):
        """--global would live in HOME, which is a tmpfs at run time."""
        assert 'git config --system user.name "daedalus"' in daedalus.CONTAINERFILE
        assert 'git config --system user.email "daedalus@localhost.local"' in daedalus.CONTAINERFILE
        assert "--global" not in daedalus.CONTAINERFILE

    def test_git_identity_is_set_as_root(self):
        """/etc/gitconfig is only writable before USER drops to podman."""
        assert daedalus.CONTAINERFILE.index("git config --system") < daedalus.CONTAINERFILE.index("USER podman")

    def test_installs_the_profile_script_outside_home(self):
        assert "COPY daedalus.sh /etc/profile.d/daedalus.sh" in daedalus.CONTAINERFILE

    def test_runs_as_the_podman_user(self):
        assert "USER podman" in daedalus.CONTAINERFILE
        assert "WORKDIR /home/podman" in daedalus.CONTAINERFILE
        assert daedalus.CONTAINER_HOME == "/home/podman"

    def test_entrypoint_is_a_bash_login_shell(self):
        assert 'ENTRYPOINT ["/bin/bash", "-l"]' in daedalus.CONTAINERFILE


bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")


@pytest.fixture
def profile(tmp_path):
    path = tmp_path / "daedalus.sh"
    path.write_text(daedalus.PROFILE_SCRIPT)
    return path


def fake_bin(tmp_path, name, body):
    bin = tmp_path / "bin"
    bin.mkdir(exist_ok=True)
    script = bin / name
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return bin


def source(profile, command, path, home, stdin=""):
    return subprocess.run(
        ["bash", "-c", f'. "{profile}"; {command}'],
        input=stdin,
        capture_output=True,
        text=True,
        env={"PATH": f"{path}:/usr/bin:/bin", "HOME": str(home)},
        check=True,
    )


@bash
class TestProfileScript:
    def test_set_daedalus_secret_exports_without_echo(self, tmp_path, profile):
        bin = fake_bin(tmp_path, "ssh-add", "exit 0")

        result = source(profile, "set_daedalus_secret; echo \"[$DAEDALUS_SECRET]\"", bin, tmp_path, "hunter2\n")

        assert result.stdout.strip() == "[hunter2]"
        assert result.stdout.count("hunter2") == 1

    def test_reuses_a_running_agent(self, tmp_path, profile):
        bin = fake_bin(tmp_path, "ssh-add", "exit 0")
        fake_bin(tmp_path, "ssh-agent", f"touch {tmp_path}/agent-started")

        result = source(profile, 'echo "$SSH_AUTH_SOCK"', bin, tmp_path)

        assert result.stdout.strip() == f"{tmp_path}/.ssh/agent.sock"
        assert not (tmp_path / "agent-started").exists()

    def test_starts_an_agent_when_none_is_reachable(self, tmp_path, profile):
        bin = fake_bin(tmp_path, "ssh-add", "exit 2")
        fake_bin(tmp_path, "ssh-agent", f'echo "$@" > {tmp_path}/agent-args')

        source(profile, "true", bin, tmp_path)

        assert (tmp_path / "agent-args").read_text().split() == ["-s", "-a", f"{tmp_path}/.ssh/agent.sock"]
        assert (tmp_path / ".ssh").stat().st_mode & 0o777 == 0o700


class TestPrintBuildLog:
    @pytest.mark.parametrize(
        "line, expected",
        [
            (json.dumps({"stream": "STEP 1/5\n"}), "STEP 1/5\n"),
            (json.dumps({"stream": "STEP 1/5\n"}).encode(), "STEP 1/5\n"),
            (json.dumps({"error": "no space\n"}), "no space\n"),
            (json.dumps({"aux": {"ID": "x"}}), ""),
            ("not json\n", "not json\n"),
            (b"\xffraw\n", "�raw\n"),
        ],
    )
    def test_prints_the_useful_text(self, capsys, line, expected):
        daedalus.print_build_log(line)

        assert capsys.readouterr().out == expected


class TestBuild:
    def test_builds_from_a_context_with_both_embedded_files(self, manager, client):
        seen = {}

        def build(**kwargs):
            context = Path(kwargs["path"])
            seen["files"] = {p.name: p.read_text() for p in context.iterdir()}
            return client.image, iter([])

        client.images.build.side_effect = build

        assert manager.build("v2") is True
        assert seen["files"] == {
            "Containerfile": daedalus.CONTAINERFILE,
            "daedalus.sh": daedalus.PROFILE_SCRIPT,
        }
        kwargs = client.images.build.call_args.kwargs
        assert kwargs["dockerfile"] == "Containerfile"
        assert kwargs["tag"] == "alpine-dev:v2"
        assert kwargs["labels"] == {"app.name": "alpine-dev", "app.managed": "true"}

    def test_context_is_cleaned_up(self, manager, client):
        client.images.build.return_value = (client.image, iter([]))

        manager.build()

        assert not Path(client.images.build.call_args.kwargs["path"]).exists()

    def test_prints_the_build_log(self, manager, client, capsys):
        client.image.short_id = "abc123"
        client.images.build.return_value = (client.image, iter([json.dumps({"stream": "STEP 1/5"})]))

        assert manager.build() is True
        out = capsys.readouterr().out
        assert "STEP 1/5" in out
        assert "Image built successfully: alpine-dev:latest (abc123)" in out

    def test_build_error_prints_its_log(self, manager, client, capsys):
        client.images.build.side_effect = BuildError("failed", iter([json.dumps({"error": "apk failed"})]))

        assert manager.build() is False
        captured = capsys.readouterr()
        assert "apk failed" in captured.out
        assert "Error building image: failed" in captured.err

    def test_other_errors_fail_the_build(self, manager, client, capsys):
        client.images.build.side_effect = ConnectionError("no socket")

        assert manager.build() is False
        assert "Error building image: no socket" in capsys.readouterr().err
