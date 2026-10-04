"""Terraform plumbing shared by the phases that run `terraform apply` (2 and 4)."""

import json
import os

from config_ops import write_text_atomic
from range_ops import terraform_dir, terraform_plugin_cache_dir


def write_tfvars(ctx):
    """Persist ctx.tfvars to the per-competition terraform.tfvars.json (0600, atomic)."""
    write_text_atomic(ctx.tfvars_path, json.dumps(ctx.tfvars, indent=2))


def terraform_env_and_cwd(ctx):
    """(env, cwd) for a terraform run: the shared provider-plugin cache + the comp workdir."""
    tf_env = {**os.environ, "TF_PLUGIN_CACHE_DIR": str(terraform_plugin_cache_dir())}
    return tf_env, str(terraform_dir(ctx.comp_dir))
