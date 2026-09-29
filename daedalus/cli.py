"""The ``daedalus`` command: everything the Makefile and daedalus.mk used to do.

Project commands (run from any project; settings in .daedalus/config.toml):

    daedalus pull                  pull the pinned daedalus + ariadne images
    daedalus shell                 operator shell: ssh-agent, gpg-agent, libvirt, OpenBao
    daedalus run -- CMD ...        run one command in an operator container
    daedalus run -c 'CMD && CMD'   ... through `bash -lc`
    daedalus agent [CMD ...]       fenced shell for AI agents (alias: labyrinth)
    daedalus agent-up|agent-down   start/stop the egress proxy and agent network
    daedalus agent-check           prove the fence works
    daedalus ask|hyperplan|ultrawork GOAL
                                   daedalus-agent inside the fence
    daedalus digest                print the pinned image's digest
    daedalus clean                 remove the shared volumes
    daedalus config                show the effective settings

Workshop commands (run from the daedalus repo):

    daedalus build|test|lock|push|unit-test
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from daedalus import __version__
from daedalus.config import Config, ConfigError, describe, load, parse_override
from daedalus.podman import EGRESS_NET, CommandError, Podman, Runner, gpg_agent_socket

INSIDE = bool(os.environ.get("DAEDALUS_INSIDE"))
AGENT_MODES = {"ask": "run", "hyperplan": "hyperplan", "ultrawork": "ultrawork"}


class UsageError(Exception):
    pass


class App:
    def __init__(self, args: argparse.Namespace, *, inside: bool = INSIDE) -> None:
        self.args = args
        self.inside = inside
        self.project_dir = Path(args.project_dir).resolve()
        overrides = dict(parse_override(o) for o in args.set)
        for name in ("image", "tag", "digest", "registry", "project"):
            value = getattr(args, name)
            if value is not None:
                overrides[name] = value
        self.config: Config = load(self.project_dir, overrides=overrides)
        self.runner = Runner(dry_run=args.dry_run, echo=args.verbose)
        self.podman = Podman(
            self.config,
            self.project_dir,
            gpg_socket=None if args.dry_run else gpg_agent_socket(),
        )

    # --- helpers ----------------------------------------------------------------

    def host_only(self) -> None:
        if self.inside:
            raise UsageError(
                f"`daedalus {self.args.command}` starts containers; you are already inside one"
            )

    def workshop(self) -> Path:
        if not (self.project_dir / "Containerfile").is_file():
            raise UsageError(
                f"`daedalus {self.args.command}` builds the workshop images; "
                "run it from the daedalus repo (or pass -C)"
            )
        return self.project_dir

    def say(self, text: str) -> None:
        print(text, file=sys.stderr, flush=True)

    def egress_ip(self) -> str:
        return self.runner.output(self.podman.egress_inspect(), placeholder="<egress-ip>")

    # --- project commands ---------------------------------------------------------

    def cmd_pull(self) -> int:
        self.host_only()
        self.runner.run([self.config.podman, "pull", self.config.ref()])
        self.runner.run([self.config.podman, "pull", self.config.egress_ref()])
        return 0

    def cmd_shell(self) -> int:
        self.host_only()
        return self.runner.run(self.podman.operator_run(), check=False)

    def cmd_run(self) -> int:
        command = self._command()
        if self.inside:  # like DD_RUN being empty inside: just run it
            return self.runner.run(command, check=False)
        return self.runner.run(self.podman.operator_run(command), check=False)

    def _command(self) -> list[str]:
        args = self.args
        rest = list(args.cmd)
        if rest and rest[0] == "--":
            rest = rest[1:]
        if args.shell_command and rest:
            raise UsageError("give either -c 'CMD' or -- CMD ..., not both")
        if args.shell_command:
            return ["bash", "-lc", args.shell_command]
        if not rest:
            raise UsageError("usage: daedalus run -- ansible --version   (or -c 'CMD')")
        return rest

    def cmd_agent_up(self) -> int:
        self.host_only()
        p, pm, r = self.podman, self.config.podman, self.runner
        if not p.allowlist.is_file() and not self.args.dry_run:
            raise UsageError(f"missing allowlist {p.allowlist}")
        if not r.ok([pm, "network", "exists", EGRESS_NET]):
            r.run([pm, "network", "create", EGRESS_NET], quiet=True)
        if not r.ok([pm, "network", "exists", p.agent_net]):
            r.run([pm, "network", "create", "--internal", p.agent_net], quiet=True)
        r.run([pm, "rm", "-f", "-i", p.egress_ctr], quiet=True)
        r.run(p.egress_start(), quiet=True)
        r.run([pm, "network", "connect", p.agent_net, p.egress_ctr])
        self.say(f"egress proxy {p.egress_ctr} up; allowlist: {p.allowlist}")
        return 0

    def cmd_agent_down(self) -> int:
        self.host_only()
        pm = self.config.podman
        self.runner.run([pm, "rm", "-f", "-i", self.podman.egress_ctr], check=False, quiet=True)
        self.runner.run([pm, "network", "rm", "-f", self.podman.agent_net], check=False, quiet=True)
        return 0

    def cmd_agent(self) -> int:
        self.host_only()
        self.cmd_agent_up()
        command = [c for c in self.args.cmd if c != "--"] or self.config.agent_cmd
        return self.runner.run(self.podman.agent_run(self.egress_ip(), command), check=False)

    def cmd_agent_check(self) -> int:
        self.host_only()
        self.cmd_agent_up()
        allowed = first_allowed_host(self.podman.allowlist) if not self.args.dry_run else "pypi.org"
        if not allowed:
            raise UsageError(f"{self.podman.allowlist} lists no domains")
        script = (
            f"echo 'allowed : https://{allowed}'; "
            f"curl -sS -o /dev/null -w '  -> %{{http_code}}\\n' https://{allowed}/ || true; "
            "echo 'blocked : https://example.org'; "
            "curl -sS -o /dev/null -w '  -> %{http_code}\\n' https://example.org/ 2>&1 "
            "| sed 's/^/  -> /'; "
            "echo 'direct  : no proxy'; "
            "curl -sS --noproxy '*' -m 5 -o /dev/null https://1.1.1.1/ 2>&1 "
            "| sed 's/^/  -> /' || true"
        )
        return self.runner.run(
            self.podman.agent_run(self.egress_ip(), ["bash", "-c", script]), check=False
        )

    def cmd_agent_mode(self) -> int:
        c = self.config
        command = ["daedalus-agent", AGENT_MODES[self.args.command]]
        if c.agent_tools:
            command += ["--tool", ",".join(c.agent_tools)]
        if c.agent_model:
            command += ["--model", c.agent_model]
        extra = [a for a in self.args.agent_args if a != "--"]
        command += [*c.agent_args, *extra, self.args.goal]
        if self.inside:
            return self.runner.run(command, check=False)
        self.cmd_agent_up()
        return self.runner.run(self.podman.agent_run(self.egress_ip(), command), check=False)

    def cmd_digest(self) -> int:
        self.host_only()
        c = self.config
        ref = f"{c.resolved_image()}:{c.tag}"
        print(self.runner.output([c.podman, "image", "inspect", "--format", "{{.Digest}}", ref]))
        return 0

    def cmd_clean(self) -> int:
        self.host_only()
        self.cmd_agent_down()
        project = self.podman.project
        volumes = [
            "daedalus-containers", "daedalus-vagrant", "daedalus-cache", "daedalus-history",
            f"labyrinth-{project}-cache", f"labyrinth-{project}-history",
        ]  # fmt: skip
        self.runner.run([self.config.podman, "volume", "rm", *volumes], check=False)
        return 0

    def cmd_config(self) -> int:
        print(describe(self.config))
        print(f"# image ref: {self.config.ref()}")
        print(f"# egress ref: {self.config.egress_ref()}")
        return 0

    # --- workshop commands ----------------------------------------------------------

    def _tags(self, image: str) -> list[str]:
        return ["-t", f"{image}:{self.config.tag}", "-t", f"{image}:latest"]

    def cmd_build(self) -> int:
        self.host_only()
        root, c = self.workshop(), self.config
        only = self.args.only
        if only in (None, "daedalus"):
            self.runner.run([
                c.podman, "build", "-f", str(root / "Containerfile"),
                "--build-arg", f"FEDORA_VERSION={c.fedora_version}",
                "--build-arg", f"OPENBAO_VERSION={c.openbao_version}",
                "--build-arg", f"FJ_VERSION={c.fj_version}",
                "--build-arg", f"FJ_SHA256={c.fj_sha256}",
                *self._tags(c.resolved_image()), str(root),
            ])  # fmt: skip
        if only in (None, "ariadne"):
            self.runner.run([
                c.podman, "build", "-f", str(root / "ariadne/Containerfile"),
                "--build-arg", f"FEDORA_VERSION={c.fedora_version}",
                *self._tags(c.resolved_egress_image()), str(root / "ariadne"),
            ])  # fmt: skip
        return 0

    def cmd_test(self) -> int:
        self.host_only()
        c = self.config
        return self.runner.run(
            [
                c.podman,
                "run",
                "--rm",
                f"--userns=keep-id:uid={c.uid},gid={c.uid}",
                "--security-opt",
                "label=disable",
                "--device",
                "/dev/fuse",
                "-e",
                "DAEDALUS_SELFTEST_BUILD=1",
                f"{c.resolved_image()}:{c.tag}",
                "daedalus-selftest",
            ],  # fmt: skip
            check=False,
        )

    def cmd_lock(self) -> int:
        """Resolve in the image's Fedora release so the Python version matches."""
        self.host_only()
        root, c = self.workshop(), self.config
        compile_ = "uv pip compile --universal --generate-hashes --python /usr/bin/python3"
        script = (
            "dnf -y -q install uv python3 >/dev/null && "
            f"{compile_} requirements-tools.in -o requirements-tools.txt && "
            f"{compile_} agent/pyproject.toml -o agent/requirements.txt"
        )
        return self.runner.run([
            c.podman, "run", "--rm", "--security-opt", "label=disable",
            "-v", f"{root}:/src", "-w", "/src",
            f"registry.fedoraproject.org/fedora:{c.fedora_version}", "bash", "-c", script,
        ])  # fmt: skip

    def cmd_push(self) -> int:
        self.host_only()
        self.workshop()
        c = self.config
        for image in (c.resolved_image(), c.resolved_egress_image()):
            for tag in (c.tag, "latest"):
                self.runner.run([c.podman, "push", f"{image}:{tag}"])
        inspect = [c.podman, "image", "inspect", "--format", "{{.Digest}}"]
        digest = self.runner.output(
            [*inspect, f"{c.resolved_image()}:{c.tag}"], placeholder="sha256:<digest>"
        )
        self.say(f'pin in projects with: digest = "{digest}"   (.daedalus/config.toml)')
        return 0

    def cmd_unit_test(self) -> int:
        root = self.workshop()
        own = ["uv", "run", "--directory", str(root), "--with", "pytest", "pytest", "-q", "tests"]
        code = self.runner.run(own, check=False)
        agent = [
            "uv", "run", "--directory", str(root / "agent"), "--python", "3.12",
            "--extra", "test", "pytest", "-q",
        ]  # fmt: skip
        return code or self.runner.run(agent, check=False)


def first_allowed_host(allowlist: Path) -> str:
    for line in allowlist.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line.lstrip(".")
    return ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="daedalus",
        description="Run a project's work inside the pinned Daedalus workshop container.",
        epilog="Settings: .daedalus/config.toml < DAEDALUS_<NAME> env < -o name=value. "
        "`daedalus config` lists them.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-C", "--project-dir", default=".", help="project directory (default: current)"
    )
    parser.add_argument(
        "-o", "--set", action="append", default=[], metavar="NAME=VALUE",
        help="override a setting (repeatable)",
    )  # fmt: skip
    parser.add_argument("--image", help="daedalus image (default: <registry>/daedalus)")
    parser.add_argument("--tag", help="image tag")
    parser.add_argument("--digest", help="pin the image by sha256 digest")
    parser.add_argument("--registry", help="registry prefix for both images")
    parser.add_argument("--project", help="project name (default: directory name)")
    parser.add_argument("-n", "--dry-run", action="store_true", help="print commands, run nothing")
    parser.add_argument("-v", "--verbose", action="store_true", help="print commands as they run")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name: str, help: str, handler: Callable[[App], int], **kw) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help, description=help, **kw)
        p.set_defaults(handler=handler)
        return p

    add("pull", "pull the pinned daedalus and ariadne images", App.cmd_pull)
    add("shell", "operator shell with your agents and credentials forwarded", App.cmd_shell)
    run = add("run", "run one command in an operator container", App.cmd_run)
    run.add_argument("-c", dest="shell_command", metavar="CMD", help="run CMD with `bash -lc`")
    run.add_argument("cmd", nargs=argparse.REMAINDER, help="-- COMMAND [ARGS...]")
    agent = add(
        "agent", "fenced shell for AI agents (allowlisted egress only)", App.cmd_agent,
        aliases=["labyrinth"],
    )  # fmt: skip
    agent.add_argument("cmd", nargs=argparse.REMAINDER, help="command (default: agent_cmd)")
    add("agent-up", "start the egress proxy and the agent network", App.cmd_agent_up)
    add("agent-down", "stop the egress proxy and remove the agent network", App.cmd_agent_down)
    add("agent-check", "prove the fence: allowed host OK, others blocked", App.cmd_agent_check)
    for mode, what in (
        ("ask", "one daedalus-agent, only the tools in agent_tools"),
        ("hyperplan", "a daedalus-agent planning team writes a plan"),
        ("ultrawork", "plan and build with daedalus-agent teams"),
    ):
        p = add(mode, what, App.cmd_agent_mode)
        p.add_argument("goal", help="what the agent should do")
        p.add_argument(
            "agent_args", nargs=argparse.REMAINDER,
            help="extra daedalus-agent flags after --, e.g. -- --planners 6",
        )  # fmt: skip
    add("digest", "print the digest of the pinned image", App.cmd_digest)
    add("clean", "remove the shared operator and agent volumes", App.cmd_clean)
    add("config", "show the effective settings", App.cmd_config)

    build = add("build", "build the daedalus and ariadne images (workshop repo)", App.cmd_build)
    build.add_argument("--only", choices=["daedalus", "ariadne"], help="build just one image")
    add("test", "run daedalus-selftest in the built image", App.cmd_test)
    add("lock", "re-pin requirements-tools.txt and agent/requirements.txt", App.cmd_lock)
    add("push", "push both images (tag and latest)", App.cmd_push)
    add("unit-test", "run the daedalus and daedalus-agent unit tests", App.cmd_unit_test)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(App(args))
    except (UsageError, ConfigError) as exc:
        print(f"daedalus: {exc}", file=sys.stderr)
        return 2
    except CommandError as exc:
        print(f"daedalus: {exc}", file=sys.stderr)
        return exc.code
    except FileNotFoundError as exc:
        print(f"daedalus: {exc.filename or exc}: not found", file=sys.stderr)
        return 127
    except KeyboardInterrupt:
        return 130
