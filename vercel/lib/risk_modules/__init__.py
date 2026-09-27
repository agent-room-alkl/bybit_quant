"""Shadow-mode risk modules. Pure functions; must not change live trade behavior."""
from .adaptive_exit import AdaptiveExitManager, ExitDiagnostics

__all__ = ["AdaptiveExitManager", "ExitDiagnostics"]
