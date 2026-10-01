#!/usr/bin/env python3
"""Finish the engine-template build on the ALREADY-UP build VM (vmid 1640 at the
planned mgmt IP) and convert it — the exact post-SSH tail of
template_ops.build_engine_template, driven by hand so a flaky fresh-clone SSH
window can't burn the whole flow again. Saves the hash + build info to
.deploy_state.json exactly as deploy.py would."""

import sys, os
sys.path.insert(0, '/home/hna/dev/dawgsec/tezcatlipoca-amongus')
os.chdir('/home/hna/dev/dawgsec/tezcatlipoca-amongus')
from dotenv import load_dotenv
load_dotenv('.env')

import template_ops, engine_ops, json
from range_ops import stop_vm, proxmox_api
from hardening_ops import wait_boxes_settled, _box_settled_via_ssh
from ssh_ops import wait_for_ssh

node = os.environ['TF_VAR_proxmox_node']
engine_vmid = int(sys.argv[1])          # 1500
build_ip = os.environ['TF_VAR_engine_mgmt_ip']

from pathlib import Path
comp_dir = Path('competitions/amongus-cde-2026')
state_path = comp_dir / '.deploy_state.json'
state = json.loads(state_path.read_text())

postgres_password = state['postgres_password']
redis_password = state['redis_password']
quotient_ref = state.get('quotient_ref')

# rebuild the same engine hash deploy.py computed (config + code classes)
from template_ops import engine_hash_inputs, hash_from_inputs, _clean_func
from engine_ops import bootstrap_scoring_engine
from template_ops import tf_resource_block, sha256_text
main_tf_text = Path('terraform/main.tf').read_text()
engine_inputs = engine_hash_inputs(
    int(os.environ['TF_VAR_template_vm_id']), quotient_ref,
    main_tf_text, bootstrap_scoring_engine)
engine_hash = hash_from_inputs(engine_inputs)
print('engine hash:', engine_hash)

ssh_key = '/home/hna/dev/dawgsec/tezcatlipoca/proxmox'
vm_username = os.environ['TF_VAR_vm_username']
ctx = {
    'node': node,
    'comp_dir': comp_dir,
    'scoring_engine_ip': build_ip,
    'ssh_key_path': ssh_key,
    'vm_username': vm_username,
    'box_username': vm_username,
    'postgres_password': postgres_password,
    'redis_password': redis_password,
}
# the deploy ctx also carries known_hosts; build_ctx gets it via **ctx upstream
ctx['known_hosts'] = None

if not wait_for_ssh(ssh_key, vm_username, build_ip, timeout=120):
    raise SystemExit('build VM SSH not up')
print('SSH OK')

print('waiting for settle...')
unsettled = wait_boxes_settled([{'vmid': engine_vmid + 140, 'ip': build_ip}], node, timeout=600,
                               ssh_fallback=lambda t: _box_settled_via_ssh(ctx, t))
if unsettled:
    print('WARNING: never fully settled — proceeding')

print('bootstrapping (packages, Docker, Quotient, apt-cacher-ng)...')
build_info = engine_ops.bootstrap_scoring_engine(ctx, postgres_password, redis_password,
                                                 quotient_ref=quotient_ref)
print('bootstrap done:', str(build_info)[:200])

print('cleaning for template conversion...')
engine_ops.clean_engine_for_template(ctx)
vmid = engine_vmid + 140
stop_vm(node, vmid)
proxmox_api('POST', f'/nodes/{node}/qemu/{vmid}/template')
template_ops.write_template_hash(node, vmid, engine_hash,
                                 extra=f'quotient_ref={quotient_ref or "default"}')
print(f'vmid {vmid} converted to engine template')

state['engine_build_info'] = build_info
state['engine_template_vmid'] = vmid
state['engine_template_hash'] = engine_hash
state_path.write_text(json.dumps(state, indent=2))
# the reuse check reads .template-hashes.json (NOT .deploy_state.json) for BOTH the
# stamped description hash and the recorded engine entry — write it exactly as
# deploy.py's save_template_hashes would.
import template_ops
template_ops.save_template_hashes(comp_dir, engine={'hash': engine_hash,
                                                   'inputs': engine_inputs})
print('state + template-hashes saved')
