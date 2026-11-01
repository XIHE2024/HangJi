"""Compatibility entry point for the rebuilt airport-hour A/B pipeline.

The implementation lives in :mod:`modeling_pipeline`. Keeping this module
name preserves the Web server and existing operational scripts while ensuring
all callers use the v4 model contract.
"""

from modeling_pipeline import *  # noqa: F401,F403
from modeling_pipeline import main


if __name__ == "__main__":
    main()
