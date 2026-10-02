"""Create actual test links; only unavailable Windows privilege is a platform skip."""
import os

import pytest


def create_symlink(source, destination, *, is_directory=False):
    try:
        os.symlink(source, destination, target_is_directory=is_directory)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is unavailable")
        raise
