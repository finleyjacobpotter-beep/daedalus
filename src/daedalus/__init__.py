#!/usr/bin/env python3
"""
Stateless container manager using podman-py with label-based discovery.
Install: pip install podman-py
"""

import argparse
import sys
from pathlib import Path
from datetime import datetime
import os
import podman


class LabelBasedContainerManager:
    def __init__(self, app_name="alpine-dev"):
        """Initialize with app identifier."""
        self.app_name = app_name
        self.client = podman.PodmanClient()
        self.base_labels = {
            "app.name": app_name,
            "app.managed": "true"
        }

    def build(self, dockerfile_path="Containerfile", tag="latest"):
        """Build the container image."""
        full_image = f"{self.app_name}:{tag}"
        print(f"Building image: {full_image}")
        
        if not Path(dockerfile_path).exists():
            print(f"Error: {dockerfile_path} not found", file=sys.stderr)
            return False

        try:
            image, build_logs = self.client.images.build(
                dockerfile=dockerfile_path,
                tag=full_image,
                path="."
            )
            
            for log in build_logs:
                if "stream" in log:
                    print(log["stream"].strip())
            
            print(f"✓ Image built successfully: {full_image}")
            return True
        
        except Exception as e:
            print(f"Error building image: {e}", file=sys.stderr)
            return False

    def run(self, pwd=None, hostname=None, name=None, interactive=True, remove=False):
        """
        Run container with labels for stateless management.
        
        Args:
            pwd: Working directory to mount/identify
            hostname: Identifier for this container (e.g., user@machine)
            name: Container name
            interactive: Run interactively
            remove: Remove container after exit
        """
        if pwd is None:
            pwd = os.getcwd()
        
        if hostname is None:
            hostname = f"{os.getenv('USER', 'user')}@{os.getenv('HOSTNAME', 'container')}"
        
        print(f"Running container from {self.app_name}")
        print(f"Working directory: {pwd}")
        print(f"Hostname: {hostname}")
        
        try:
            # Build labels for discovery
            labels = {
                **self.base_labels,
                "app.pwd": pwd,
                "app.hostname": hostname,
                "app.created": datetime.now().isoformat(),
            }
            
            # Run the container
            container = self.client.containers.run(
                image=f"{self.app_name}:latest",
                name=name,
                tty=interactive,
                stdin_open=interactive,
                detach=False,
                rm=remove,
                read_only=True,
                cap_drop=["all"],
                security_opt=["no-new-privileges=true"],
                labels=labels,
                volumes={pwd: {"bind": pwd, "mode": "rw"}},  # Mount pwd as writable
            )
            
            print(f"✓ Container exited")
            return True
        
        except Exception as e:
            print(f"Error running container: {e}", file=sys.stderr)
            return False

    def list_containers(self, filter_by_hostname=None):
        """List all containers managed by this app (via labels)."""
        try:
            # Filter by labels
            filters = {"label": ["app.managed=true", f"app.name={self.app_name}"]}
            containers = self.client.containers.list(
                all=True,
                filters=filters
            )
            
            if not containers:
                print("No managed containers found")
                return True
            
            print(f"\n{'CONTAINER ID':<12} {'NAME':<20} {'PWD':<30} {'HOSTNAME':<20} {'STATUS':<10}")
            print("-" * 95)
            
            for container in containers:
                container_id = container.id[:12]
                name = (container.names[0] if container.names else "N/A")[:20]
                
                # Extract labels
                labels = container.labels or {}
                pwd = labels.get("app.pwd", "N/A")[:30]
                hostname = labels.get("app.hostname", "N/A")[:20]
                status = container.status[:10]
                
                # Optional filtering by hostname
                if filter_by_hostname and labels.get("app.hostname") != filter_by_hostname:
                    continue
                
                print(f"{container_id:<12} {name:<20} {pwd:<30} {hostname:<20} {status:<10}")
            
            return True
        
        except Exception as e:
            print(f"Error listing containers: {e}", file=sys.stderr)
            return False

    def inspect(self, container_id):
        """Inspect a managed container and display label metadata."""
        try:
            container = self.client.containers.get(container_id)
            labels = container.labels or {}
            
            # Check if managed
            if labels.get("app.name") != self.app_name or labels.get("app.managed") != "true":
                print(f"Container {container_id} is not managed by {self.app_name}")
                return False
            
            print(f"\nContainer: {container.id[:12]} ({container.names[0] if container.names else 'N/A'})")
            print(f"Status:    {container.status}")
            print(f"Image:     {container.image.short_id if container.image else 'N/A'}")
            print(f"\nManagement Labels:")
            print(f"  App:      {labels.get('app.name', 'N/A')}")
            print(f"  PWD:      {labels.get('app.pwd', 'N/A')}")
            print(f"  Hostname: {labels.get('app.hostname', 'N/A')}")
            print(f"  Created:  {labels.get('app.created', 'N/A')}")
            print(f"  Version:  {labels.get('app.version', 'N/A')}")
            
            return True
        
        except Exception as e:
            print(f"Error inspecting container: {e}", file=sys.stderr)
            return False

    def stop_by_label(self, label_key, label_value):
        """Stop all containers matching a label."""
        try:
            filters = {"label": [f"{label_key}={label_value}", f"app.name={self.app_name}"]}
            containers = self.client.containers.list(all=True, filters=filters)
            
            if not containers:
                print(f"No containers found with label {label_key}={label_value}")
                return True
            
            for container in containers:
                print(f"Stopping {container.id[:12]}...")
                container.stop()
            
            print(f"✓ Stopped {len(containers)} container(s)")
            return True
        
        except Exception as e:
            print(f"Error stopping containers: {e}", file=sys.stderr)
            return False

    def remove_by_label(self, label_key, label_value, force=False):
        """Remove all containers matching a label."""
        try:
            filters = {"label": [f"{label_key}={label_value}", f"app.name={self.app_name}"]}
            containers = self.client.containers.list(all=True, filters=filters)
            
            if not containers:
                print(f"No containers found with label {label_key}={label_value}")
                return True
            
            for container in containers:
                print(f"Removing {container.id[:12]}...")
                container.remove(force=force)
            
            print(f"✓ Removed {len(containers)} container(s)")
            return True
        
        except Exception as e:
            print(f"Error removing containers: {e}", file=sys.stderr)
            return False

    def clean_image(self, tag="latest"):
        """Remove image."""
        full_image = f"{self.app_name}:{tag}"
        print(f"Removing image: {full_image}")
        
        try:
            images = self.client.images.list(filters={"reference": full_image})
            if not images:
                print(f"Image not found: {full_image}")
                return True
            
            for image in images:
                image.remove(force=True)
            
            print(f"✓ Image removed")
            return True
        
        except Exception as e:
            print(f"Error removing image: {e}", file=sys.stderr)
            return False

    def logs(self, container_id):
        """Show container logs."""
        try:
            container = self.client.containers.get(container_id)
            logs = container.logs(stdout=True, stderr=True)
            print(logs.decode())
            return True
        
        except Exception as e:
            print(f"Error retrieving logs: {e}", file=sys.stderr)
            return False


def main():
    parser = argparse.ArgumentParser(
        description="Stateless container manager using label-based discovery"
    )
    
    parser.add_argument(
        "action",
        choices=["build", "run", "list", "inspect", "stop", "remove", "clean", "logs"],
        help="Action to perform"
    )
    
    parser.add_argument(
        "--app",
        default="alpine-dev",
        help="Application name (default: alpine-dev)"
    )
    
    parser.add_argument(
        "--tag",
        default="latest",
        help="Image tag (default: latest)"
    )
    
    parser.add_argument(
        "--dockerfile",
        default="Containerfile",
        help="Dockerfile path (default: Containerfile)"
    )
    
    parser.add_argument(
        "--pwd",
        help="Working directory to mount/identify (default: current directory)"
    )
    
    parser.add_argument(
        "--hostname",
        help="Hostname identifier (default: user@machine)"
    )
    
    parser.add_argument(
        "--name",
        help="Container name"
    )
    
    parser.add_argument(
        "--container-id",
        help="Container ID for inspect/logs actions"
    )
    
    parser.add_argument(
        "--label-key",
        help="Label key for stop/remove (e.g., app.hostname)"
    )
    
    parser.add_argument(
        "--label-value",
        help="Label value for stop/remove"
    )
    
    parser.add_argument(
        "--filter-hostname",
        help="Filter list output by hostname"
    )
    
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force remove containers"
    )
    
    args = parser.parse_args()
    
    manager = LabelBasedContainerManager(app_name=args.app)
    
    success = True
    
    try:
        if args.action == "build":
            success = manager.build(args.dockerfile, args.tag)
        
        elif args.action == "run":
            success = manager.run(
                pwd=args.pwd,
                hostname=args.hostname,
                name=args.name
            )
        
        elif args.action == "list":
            success = manager.list_containers(filter_by_hostname=args.filter_hostname)
        
        elif args.action == "inspect":
            if not args.container_id:
                print("Error: --container-id required", file=sys.stderr)
                success = False
            else:
                success = manager.inspect(args.container_id)
        
        elif args.action == "stop":
            if not args.label_key or not args.label_value:
                print("Error: --label-key and --label-value required", file=sys.stderr)
                success = False
            else:
                success = manager.stop_by_label(args.label_key, args.label_value)
        
        elif args.action == "remove":
            if not args.label_key or not args.label_value:
                print("Error: --label-key and --label-value required", file=sys.stderr)
                success = False
            else:
                success = manager.remove_by_label(args.label_key, args.label_value, args.force)
        
        elif args.action == "clean":
            success = manager.clean_image(args.tag)
        
        elif args.action == "logs":
            if not args.container_id:
                print("Error: --container-id required", file=sys.stderr)
                success = False
            else:
                success = manager.logs(args.container_id)
    
    except KeyboardInterrupt:
        print("\n✓ Interrupted by user")
        success = True
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        success = False
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

