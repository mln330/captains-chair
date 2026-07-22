"""Make It So."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("make-it-so")
except PackageNotFoundError:
    # Source-tree execution does not have installed package metadata.
    __version__ = "0.3.0"

SIDECAR_PROTOCOL_VERSION = 1

__all__ = ["SIDECAR_PROTOCOL_VERSION", "__version__"]
