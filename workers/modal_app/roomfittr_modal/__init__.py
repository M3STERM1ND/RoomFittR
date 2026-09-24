"""Thin Modal wrappers for the pipeline (2.4 repo layout).

Importing this package pulls in `modal`, which the pipeline itself never
does. Everything that can run without Modal lives in `roomfittr_pipeline`.
"""

from .app import app

__all__ = ["app"]
