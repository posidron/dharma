"""Dharma exception hierarchy."""
from __future__ import annotations


class DharmaError(Exception):
    """Base exception for all Dharma errors."""


class GrammarError(DharmaError):
    """Error in grammar parsing or structure."""


class ParseError(GrammarError):
    """Error parsing a grammar file line."""

    def __init__(self, message: str, namespace: str = "", lineno: int = 0) -> None:
        self.namespace = namespace
        self.lineno = lineno
        location = f"Line {lineno} [{namespace}]" if namespace else ""
        super().__init__(f"{location}: {message}" if location else message)


class XRefError(GrammarError):
    """Error resolving cross-references between grammar elements."""


class ConfigError(DharmaError):
    """Error in configuration."""


class ServerError(DharmaError):
    """Error in the WebSocket server."""
