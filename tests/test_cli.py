from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from daedalus import cli
from daedalus.config import Config, ConfigError, load
from daedalus.podman import Podman


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".daedalus").mkdir(parents=True)
    (root / ".daedalus/agent-allowlist.txt").write_text("# comment\n\n.pypi.org\n")
    (root / ".git").mkdir()
    return root


def dry(project: Path, *argv: str, inside: bool = False) -> list[str]:
    """Run the CLI in dry-run mode and return the commands it would run."""
    args = cli.build_parser().parse_args(["-C", str(project), "-n", *argv])
    app = cli.App(args, inside=inside)
    code = args.handler(app)
    assert code == 0
    return [shlex.join(c) for c in app.runner.log]


# --- config -----------------------------------------------------------------


def test_precedence(project: Path) -> None:
    (project / ".daedalus/config.toml").write_text(
        'tag = "1.0"\nimage = "reg/dd"\nagent_memory = "2g"\nagent_tools = ["read_file"]\n'
    )
    env = {"DAEDALUS_AGENT_MEMORY": "4g", "DAEDALUS_UID": "1234", "UNRELATED": "x"}
    config = load(project, environ=env, overrides={"tag": "2.0"})
    assert config.ref() == "reg/dd:2.0"
    assert config.agent_memory == "4g"
    assert config.uid == 1234
    assert config.agent_tools == ["read_file"]


def test_env_types(project: Path) -> None:
    env = {
        "DAEDALUS_AGENT_TOOLS": "read_file, grep",
        "DAEDALUS_LIBVIRT_KEEP_GROUPS": "1",
        "DAEDALUS_EXTRA_ARGS": "--cap-add SYS_PTRACE -e 'A=b c'",
        "DAEDALUS_MODE": "agent",  # set inside the container; not a setting, ignored
    }
    config = load(project, environ=env)
    assert config.agent_tools == ["read_file", "grep"]
    assert config.libvirt_keep_groups is True
    assert config.extra_args == ["--cap-add", "SYS_PTRACE", "-e", "A=b c"]


def test_bad_settings(project: Path) -> None:
    with pytest.raises(ConfigError, match="unknown setting"):
        load(project, environ={}, overrides={"nope": "1"})
    with pytest.raises(ConfigError, match="integer"):
        load(project, environ={}, overrides={"uid": "x"})
    (project / ".daedalus/config.toml").write_text("tag = \n")
    with pytest.raises(ConfigError):
        load(project, environ={})


def test_refs() -> None:
    c = Config(registry="r", tag="t")
    assert (c.ref(), c.egress_ref()) == ("r/daedalus:t", "r/ariadne:t")
    c.digest = "sha256:abc"
    c.egress_tag = "e"
    assert (c.ref(), c.egress_ref()) == ("r/daedalus@sha256:abc", "r/ariadne:e")


# --- podman command lines -------------------------------------------------------


def test_operator_forwards_only_what_exists(project: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh/config").write_text("")
    sock = tmp_path / "ssh.sock"
    sock.write_text("")
    config = Config(libvirt_sock=str(tmp_path / "missing"))
    pm = Podman(config, project, environ={"SSH_AUTH_SOCK": str(sock)}, home=home, tty=False)
    args = shlex.join(pm.operator_run(["true"]))
    assert f"-v {home}/.ssh/config:/home/op/.ssh/config:ro" in args
    assert f"-v {sock}:/run/host/ssh-agent.sock" in args
    assert ".gitconfig" not in args and "libvirt" not in args
    assert f"-v {project}:{project} -w {project}" in args
    assert " -t " not in args
    assert args.endswith(f"{config.ref()} true")


def test_agent_has_no_host_secrets(project: Path) -> None:
    pm = Podman(Config(), project, environ={"SSH_AUTH_SOCK": "/tmp"}, tty=True)
    args = pm.agent_run("10.0.0.2", ["bash"])
    joined = shlex.join(args)
    assert "--dns none" in joined and "--cap-drop ALL" in joined
    assert f"-v {project}/.git:{project}/.git:ro" in joined
    assert "--add-host ariadne:10.0.0.2" in joined
    assert "SSH_AUTH_SOCK" not in joined and ".gnupg" not in joined
    assert "-t" in args


# --- commands (dry run) -----------------------------------------------------------


def test_run_forms(project: Path) -> None:
    [cmd] = dry(project, "run", "--", "ansible", "--version")
    assert cmd.startswith("podman run --rm --init -i") and cmd.endswith("ansible --version")
    [cmd] = dry(project, "run", "-c", "make lint && make test")
    assert cmd.endswith("bash -lc 'make lint && make test'")


def test_run_inside_the_container_runs_directly(project: Path) -> None:
    assert dry(project, "run", "--", "pytest", "-q", inside=True) == ["pytest -q"]


def test_host_commands_refuse_inside(project: Path) -> None:
    args = cli.build_parser().parse_args(["-C", str(project), "-n", "agent"])
    with pytest.raises(cli.UsageError, match="already inside"):
        args.handler(cli.App(args, inside=True))


def test_run_usage(project: Path, capsys) -> None:
    assert cli.main(["-C", str(project), "-n", "run"]) == 2
    assert "usage: daedalus run" in capsys.readouterr().err


def test_agent_modes(project: Path) -> None:
    (project / ".daedalus/config.toml").write_text(
        'agent_tools = ["read_file", "grep"]\nagent_model = "openai:m"\n'
    )
    cmds = dry(project, "ultrawork", "fix it's bug", "--", "--planners", "6")
    assert any("network create --internal labyrinth-proj" in c for c in cmds)
    assert cmds[-1].endswith(
        "daedalus-agent ultrawork --tool read_file,grep --model openai:m --planners 6 "
        "'fix it'\"'\"'s bug'"
    )
    # Inside the container the agent runs directly, no fence setup.
    assert dry(project, "ask", "why?", inside=True) == [
        "daedalus-agent run --tool read_file,grep --model openai:m 'why?'"
    ]


def test_agent_shell_default_command(project: Path) -> None:
    assert dry(project, "labyrinth")[-1].endswith("daedalus:" + Config().tag + " bash -l")
    assert dry(project, "agent", "--", "zsh")[-1].endswith(" zsh")


def test_workshop_commands_need_the_repo(project: Path, capsys) -> None:
    assert cli.main(["-C", str(project), "-n", "build"]) == 2
    assert "daedalus repo" in capsys.readouterr().err


def test_build_and_push_in_repo() -> None:
    repo = Path(__file__).resolve().parents[1]
    cmds = dry(repo, "--registry", "reg", "--tag", "9", "build")
    assert cmds[0].startswith(f"podman build -f {repo}/Containerfile")
    assert "-t reg/daedalus:9 -t reg/daedalus:latest" in cmds[0]
    assert "-t reg/ariadne:9 -t reg/ariadne:latest" in cmds[1]
    assert dry(repo, "--tag", "9", "build", "--only", "ariadne")[0].endswith(f"{repo}/ariadne")
    pushes = dry(repo, "--registry", "reg", "--tag", "9", "push")
    assert [c for c in pushes if " push " in c] == [
        "podman push reg/daedalus:9",
        "podman push reg/daedalus:latest",
        "podman push reg/ariadne:9",
        "podman push reg/ariadne:latest",
    ]


def test_config_command(project: Path, capsys) -> None:
    assert cli.main(["-C", str(project), "-o", "agent_memory=1g", "config"]) == 0
    out = capsys.readouterr().out
    assert "agent_memory = 1g" in out and "# image ref:" in out
