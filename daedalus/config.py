"""Settings: defaults < .daedalus/config.toml < DAEDALUS_* env < command line.

Every field ``name`` can be set in the project's ``.daedalus/config.toml`` as
``name = ...``, in the environment as ``DAEDALUS_<NAME>``, or on the command
line with ``-o name=value``. These replace the variables daedalus.mk used to
take (DAEDALUS_IMAGE, AGENT_MEMORY, ...).
"""

from __future__ import annotations

import datetime
import os
import shlex
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, get_type_hints

CONFIG_FILE = Path(".daedalus/config.toml")
ENV_PREFIX = "DAEDALUS_"


def _default_tag() -> str:
    return datetime.date.today().strftime("%Y.%m") + ".1"


@dataclass(slots=True)
class Config:
    # --- images -------------------------------------------------------------
    registry: str = "git.example.com/ops"
    image: str = ""  # default: <registry>/daedalus
    tag: str = field(default_factory=_default_tag)
    digest: str = ""  # sha256:... pins immutably (`daedalus digest` prints it)
    egress_image: str = ""  # default: <registry>/ariadne
    egress_tag: str = ""  # default: tag
    podman: str = "podman"

    # --- containers ---------------------------------------------------------
    project: str = ""  # default: name of the project directory
    uid: int = 1000  # container-side uid of `op`; your host uid is mapped onto it
    net: str = "host"  # operator network (host = simplest for vagrant-libvirt)
    extra_args: list[str] = field(default_factory=list)  # extra operator `podman run` args
    libvirt_sock: str = "/var/run/libvirt/libvirt-sock"
    libvirt_keep_groups: bool = False  # for group-restricted (non-polkit) libvirt sockets

    # --- agent fence --------------------------------------------------------
    agent_allowlist: str = ".daedalus/agent-allowlist.txt"
    agent_git_ro: bool = True
    agent_memory: str = "8g"
    agent_cpus: str = "4"
    agent_pids: str = "4096"
    # Host env vars passed into the agent container by name; unset ones are skipped.
    agent_env: list[str] = field(
        default_factory=lambda: [
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_BASE_URL",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "DAEDALUS_AGENT_MODEL",
            "DAEDALUS_AGENT_TOOLS",
        ]
    )
    agent_cmd: list[str] = field(default_factory=lambda: ["bash", "-l"])
    # daedalus-agent: tools stay OFF unless listed; model is provider:model.
    agent_tools: list[str] = field(default_factory=list)
    agent_model: str = ""
    agent_args: list[str] = field(default_factory=list)

    # --- building the workshop images (this repo) ---------------------------
    fedora_version: str = "44"
    openbao_version: str = "2.7.0"
    fj_version: str = "0.6.0"
    fj_sha256: str = ""

    # --- derived --------------------------------------------------------------

    def resolved_image(self) -> str:
        return self.image or f"{self.registry}/daedalus"

    def resolved_egress_image(self) -> str:
        return self.egress_image or f"{self.registry}/ariadne"

    def ref(self) -> str:
        if self.digest:
            return f"{self.resolved_image()}@{self.digest}"
        return f"{self.resolved_image()}:{self.tag}"

    def egress_ref(self) -> str:
        return f"{self.resolved_egress_image()}:{self.egress_tag or self.tag}"

    def project_name(self, project_dir: Path) -> str:
        return self.project or project_dir.name


class ConfigError(ValueError):
    pass


_HINTS: dict[str, Any] = {}


def _field_types() -> dict[str, Any]:
    if not _HINTS:
        hints = get_type_hints(Config)
        _HINTS.update({f.name: hints[f.name] for f in fields(Config)})
    return _HINTS


def _coerce(name: str, value: Any) -> Any:
    """Turn a TOML value or a string (env, -o) into the field's type."""
    kind = _field_types()[name]
    if kind is bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off", ""}:
            return False
        raise ConfigError(f"{name}: expected a boolean, got {value!r}")
    if kind is int:
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{name}: expected an integer, got {value!r}") from None
    if kind == list[str]:
        if isinstance(value, list):
            return [str(v) for v in value]
        text = str(value)
        # Comma lists for names (tools, env vars); shell words otherwise.
        if name in {"agent_tools", "agent_env"}:
            return [p for p in text.replace(",", " ").split() if p]
        return shlex.split(text)
    return str(value)


def apply(config: Config, values: Mapping[str, Any], source: str) -> None:
    known = _field_types()
    for key, value in values.items():
        name = key.replace("-", "_")
        if name not in known:
            raise ConfigError(f"{source}: unknown setting {key!r}")
        setattr(config, name, _coerce(name, value))


def load(
    project_dir: Path,
    *,
    overrides: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Config:
    config = Config()
    path = project_dir / CONFIG_FILE
    if path.is_file():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from None
        apply(config, data, str(path))
    env = os.environ if environ is None else environ
    known = _field_types()
    from_env = {
        key[len(ENV_PREFIX) :].lower(): value
        for key, value in env.items()
        if key.startswith(ENV_PREFIX) and key[len(ENV_PREFIX) :].lower() in known
    }
    apply(config, from_env, "environment")
    if overrides:
        apply(config, overrides, "command line")
    return config


def parse_override(text: str) -> tuple[str, str]:
    key, sep, value = text.partition("=")
    if not sep or not key:
        raise ConfigError(f"-o expects key=value, got {text!r}")
    return key.strip(), value


def describe(config: Config) -> str:
    lines = []
    for f in fields(Config):
        value = getattr(config, f.name)
        shown = " ".join(shlex.quote(v) for v in value) if isinstance(value, list) else value
        lines.append(f"{f.name} = {shown!s}")
    return "\n".join(lines)
