"""AIOS v0.2.0 usable MVP package.

This package is the AIOS v0.2.0 working MVP. It implements the full
required flow end-to-end on a single Python process:

    POST /task -> Workflow create -> Planner -> Executor (real model + file tools)
    -> Reviewer -> Result persistence -> GET /task/<id>

The MVP deliberately uses only the Python standard library plus
``requests`` so it can run on any Linux/Windows/macOS host with
Python 3.11+ and no external services. A real HTTP provider adapter
is included; when ``MINIMAX_API_KEY`` (or any other supported key)
is present, the Executor calls the real model. When no key is
present, a deterministic ``local`` provider is used so the full
flow can still be exercised and verified end-to-end.

The MVP keeps the role boundaries from the AIOS architecture:

    * Entry Gateway  - accepts POST /task, builds initial Workflow.
    * Planner        - independent role, reads task, emits plan JSON.
    * Executor       - independent role, drives the real model and
                        file tools to produce the work product.
    * Reviewer       - strict, independent role, validates the work
                        product against acceptance criteria and either
                        accepts or rejects.
    * Result Store   - persists workflow state + artefacts.
"""

__version__ = "0.2.0"
__all__ = ["__version__"]
