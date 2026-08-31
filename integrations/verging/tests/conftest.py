"""Pin adapter state to a throwaway directory before Basic Memory is imported.

``integrations.verging`` reads ``VERGING_ADAPTER_DATA_DIR`` at package-import
time, so this has to run before any test module imports the adapter. pytest
imports conftest first, which is what makes this ordering reliable.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

TEST_PRODUCT_KEY = "test-product-key"

DATA_ROOT = Path(tempfile.mkdtemp(prefix="verging-adapter-tests-"))

os.environ["VERGING_ADAPTER_DATA_DIR"] = str(DATA_ROOT)
os.environ["VERGING_PRODUCT_KEY"] = TEST_PRODUCT_KEY
