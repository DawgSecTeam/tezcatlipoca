"""Deploy entry module: loads .env, then hands off to the `deploy_lib` package.

The pipeline lives in deploy_lib/ (layout: deploy_lib/__init__.py). This module keeps the
import-time environment bootstrap the pipeline relies on (TF_VAR_* and the Proxmox
credentials are read from the environment) and re-exports the three names callers use:
`main` (create-competition.py), `deploy` and `prepare`/`DeployContext`.
"""

from pathlib import Path

import urllib3
from dotenv import load_dotenv

ENV_PATH = Path(".env")
load_dotenv(ENV_PATH)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from deploy_lib.cli import main  # noqa: E402
from deploy_lib.context import DeployContext  # noqa: E402
from deploy_lib.prepare import prepare  # noqa: E402
from deploy_lib.runner import deploy  # noqa: E402

__all__ = ["DeployContext", "deploy", "main", "prepare"]

if __name__ == "__main__":
    main()
