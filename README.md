# Target

Target is an early local prototype for running and inspecting Ansible jobs. It runs approved, read-only workflows against `localhost`, stores job history in SQLite, and shows real results in a Vite-powered browser dashboard.

## Run it

From the Nix shell, install the locked frontend dependencies once, then run the API and Vite in separate terminals:

```sh
nix develop --command npm ci
nix develop --command python server.py
# In another terminal:
nix develop --command npm run dev
```

Open <http://127.0.0.1:5173>. Choose a job template and select **Run template**, or launch it from **Templates**. Filter history by workflow group, template, or status. Each run records its template definition, inventory name, target group, and resolved host names; later template renames do not rewrite history. Existing runs retain their original inline localhost inventory label.

The dashboard refreshes active runs every two seconds and retains the latest 100,000 output characters, including output produced before cancellation or timeout. Output follows the latest lines while you are at the bottom; scroll up to read earlier lines. You can cancel a queued or running job from its details panel. To run the checks:

```sh
nix develop --command python -m unittest discover -s tests -v
nix develop --command ansible-playbook -i inventories/local.yml --limit control playbooks/smoke.yml
nix develop --command npm run build
```

The Nix flake pins Python, Ansible, Node, and Prettier. `package-lock.json` pins Vite and its dependencies. Frontend source lives in `frontend/`; Vite proxies `/api` to the Python server on port 8000 and restricts file serving to that directory. The API does not serve the frontend. Job history is saved in `.data/target.sqlite3` and is ignored by Git.

## Scope and safety

### Templates and inventory

The approved catalog is defined in `catalog.py`. It contains a **Diagnostics** smoke check targeting `control`, and a **Connectivity** check targeting its parent group `development`. `inventories/local.yml` is the real inventory used by both playbooks:

```text
Local development
└── all
    ├── ungrouped (empty)
    └── development
        └── control
            └── localhost
```

The **Inventory** view uses `ansible-inventory --list` to resolve this hierarchy. Group counts include inherited hosts without double-counting them. Selecting a host shows its direct groups and all inherited memberships. Host variables are not exposed through the API.

Inventory and template definitions are managed in the repository, not edited in the browser. Restart the API after changing them. This version accepts only localhost with a local connection. The launch API accepts an approved `template_id`, never a playbook path, inventory path, host pattern, or command. An empty launch request remains compatible with the original smoke-check API.

Both servers listen on `127.0.0.1` only. The API checks the host and browser origin, allows one job at a time across all templates, limits a run to 90 seconds, and records the result. Cancelling sends SIGKILL to the local job's process group and records it as cancelled. The UI shows no simulated hosts or jobs.

This is a development slice, not a production automation service. There is no login, approval record, customer worker, container isolation, remote inventory, or secrets integration yet. Do not expose the local API through a reverse proxy or run it on a shared machine. Those boundaries are the next feature before any remote execution.
