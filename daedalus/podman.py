"""The podman command lines, as argv lists (no shell quoting to get wrong)."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from daedalus.config import Config

HOME = "/home/op"
EGRESS_NET = "ariadne-net"
PROXY = "http://ariadne:3128"


class CommandError(RuntimeError):
    def __init__(self, argv: Sequence[str], code: int) -> None:
        super().__init__(f"command failed ({code}): {shlex.join(argv)}")
        self.code = code


@dataclass(slots=True)
class Runner:
    """Runs commands, or with ``dry_run`` only prints them."""

    dry_run: bool = False
    echo: bool = False
    log: list[list[str]] = field(default_factory=list)

    def _show(self, argv: Sequence[str]) -> None:
        if self.dry_run or self.echo:
            print(f"+ {shlex.join(argv)}", file=sys.stderr, flush=True)

    def run(self, argv: Sequence[str], *, check: bool = True, quiet: bool = False) -> int:
        self.log.append(list(argv))
        self._show(argv)
        if self.dry_run:
            return 0
        out = subprocess.DEVNULL if quiet else None
        code = subprocess.run(list(argv), stdout=out, stderr=out if quiet else None).returncode
        if check and code != 0:
            raise CommandError(argv, code)
        return code

    def output(self, argv: Sequence[str], *, placeholder: str = "") -> str:
        self.log.append(list(argv))
        self._show(argv)
        if self.dry_run:
            return placeholder
        result = subprocess.run(list(argv), capture_output=True, text=True)
        if result.returncode != 0:
            raise CommandError(argv, result.returncode)
        return result.stdout.strip()

    def ok(self, argv: Sequence[str]) -> bool:
        """True when the command succeeds (a probe; in a dry run, assume not)."""
        self.log.append(list(argv))
        self._show(argv)
        if self.dry_run:
            return False
        return subprocess.run(list(argv), capture_output=True).returncode == 0


def gpg_agent_socket() -> str | None:
    try:
        result = subprocess.run(
            ["gpgconf", "--list-dirs", "agent-socket"], capture_output=True, text=True
        )
    except OSError:
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


@dataclass(slots=True)
class Podman:
    config: Config
    project_dir: Path
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    home: Path = field(default_factory=Path.home)
    tty: bool = field(default_factory=lambda: sys.stdin.isatty())
    gpg_socket: str | None = None

    @property
    def project(self) -> str:
        return self.config.project_name(self.project_dir)

    @property
    def agent_net(self) -> str:
        return f"labyrinth-{self.project}"

    @property
    def egress_ctr(self) -> str:
        return f"ariadne-{self.project}"

    @property
    def allowlist(self) -> Path:
        return (self.project_dir / self.config.agent_allowlist).resolve()

    def base(self) -> list[str]:
        return [self.config.podman]

    # Shared by both modes. The project is mounted at the SAME path as on the
    # host so paths in libvirt/vagrant/molecule state stay valid on both sides.
    def common(self) -> list[str]:
        uid = self.config.uid
        d = str(self.project_dir)
        args = ["--rm", "--init", "-i"] + (["-t"] if self.tty else [])
        return args + [
            f"--userns=keep-id:uid={uid},gid={uid}",
            "--security-opt", "label=disable",
            "--hostname", f"daedalus-{self.project}",
            "-e", f"DAEDALUS_PROJECT={self.project}",
            "-e", "TERM", "-e", "COLORTERM", "-e", "TZ",
            "-v", f"{d}:{d}", "-w", d,
        ]  # fmt: skip

    # Operator: forwards your agents and credentials, full network.
    def operator(self) -> list[str]:
        c, env, home = self.config, self.environ, self.home
        args = [
            "-e", "DAEDALUS_MODE=operator",
            "--network", c.net,
            "--device", "/dev/fuse",
            "-v", f"daedalus-containers:{HOME}/.local/share/containers",
            "-v", f"daedalus-vagrant:{HOME}/.vagrant.d",
            "-v", f"daedalus-cache:{HOME}/.cache",
            "-v", f"daedalus-history:{HOME}/.history",
        ]  # fmt: skip

        def mount(src: str | Path | None, dst: str, opts: str = "") -> None:
            if src and os.path.exists(src):
                args.extend(["-v", f"{src}:{dst}{opts}"])

        ssh_sock = env.get("SSH_AUTH_SOCK")
        if ssh_sock and os.path.exists(ssh_sock):
            args += ["-v", f"{ssh_sock}:/run/host/ssh-agent.sock"]
            args += ["-e", "SSH_AUTH_SOCK=/run/host/ssh-agent.sock"]
        mount(home / ".ssh/config", f"{HOME}/.ssh/config", ":ro")
        mount(home / ".ssh/known_hosts", f"{HOME}/.ssh/known_hosts")
        mount(home / ".gitconfig", f"{HOME}/.gitconfig", ":ro")
        mount(home / ".gnupg", f"{HOME}/.gnupg")
        mount(self.gpg_socket, f"/run/user/{c.uid}/gnupg/S.gpg-agent")
        if os.path.exists(c.libvirt_sock):
            args += ["-v", f"{c.libvirt_sock}:/var/run/libvirt/libvirt-sock"]
            args += ["-e", "LIBVIRT_DEFAULT_URI=qemu:///system"]
        if c.libvirt_keep_groups:
            args += ["--group-add", "keep-groups"]
        args += ["-e", "BAO_ADDR", "-e", "BAO_NAMESPACE", "-e", "BAO_TOKEN"]
        if env.get("BAO_CACERT"):
            args += ["-v", f"{env['BAO_CACERT']}:/run/host/bao-ca.pem:ro"]
            args += ["-e", "BAO_CACERT=/run/host/bao-ca.pem"]
        mount(home / ".vault-token", f"{HOME}/.vault-token")
        mount(home / ".local/share/forgejo-cli", f"{HOME}/.local/share/forgejo-cli")
        return args + list(c.extra_args)

    # Agent: no host sockets, no credentials, own volumes (so it cannot poison
    # the operator's image store, box cache or pip cache), internal network only.
    def agent(self) -> list[str]:
        c = self.config
        args = [
            "-e", "DAEDALUS_MODE=agent",
            "--network", self.agent_net, "--dns", "none",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", c.agent_memory, "--cpus", c.agent_cpus, "--pids-limit", c.agent_pids,
            "-v", f"labyrinth-{self.project}-cache:{HOME}/.cache",
            "-v", f"labyrinth-{self.project}-history:{HOME}/.history",
        ]  # fmt: skip
        git = self.project_dir / ".git"
        if c.agent_git_ro and git.exists():
            args += ["-v", f"{git}:{git}:ro"]
        for name in c.agent_env:
            args += ["-e", name]
        return args

    # The proxy is addressed by IP + --add-host because the agent has no DNS at
    # all (--dns none), which also closes DNS as an exfiltration channel.
    def proxy(self, egress_ip: str) -> list[str]:
        args = ["--add-host", f"ariadne:{egress_ip}"]
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            args += ["-e", f"{var}={PROXY}"]
        for var in ("NO_PROXY", "no_proxy"):
            args += ["-e", f"{var}=localhost,127.0.0.1"]
        return args

    # --- whole commands -------------------------------------------------------

    def operator_run(self, command: Sequence[str] = ()) -> list[str]:
        return [*self.base(), "run", *self.common(), *self.operator(), self.config.ref(), *command]

    def agent_run(self, egress_ip: str, command: Sequence[str] = ()) -> list[str]:
        return [
            *self.base(), "run", *self.common(), *self.agent(), *self.proxy(egress_ip),
            self.config.ref(), *command,
        ]  # fmt: skip

    def egress_inspect(self) -> list[str]:
        template = f'{{{{ (index .NetworkSettings.Networks "{self.agent_net}").IPAddress }}}}'
        return [*self.base(), "inspect", "-f", template, self.egress_ctr]

    def egress_start(self) -> list[str]:
        return [
            *self.base(), "run", "-d", "--name", self.egress_ctr, "--network", EGRESS_NET,
            "--security-opt", "label=disable", "--read-only",
            "--tmpfs", "/var/spool/squid", "--tmpfs", "/run/squid", "--tmpfs", "/var/log/squid",
            "-v", f"{self.allowlist}:/etc/squid/allowlist.txt:ro",
            self.config.egress_ref(),
        ]  # fmt: skip
