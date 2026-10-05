# Upgrading to 0.2.0 (breaking)

0.2.0 removes every pre-v3 path: there is one pipeline (eight phases, golden templates + linked
clones, `PIPELINE_VERSION = 3`). Things that stop working or change:

- **Pre-0.2.0 ranges cannot be torn down by the scripts.** Teardown requires the run id and the
  full ownership tag set (`tezcatlipoca` + `comp-<dir>` + `run-<id>`) that deploys stamp since
  0.2.0, and a deployed comp whose per-comp `terraform/terraform.tfstate` is missing is refused.
  Remove an old range by hand on the node.
- **Engine and golden template hashes change** (`terraform/main.tf` changed), so the first deploy
  rebuilds the engine template and goldens; old ones are not reused.
- **VMs are tagged with the competition *directory* name** (`TF_VAR_competition`), not the
  Compfile `name`, so ownership guards match when the two differ.
- **The concurrency gate warns instead of refusing** when another deploy holds a lock; give each
  concurrent deploy its own `--scoring-vmid`, `TF_VAR_team_identifiers` and
  `TF_VAR_engine_mgmt_ip` (see [environment-facts.md](environment-facts.md)).
- **Removed scripts/flags:** the legacy deploy/destroy/redeploy/verify/scrim entry points keep
  their names but are thin CLIs over `deploy_lib/`, `verifier/`, `scrim/`, `scrim_report/`,
  `artifacts_lib/`, `preflight/`; code importing the old monolith internals must import the owning
  module (`pipeline_api` is the supported surface for redeploy).
- **nakon needs the 0-step fix** (vendor/nakon `22360ba`): the driver-side workaround is gone, so a
  managed box with no pins relies on it. The preflight warns about zero-pin boxes.
- **Apply `docs/vulndb-fixes/domain-join-linux.sh`** to the catalog before deploying Fedora
  domain members (see [vulndb-fixes/README.md](vulndb-fixes/README.md)).
