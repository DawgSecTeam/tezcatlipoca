# tezcatlipoca web UI

A browser front end for authoring, editing and deploying competitions. It edits the one
canonical home of a competition, `competitions/<id>/`, and runs the same CLI drivers an
operator would type. It does not reimplement any deploy logic.

## Run it

```bash
pip install -r webui/requirements.txt
(cd webui/frontend && npm install && npm run build)
python3 webui/server.py                # http://127.0.0.1:8765  (TEZ_WEBUI_HOST / TEZ_WEBUI_PORT)
```

For frontend work, run `npm run dev` in `webui/frontend/` (Vite on :5173, which proxies `/api` to
:8765) next to `python3 webui/server.py`.

Run the server from the repo root, or from a **practice worktree** (see
[AGENTS.md](../AGENTS.md#practice-runs-must-run-from-a-new-worktree)). Every path resolves from the
checkout the server runs in, and so do deploys.

## What edits what

| Screen | File(s) in `competitions/<id>/` |
|---|---|
| Comps grid, **New competition** | creates the dir: `Compfile`, `boxes.json`, `box_services.json`, `box_vulns.json`, `injects/`, `packet.md` |
| Overview → **Edit info** | `Compfile` (name, scenario, difficulty, domain prefix/suffix; other keys are preserved) |
| Boxes, box page, **Settings** | `boxes.json` (a rename carries the box's pins in both pin files) |
| Box → OS pill | `boxes.json` `template` (Proxmox templates when the API is reachable, plus every template any comp uses) |
| Box → **Services** | `box_services.json[box]`: catalog pin, nakon vars, scoring overrides (`display`, `port`, `check`, `score_only`, `plant_only`) |
| Box → **Misconfigs** | `box_vulns.json[box]`: catalog pin + nakon vars |
| Injects | `injects/<slug>/inject.json` + `briefing.md` |
| Packet | `packet.md` |

Pins are written back in the files' own idiom: a bare `"name"` when an entry has nothing but a
name, otherwise `{"name": …, "vars": {…}}`. Entries you don't touch are written back exactly as
they were read.

`packets/<id>/packet.yaml` is not edited here. It is an optional upstream input that
`compile-packet.py` turns into a comp dir
([docs/packet-profiles.md](../docs/packet-profiles.md)). Once the comp dir exists, the comp dir is
the source of truth.

## Keyboard

| Key | Where | Does |
|---|---|---|
| `/` | comps grid, box page, services/misconfigs | focus the search box |
| `⌘S` / `Ctrl S` | any editor with unsaved changes | save |
| `↑` `↓` `Enter` | catalog picker | browse, then add the highlighted config |
| `⌘B` `⌘I` `⌘K` | Markdown editor | bold, italic, link |
| `Esc` | dialogs, search boxes | close, or clear the search |

The theme follows the OS on first visit; the sun/moon button switches it and the browser remembers.

## Catalog (the "Add +" pickers)

The pickers come from nakon's catalog (`vendor/nakon`, so run `git submodule update --init`),
through nakon's own `auto` source: vulndb-ui over HTTP if `VULNDB_UI_URL` is set, otherwise MySQL
using the credentials in `vendor/nakon/.env`. Results are filtered by the box's platform
(`windows` if the template name contains `win`, else `linux`) and by tab (services = category
`service`, misconfigs = everything else). Required vars come from the script plus
`constants.REQUIRED_VARS`, and `constants.KNOWN_BROKEN_CONFIGS` rows are flagged.

For offline development, point `TEZ_WEBUI_CATALOG_FILE` at a JSON array of catalog rows
(`name`, `platform`, `category`, `script`, optional `description`).

## Deploy panel

| Button | Runs |
|---|---|
| Plan only | `create-competition.py --competition <id> --teams N --yes --plan-only` |
| Deploy | the same command without `--plan-only` (asks for confirmation first) |
| Verify | `verify-competition.py competitions/<id>` |

`--scoring-vmid`, `--engine-node` and `--team-node` are passed when set. The node controls only
appear when a `nodes.json` exists ([docs/multi-node.md](../docs/multi-node.md)). Output streams
into the panel and is kept in `logs/webui/` (gitignored). Only one job per competition runs at a
time. The job list is kept in memory, so restarting the server forgets it (the log files stay).

Teardown is deliberately not a button yet: use `destroy-competition.py`.

## Not yet

- Nextcloud / office-document editing for injects and the packet (Markdown for now).
- Destroy / redeploy buttons.
- Auth. The server binds to 127.0.0.1 by default and has no login, so don't expose it.
