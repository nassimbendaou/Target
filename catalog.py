"""Approved local workflows and the inventory resolved by Ansible."""

import json
import os
import subprocess
import tempfile
from copy import deepcopy
from hashlib import sha256
from pathlib import Path


ROOT = Path(__file__).resolve().parent
INVENTORY_SOURCE = ROOT / "inventories" / "local.yml"
TEMPLATES = {
    "local-smoke": {
        "name": "Local smoke check",
        "description": "Gather facts and report this machine's operating system.",
        "group": "Diagnostics",
        "playbook": "playbooks/smoke.yml",
        "target_group": "control",
    },
    "local-connectivity": {
        "name": "Local connectivity check",
        "description": "Verify Ansible can execute a Python module on the local host.",
        "group": "Connectivity",
        "playbook": "playbooks/connectivity.yml",
        "target_group": "development",
    },
}
LEGACY_TEMPLATE = {
    "id": "local-smoke",
    "name": "Local smoke check",
    "description": "Gather facts and report this machine's operating system.",
    "group": "Diagnostics",
    "playbook": "playbooks/smoke.yml",
    "inventory": {"id": "localhost-inline", "name": "Localhost (inline)"},
    "target_group": "all",
    "target_hosts": ["localhost"],
}


class JobCatalog:
    def __init__(self, inventory_bin):
        if not inventory_bin:
            raise ValueError(
                "ansible-inventory is required; start Target with nix develop"
            )
        self._templates = deepcopy(TEMPLATES)
        inventory = INVENTORY_SOURCE.read_bytes().decode("utf-8")
        config = (ROOT / "ansible.cfg").read_bytes().decode("utf-8")
        # shortcut: snapshot self-contained playbooks only; bundle roles, vars, and includes before accepting external projects.
        self._files = {
            template_id: {
                "inventory.yml": inventory,
                "ansible.cfg": config,
                "playbook.yml": (ROOT / template["playbook"])
                .read_bytes()
                .decode("utf-8"),
            }
            for template_id, template in self._templates.items()
        }
        with tempfile.TemporaryDirectory(prefix="target-catalog-") as directory:
            directory = Path(directory)
            (directory / "inventory.yml").write_bytes(inventory.encode("utf-8"))
            (directory / "ansible.cfg").write_bytes(config.encode("utf-8"))
            result = subprocess.run(
                [inventory_bin, "-i", str(directory / "inventory.yml"), "--list"],
                cwd=directory,
                env={**os.environ, "ANSIBLE_CONFIG": str(directory / "ansible.cfg")},
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
        data = json.loads(result.stdout)
        hostvars = data.get("_meta", {}).get("hostvars", {})
        if (
            set(hostvars) != {"localhost"}
            or hostvars["localhost"].get("ansible_connection") != "local"
        ):
            raise ValueError(
                "The local inventory must contain only localhost with a local connection"
            )
        self.inventory = {
            "id": "local",
            "name": "Local development",
            "description": "Repository-managed inventory. One real host on this machine.",
            "groups": {
                name: {
                    "children": group.get("children", []),
                    "hosts": group.get("hosts", []),
                }
                for name, group in data.items()
                if name != "_meta"
            },
            "hosts": [{"name": "localhost", "connection": "local"}],
        }
        for group in list(self.inventory["groups"].values()):
            for child in group["children"]:
                self.inventory["groups"].setdefault(
                    child, {"children": [], "hosts": []}
                )
        if self.group_hosts("all") != ["localhost"]:
            raise ValueError("Only localhost is supported in this inventory")
        for name, group in self.inventory["groups"].items():
            group["host_count"] = len(self.group_hosts(name))
        self.inventory["hosts"][0].update(
            direct_groups=[
                name
                for name, group in self.inventory["groups"].items()
                if "localhost" in group["hosts"]
            ],
            groups=[
                name
                for name in self.inventory["groups"]
                if "localhost" in self.group_hosts(name)
            ],
        )
        for template in self._templates.values():
            if self.group_hosts(template["target_group"]) != ["localhost"]:
                raise ValueError("Every approved template must target the local host")

    def group_hosts(self, name):
        pending, visited, hosts = [name], set(), set()
        while pending:
            name = pending.pop()
            if name in visited:
                continue
            visited.add(name)
            group = self.inventory["groups"][name]
            hosts.update(group["hosts"])
            pending.extend(group["children"])
        return sorted(hosts)

    def template(self, template_id):
        definition = self._templates[template_id]
        template = {
            "id": template_id,
            **definition,
            "inventory": {"id": self.inventory["id"], "name": self.inventory["name"]},
            "target_hosts": self.group_hosts(definition["target_group"]),
            "source_hashes": {
                name: sha256(content.encode("utf-8")).hexdigest()
                for name, content in self._files[template_id].items()
            },
        }
        template["revision"] = sha256(
            json.dumps(template, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return template

    def describe(self):
        return {
            "templates": [
                self.template(template_id) for template_id in self._templates
            ],
            "inventories": [self.inventory],
        }

    def execution_files(self, template_id):
        return self._files[template_id].copy()
