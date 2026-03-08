"""Dharma — generation-based, context-free grammar fuzzer."""
from importlib.metadata import version, PackageNotFoundError

from dharma.dharma import DharmaCommandLine

try:
    __version__ = version("dharma")
except PackageNotFoundError:
    __version__ = "2.0.0-dev"

__all__ = ["DharmaCommandLine", "__version__"]
