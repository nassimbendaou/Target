"""Approved local workflows and the inventory resolved by Ansible."""

import json
import os
import subprocess
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
        result = subprocess.run(
            [inventory_bin, "-i", str(INVENTORY_SOURCE), "--list"],
            cwd=ROOT,
            env={**os.environ, "ANSIBLE_CONFIG": str(ROOT / "ansible.cfg")},
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
        for template in TEMPLATES.values():
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
        definition = TEMPLATES[template_id]
        return {
            "id": template_id,
            **definition,
            "inventory": {"id": self.inventory["id"], "name": self.inventory["name"]},
            "target_hosts": self.group_hosts(definition["target_group"]),
        }

    def describe(self):
        return {
            "templates": [self.template(template_id) for template_id in TEMPLATES],
            "inventories": [self.inventory],
        }

    def command(self, ansible_bin, template_id):
        template = TEMPLATES[template_id]
        return [
            ansible_bin,
            "-i",
            str(INVENTORY_SOURCE),
            "-c",
            "local",
            "--limit",
            template["target_group"],
            template["playbook"],
        ]
