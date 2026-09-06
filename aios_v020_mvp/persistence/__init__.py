"""Persistence subpackage for the v0.2.0 MVP.

Contains the JSON-based workflow store and the filesystem
artefact store. The MVP avoids Redis to keep the runnable
footprint to a single Python process.
"""

from .json_store import JSONStore, JSONStoreError
from .files import FileResultStore

__all__ = ["JSONStore", "JSONStoreError", "FileResultStore"]
