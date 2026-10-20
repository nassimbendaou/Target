# Target

Target is an early local prototype for running and inspecting Ansible jobs. It runs one bundled, read-only playbook against `localhost`, stores job history in SQLite, and shows real results in a Vite-powered browser dashboard.

## Run it

From the Nix shell, install the locked frontend dependencies once, then run the API and Vite in separate terminals:

```sh
nix develop --command npm ci
nix develop --command python server.py
# In another terminal:
nix develop --command npm run dev
```

Open <http://127.0.0.1:5173>. Choose **Run smoke check** and inspect its status and Ansible output. You can cancel a queued or running job from its details panel. To run the checks:

```sh
nix develop --command python -m unittest discover -s tests -v
nix develop --command ansible-playbook -i localhost, -c local playbooks/smoke.yml
nix develop --command npm run build
```

The Nix flake pins Python, Ansible, Node, and Prettier. `package-lock.json` pins Vite and its dependencies. Frontend source lives in `frontend/`; Vite proxies `/api` to the Python server on port 8000 and restricts file serving to that directory. The API does not serve the frontend. Job history is saved in `.data/target.sqlite3` and is ignored by Git.

## Scope and safety

Both servers listen on `127.0.0.1` only. The API accepts no playbook, inventory, credential, or command arguments from the browser. It checks the host and browser origin, allows one job at a time, limits a run to 90 seconds, and records the result. Cancelling sends SIGKILL to the local job's process group and records it as cancelled. The UI uses a fixed localhost workflow and shows no simulated jobs.

This is a development slice, not a production automation service. There is no login, approval record, customer worker, container isolation, remote inventory, or secrets integration yet. Do not expose the local API through a reverse proxy or run it on a shared machine. Those boundaries are the next feature before any remote execution.
