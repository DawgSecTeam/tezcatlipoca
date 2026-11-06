"""The GCP provider: Compute Engine ranges (one VPC per run, labels for ownership).

The operation surface mirrors providers/proxmox/pipeline.py; each op is filled by
the GCP backend as it lands (WS3): networks, engine template image, goldens as
custom images, team instances, machine-image restore points, preflight, teardown,
red01. Until an op is implemented it refuses by name so no half-capability
pretends to work.
"""

PLATFORM = "gcp"

_TODO = "not implemented on the gcp provider yet — this pipeline stage lands with the GCP backend"


def _refuse(op):
    raise SystemExit(f"  ERROR: {op} {_TODO}.")


def reclaim_interrupted(ctx):
    _refuse("reclaim_interrupted")


def engine_template(ctx):
    _refuse("engine_template")


def create_estate(ctx):
    _refuse("create_estate")


def build_golden_sets(ctx, stored, coverage_recorder):
    _refuse("build_golden_sets")


def apply_team_boxes(ctx, sources):
    _refuse("apply_team_boxes")


def windows_bootstrap(ctx, target, features=()):
    _refuse("windows_bootstrap")


def restore_point_create(ctx, target, name, description=""):
    _refuse("restore_point_create")


def restore_points_list(ctx, target):
    _refuse("restore_points_list")


def restore_point_rollback(ctx, target, name):
    _refuse("restore_point_rollback")


def restore_point_hint(ctx, target):
    _refuse("restore_point_hint")


def live_unit_numbers():
    _refuse("live_unit_numbers")


def destroy_estate(document, *, full=True, nodes=None, log=print):
    _refuse("destroy_estate")


def sweep_tagged_leftovers(nodes, ownership, keep_templates=False):
    _refuse("sweep_tagged_leftovers")


def pre_stop_boxes(teams, boxes, default_node, team_nodes=None, ownership=None):
    _refuse("pre_stop_boxes")


def red01_deploy(comp_dir, **kwargs):
    _refuse("red01_deploy")


def red01_destroy(comp_dir, **kwargs):
    _refuse("red01_destroy")


def red01_occupant_verdict(vmid, comp, run_id=None):
    _refuse("red01_occupant_verdict")
