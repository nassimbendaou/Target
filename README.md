# Target

Target is an early local prototype for running and inspecting Ansible jobs. This first slice runs one bundled, read-only playbook against `localhost`, stores job history in SQLite, and shows real results in a browser.

## Run it

```sh
nix develop --command python server.py
```

Open <http://127.0.0.1:8000>. Choose **Run smoke check** and inspect its status and Ansible output. To run the checks:

```sh
nix develop --command python -m unittest discover -s tests -v
nix develop --command ansible-playbook -i localhost, -c local playbooks/smoke.yml
```

The Nix flake pins Python, Ansible, Node, and Prettier. No Node packages or frontend build step are needed. Job history is saved in `.data/target.sqlite3` and is ignored by Git.

## Scope and safety

The API listens on `127.0.0.1` only. It accepts no playbook, inventory, credential, or command arguments from the browser. It checks the host and browser origin, allows one job at a time, limits a run to 90 seconds, and records the result. The UI uses a fixed localhost workflow and shows no simulated jobs.

This is a development slice, not a production automation service. There is no login, approval record, customer worker, container isolation, remote inventory, or secrets integration yet. Do not expose the local API through a reverse proxy or run it on a shared machine. Those boundaries are the next feature before any remote execution.
