"""Bootstrap and readiness boundary for a provisioned household runtime."""

from typing import Any

__all__ = ["RuntimeBootstrapper", "RuntimeService"]


def __getattr__(name: str) -> Any:
    """Preserve public imports without preloading a ``python -m`` target."""
    if name == "RuntimeBootstrapper":
        from hermes_cloud.runtime.bootstrap import RuntimeBootstrapper

        return RuntimeBootstrapper
    if name == "RuntimeService":
        from hermes_cloud.runtime.service import RuntimeService

        return RuntimeService
    raise AttributeError(name)
