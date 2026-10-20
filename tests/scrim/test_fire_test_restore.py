"""A `--skip-deploy` recovery reuses a range red already damaged. The fire test only did
`systemctl unmask; start`, which cannot undo `stop_rename_config` (nginx's binary sat at
`nginx.tlaloc-off`), so the recovery looped for 180 s and refused. Red's own restore table
is in the run's evidence once pulled — apply its recipe for the unit first."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from scrim import fire_test


def _args_with_world(tmp_path, entries):
    run_dir = tmp_path / "run"
    world = run_dir / "evidence" / "red" / "world.json"
    world.parent.mkdir(parents=True)
    world.write_text(json.dumps({"restores": entries}))
    return SimpleNamespace(run_dir=str(run_dir))


def test_reds_restore_recipe_is_applied_for_the_unit(tmp_path):
    args = _args_with_world(tmp_path, [
        {"ip": "192.168.105.4", "service": "nginx",
         "recipe": ["mv -f /usr/sbin/nginx.tlaloc-off /usr/sbin/nginx",
                    "systemctl unmask nginx; systemctl start nginx"]}])
    ran = []
    assert fire_test._red_restore(args, "192.168.105.4", "nginx", ran.append) is True
    assert ran == ["mv -f /usr/sbin/nginx.tlaloc-off /usr/sbin/nginx",
                   "systemctl unmask nginx; systemctl start nginx"]


def test_no_matching_recipe_leaves_the_plain_start_to_run(tmp_path):
    args = _args_with_world(tmp_path, [
        {"ip": "192.168.105.4", "service": "postfix", "recipe": ["true"]}])
    assert fire_test._red_restore(args, "192.168.105.4", "nginx", lambda _c: None) is False
