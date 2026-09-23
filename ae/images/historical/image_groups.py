"""Instance-ID → data-image group resolver (host-side).

SWE-bench instance_ids are of the form "<owner>__<repo>-<num>", e.g.
  django__django-16527, sphinx-doc__sphinx-1234, pydata__xarray-5678.

We keep 5 data images in the repo root:
  data-django.xfs  data-sympy.xfs  data-sphinx.xfs  data-sci.xfs  data-tools.xfs
This module maps an instance_id to the correct image path.
"""
from __future__ import annotations
import os

# Owner prefix (before first "__") → group
OWNER_TO_GROUP = {
    "django":       "django",
    "sympy":        "sympy",
    "sphinx-doc":   "sphinx",
    # sci bucket
    "astropy":      "sci",
    "matplotlib":   "sci",
    "scikit-learn": "sci",
    "pydata":       "sci",    # xarray
    # tools bucket
    "pytest-dev":   "tools",
    "pylint-dev":   "tools",
    "psf":          "tools",  # requests
    "pallets":      "tools",  # flask
    "mwaskom":      "tools",  # seaborn
}

ALL_GROUPS = ("django", "sympy", "sphinx", "sci", "tools")


def infer_group(instance_id: str) -> str:
    """Return the data-image group for a given SWE-bench instance_id."""
    owner = instance_id.split("__", 1)[0]
    try:
        return OWNER_TO_GROUP[owner]
    except KeyError:
        raise ValueError(
            f"Unknown owner prefix '{owner}' in instance_id '{instance_id}'. "
            f"Known prefixes: {sorted(OWNER_TO_GROUP)}"
        )


def data_image_path(project_root: str, group: str) -> str:
    image_root = os.environ.get("DELTABOX_XFS_IMAGE_DIR", project_root)
    return os.path.join(image_root, f"data-{group}.xfs")


def data_image_for_instance(project_root: str, instance_id: str) -> str:
    return data_image_path(project_root, infer_group(instance_id))
