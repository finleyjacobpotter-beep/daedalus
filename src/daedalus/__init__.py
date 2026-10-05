#!/usr/bin/env python3
"""
Stateless container manager using podman-py with label-based discovery.
Install: pip install podman
"""

import argparse
import json
import sys
from datetime import datetime
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
import functools
import podman
from podman.errors import BuildError


# Home directory inside the container; mounted as a tmpfs since the root filesystem is read-only
CONTAINER_HOME = "/home/podman"

CONTAINERFILE = r"""FROM alpine:latest

RUN apk add --no-cache \
    bash neovim git openssh-client podman nix shadow tmux

RUN addgroup -S podman && \
    adduser -S podman -G podman && \
    mkdir -p /home/podman && \
    chown -R podman /home/podman

# System-wide, because HOME is a tmpfs at run time and would hide ~/.gitconfig
RUN git config --system user.name "daedalus" && \
    git config --system user.email "daedalus@localhost.local"

# Login shells source /etc/profile.d, which survives the tmpfs HOME
COPY daedalus.sh /etc/profile.d/daedalus.sh

WORKDIR /home/podman
USER podman

VOLUME ["/tmp"]
ENTRYPOINT ["/bin/bash", "-l"]

LABEL \
    app.name="alpine-dev" \
    app.version="1.0" \
    app.description="Secure Alpine dev container" \
    app.managed="true"
"""

# Installed as /etc/profile.d/daedalus.sh and sourced by the login shell
PROFILE_SCRIPT = r"""# daedalus container shell setup

# Start an ssh-agent, or reuse the one already running on the shared socket.
export SSH_AUTH_SOCK="${XDG_RUNTIME_DIR:-$HOME/.ssh}/agent.sock"
ssh-add -l >/dev/null 2>&1
if [ $? -eq 2 ]; then
    mkdir -p -m 700 "$(dirname "$SSH_AUTH_SOCK")"
    rm -f "$SSH_AUTH_SOCK"
    eval "$(ssh-agent -s -a "$SSH_AUTH_SOCK")" >/dev/null
fi

# Prompt for DAEDALUS_SECRET without echoing it and export it for this shell.
# The value only lives in the environment; nothing is written to disk.
set_daedalus_secret() {
    local secret
    read -r -s -p "DAEDALUS_SECRET: " secret
    echo
    export DAEDALUS_SECRET="$secret"
}
"""


def print_build_log(line):
    """Print one line of podman's JSON build output."""
    if isinstance(line, bytes):
        line = line.decode(errors="replace")
    try:
        entry = json.loads(line)
    except ValueError:
        print(line.rstrip())
        return
    text = entry.get("stream") or entry.get("error")
    if text:
        print(text.rstrip())


def reports_errors(doing):
    """Print "Error <doing>: <exception>" to stderr and return False if the action raises."""
    def decorate(action):
        @functools.wraps(action)
        def wrapper(*args, **kwargs):
            try:
                return action(*args, **kwargs)
            except Exception as e:
                print(f"Error {doing}: {e}", file=sys.stderr)
                return False
        return wrapper
    return decorate


# One format for the list header and its rows; values are truncated to the column width
LIST_COLUMNS = (("CONTAINER ID", 12), ("NAME", 20), ("PWD", 30), ("HOSTNAME", 20), ("STATUS", 10))


def list_row(*values):
    return " ".join(f"{value[:width]:<{width}}" for value, (_, width) in zip(values, LIST_COLUMNS))


def container_name(container):
    return container.names[0] if container.names else "N/A"


class LabelBasedContainerManager:
    def __init__(self, app_name="alpine-dev"):
        """Initialize with app identifier."""
        self.app_name = app_name
        self.client = podman.PodmanClient.from_env()
        self.base_labels = {
            "app.name": app_name,
            "app.managed": "true"
        }

    @reports_errors("building image")
    def build(self, tag="latest"):
        """Build the container image from the embedded CONTAINERFILE and PROFILE_SCRIPT."""
        full_image = f"{self.app_name}:{tag}"
        print(f"Building image: {full_image}")

        try:
            with tempfile.TemporaryDirectory() as context:
                Path(context, "Containerfile").write_text(CONTAINERFILE)
                Path(context, "daedalus.sh").write_text(PROFILE_SCRIPT)
                image, build_logs = self.client.images.build(
                    path=context,
                    dockerfile="Containerfile",
                    tag=full_image,
                    labels=self.base_labels,
                )
        except BuildError as e:
            for line in e.build_log:
                print_build_log(line)
            raise

        for line in build_logs:
            print_build_log(line)

        print(f"✓ Image built successfully: {full_image} ({image.short_id})")
        return True

    def run(self, pwd=None, hostname=None, name=None, remove=False):
        """
        Start a container with labels for stateless management and attach an
        interactive shell to it with `podman exec -it`.

        Args:
            pwd: Working directory to mount/identify
            hostname: Identifier for this container (e.g., user@machine)
            name: Container name
            remove: Remove container after the shell exits
        """
        if pwd is None:
            pwd = os.getcwd()
        pwd = os.path.abspath(pwd)

        if hostname is None:
            hostname = f"{os.getenv('USER', 'user')}@{os.getenv('HOSTNAME', 'container')}"

        print(f"Running container from {self.app_name}")
        print(f"Working directory: {pwd}")
        print(f"Hostname: {hostname}")

        # Build labels for discovery
        labels = {
            **self.base_labels,
            "app.pwd": pwd,
            "app.hostname": hostname,
            "app.created": datetime.now().isoformat(),
        }

        podman_cli = shutil.which("podman")
        if podman_cli is None:
            print("Error: the podman CLI is required for an interactive shell", file=sys.stderr)
            return False

        try:
            # Keep the container alive in the background; the shell attaches via exec
            container = self.client.containers.run(
                image=f"{self.app_name}:latest",
                name=name,
                entrypoint=["sleep", "infinity"],
                init=True,  # Forward the stop signal so the container exits promptly
                detach=True,
                read_only=True,
                cap_drop=["all"],
                security_opt=["no-new-privileges=true"],
                labels=labels,
                # Run as the host user so files written to pwd are owned by them
                userns_mode="keep-id",
                user=f"{os.getuid()}:{os.getgid()}",
                environment={"HOME": CONTAINER_HOME},
                mounts=[{
                    "type": "tmpfs",
                    "source": "tmpfs",
                    "target": CONTAINER_HOME,
                    "chown": True,
                }],
                working_dir=pwd,
                volumes={pwd: {"bind": pwd, "mode": "rw"}},  # Mount pwd as writable
            )
        except Exception as e:
            print(f"Error running container: {e}", file=sys.stderr)
            return False

        # Talk to the same podman service as the API client
        command = [podman_cli]
        host = os.getenv("CONTAINER_HOST") or os.getenv("DOCKER_HOST")
        if host:
            command += ["--url", host]
        command += ["exec", "-it", "--workdir", pwd]
        if os.getenv("TERM"):
            command += ["--env", f"TERM={os.environ['TERM']}"]
        command += [container.id, "/bin/bash", "-l"]

        try:
            subprocess.run(command, check=False)
            return True

        except Exception as e:
            print(f"Error in container shell: {e}", file=sys.stderr)
            return False

        finally:
            container.stop(timeout=1)
            if remove:
                container.remove(force=True)
            print("✓ Container exited")

    def find(self, *labels):
        """All of this app's containers carrying every given "key=value" label."""
        return self.client.containers.list(
            all=True, filters={"label": [*labels, f"app.name={self.app_name}"]}
        )

    @reports_errors("listing containers")
    def list_containers(self, filter_by_hostname=None):
        """List all containers managed by this app (via labels)."""
        containers = self.find("app.managed=true")

        if not containers:
            print("No managed containers found")
            return True

        print("\n" + list_row(*(title for title, _ in LIST_COLUMNS)))
        print("-" * 95)

        for container in containers:
            labels = container.labels or {}
            if filter_by_hostname and labels.get("app.hostname") != filter_by_hostname:
                continue
            print(list_row(
                container.id,
                container_name(container),
                labels.get("app.pwd", "N/A"),
                labels.get("app.hostname", "N/A"),
                container.status,
            ))

        return True

    @reports_errors("inspecting container")
    def inspect(self, container_id):
        """Inspect a managed container and display label metadata."""
        container = self.client.containers.get(container_id)
        labels = container.labels or {}

        # Check if managed
        if labels.get("app.name") != self.app_name or labels.get("app.managed") != "true":
            print(f"Container {container_id} is not managed by {self.app_name}")
            return False

        print(f"\nContainer: {container.id[:12]} ({container_name(container)})")
        print(f"Status:    {container.status}")
        print(f"Image:     {container.image.short_id if container.image else 'N/A'}")
        print(f"\nManagement Labels:")
        print(f"  App:      {labels.get('app.name', 'N/A')}")
        print(f"  PWD:      {labels.get('app.pwd', 'N/A')}")
        print(f"  Hostname: {labels.get('app.hostname', 'N/A')}")
        print(f"  Created:  {labels.get('app.created', 'N/A')}")
        print(f"  Version:  {labels.get('app.version', 'N/A')}")

        return True

    def for_each_by_label(self, label_key, label_value, verb, past, action):
        """Apply action to every container matching a label, reporting progress."""
        containers = self.find(f"{label_key}={label_value}")

        if not containers:
            print(f"No containers found with label {label_key}={label_value}")
            return True

        for container in containers:
            print(f"{verb} {container.id[:12]}...")
            action(container)

        print(f"✓ {past} {len(containers)} container(s)")
        return True

    @reports_errors("stopping containers")
    def stop_by_label(self, label_key, label_value):
        """Stop all containers matching a label."""
        return self.for_each_by_label(
            label_key, label_value, "Stopping", "Stopped", lambda c: c.stop()
        )

    @reports_errors("removing containers")
    def remove_by_label(self, label_key, label_value, force=False):
        """Remove all containers matching a label."""
        return self.for_each_by_label(
            label_key, label_value, "Removing", "Removed", lambda c: c.remove(force=force)
        )

    @reports_errors("removing image")
    def clean_image(self, tag="latest"):
        """Remove image."""
        full_image = f"{self.app_name}:{tag}"
        print(f"Removing image: {full_image}")

        images = self.client.images.list(filters={"reference": full_image})
        if not images:
            print(f"Image not found: {full_image}")
            return True

        for image in images:
            image.remove(force=True)

        print(f"✓ Image removed")
        return True

    @reports_errors("retrieving logs")
    def logs(self, container_id):
        """Show container logs."""
        container = self.client.containers.get(container_id)
        print(container.logs(stdout=True, stderr=True).decode())
        return True


# Options each action needs, and the message printed when one is missing
REQUIRED_OPTIONS = {
    "inspect": (("container_id",), "--container-id required"),
    "logs": (("container_id",), "--container-id required"),
    "stop": (("label_key", "label_value"), "--label-key and --label-value required"),
    "remove": (("label_key", "label_value"), "--label-key and --label-value required"),
}


def main():
    parser = argparse.ArgumentParser(
        description="Stateless container manager using label-based discovery"
    )
    parser.add_argument(
        "action",
        choices=["build", "run", "list", "inspect", "stop", "remove", "clean", "logs"],
        help="Action to perform",
    )
    parser.add_argument("--app", default="alpine-dev", help="Application name (default: alpine-dev)")
    parser.add_argument("--tag", default="latest", help="Image tag (default: latest)")
    parser.add_argument("--pwd", help="Working directory to mount/identify (default: current directory)")
    parser.add_argument("--hostname", help="Hostname identifier (default: user@machine)")
    parser.add_argument("--name", help="Container name")
    parser.add_argument("--container-id", help="Container ID for inspect/logs actions")
    parser.add_argument("--label-key", help="Label key for stop/remove (e.g., app.hostname)")
    parser.add_argument("--label-value", help="Label value for stop/remove")
    parser.add_argument("--filter-hostname", help="Filter list output by hostname")
    parser.add_argument("--rm", action="store_true", help="Remove the container when the run shell exits")
    parser.add_argument("--force", action="store_true", help="Force remove containers")
    args = parser.parse_args()

    manager = LabelBasedContainerManager(app_name=args.app)

    actions = {
        "build": lambda: manager.build(args.tag),
        "run": lambda: manager.run(pwd=args.pwd, hostname=args.hostname, name=args.name, remove=args.rm),
        "list": lambda: manager.list_containers(filter_by_hostname=args.filter_hostname),
        "inspect": lambda: manager.inspect(args.container_id),
        "stop": lambda: manager.stop_by_label(args.label_key, args.label_value),
        "remove": lambda: manager.remove_by_label(args.label_key, args.label_value, args.force),
        "clean": lambda: manager.clean_image(args.tag),
        "logs": lambda: manager.logs(args.container_id),
    }

    required, message = REQUIRED_OPTIONS.get(args.action, ((), ""))
    if not all(getattr(args, option) for option in required):
        print(f"Error: {message}", file=sys.stderr)
        sys.exit(1)

    try:
        success = actions[args.action]()
    except KeyboardInterrupt:
        print("\n✓ Interrupted by user")
        success = True
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        success = False

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
