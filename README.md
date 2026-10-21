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

## Backend layout

The backend remains one process, with no new dependencies or network interfaces:

```text
server.py               Root launcher
backend/
├── server.py           Entry point and server lifetime
├── api.py              HTTP routes, validation, and response headers
├── jobs.py             Job lifecycle, snapshots, execution, cancellation
├── storage.py          SQLite schema, recovery, connections, and history
├── environment.py      Allowlisted child-process environment
└── catalog.py          Approved templates and resolved inventory
```

`TargetApp` coordinates execution using `JobStore`. Job-state changes and lifecycle events still share one transaction; process start, cancellation, and completion still share one lock. The HTTP interface, database location, history format, and `python server.py` command are unchanged. You can also start it with `python -m backend.server`. Ruff checks the entire repository so new Python modules are covered automatically.

## Pull request checks

GitHub Actions runs the same locked Nix shell on pushes to `master` and `feat/**`, and on pull requests targeting `master`. Checks cover Python linting and formatting, workflow validation, frontend formatting, API tests, both real localhost playbooks, and the Vite build. The integration tests use temporary databases and leave your job history unchanged.

To check a workflow change locally:

```sh
nix develop --command actionlint
```

The workflow uses actions pinned to commit SHAs and a read-only repository token. Dependency installation uses `npm ci --ignore-scripts`; the frontend build uses the packages pinned in `package-lock.json`.

## Run timeline

The run inspector's **Run timeline** shows persisted lifecycle events in recorded order: queued, execution started, cancellation requested, and the final outcome. Snapshot preparation and process startup failures are recorded even when execution never starts. On API startup, unfinished runs are marked failed with an interruption-detected event; that timestamp is when Target detected the interruption, not when the process stopped. Events are committed in the same SQLite transaction as their job-state changes. API list and detail responses include the same ordered `events` array.

Completed older runs retain an empty timeline rather than reconstructed events. Rejected launch/cancel requests do not create lifecycle events. This is a local operational history, not a security audit trail: events have no authenticated actor, signing, or protection against database edits. No event write/delete API is exposed.

## Scope and safety

### Templates and inventory

The approved catalog is defined in `backend/catalog.py`. It contains a **Diagnostics** smoke check targeting `control`, and a **Connectivity** check targeting its parent group `development`. `inventories/local.yml` is the real inventory used by both playbooks:

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

### Execution input snapshots

At startup, Target captures the bundled playbooks, inventory, and Ansible configuration. Ansible resolves the captured inventory, so the hierarchy shown in the dashboard describes the same inventory used for execution. Repository edits take effect only after restarting the API.

Each launch stores its captured files privately in SQLite alongside the template definition. The worker verifies their SHA-256 hashes and the input revision before executing copies in a temporary working directory. A missing or inconsistent snapshot fails the run without falling back to live repository files. Temporary files are removed after execution, including cancellation and timeout. The run inspector's **Execution inputs** section shows the revision and individual file hashes; source contents are not returned as API metadata. Older runs show that snapshots were not captured.

This covers the bundled self-contained playbooks, not external roles, includes, variable files, secrets, or the host's state. Hashes identify inputs and detect accidental inconsistency; they are not signed audit records or execution isolation. Keep the SQLite database private.

### Runner environment

Inventory discovery and job execution receive an allowlisted environment: the local toolchain's `PATH` and locale settings, plus Target-controlled Ansible settings. Ambient cloud tokens, SSH agent sockets, Python import overrides, and Ansible configuration/plugin overrides are not forwarded. Each process gets a private temporary home and temporary-file directory, removed with its execution workspace. User home-directory Ansible configuration and plugins are not automatically loaded. This follows [Ansible's configuration precedence](https://docs.ansible.com/projects/ansible/latest/installation_guide/intro_configuration.html#environmental-configuration), where environment variables can override configuration files.

This is not a sandbox: approved playbooks still run as the API's operating-system user and can access that user's files and network. `PATH` and installed tools remain trusted; use only the pinned Nix shell and trusted bundled playbooks. No remote execution or credential injection is enabled.

Both servers listen on `127.0.0.1` only. The API checks the host and browser origin, allows one job at a time across all templates, limits a run to 90 seconds, and records the result. Cancelling sends SIGKILL to the local job's process group and records it as cancelled. The UI shows no simulated hosts or jobs.

This is a development slice, not a production automation service. There is no login, approval record, customer worker, container isolation, remote inventory, or secrets integration yet. Do not expose the local API through a reverse proxy or run it on a shared machine. Those boundaries are the next feature before any remote execution.
