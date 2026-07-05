import importlib.util
import sys
from pathlib import Path

import pytest

_BLECONN = (
    Path(__file__).resolve().parent.parent
    / "custom_components" / "unifi_ble" / "bleconn.py"
)


@pytest.fixture(scope="session")
def bleconn():
    """Load bleconn.py directly, bypassing the HA-importing package __init__.
        FIXME: Move bleconn.py into a subdir to avoid triggering __init__.py
    """
    spec = importlib.util.spec_from_file_location("unifi_ble_bleconn", _BLECONN)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # required for the slots=True dataclass
    spec.loader.exec_module(module)
    return module
