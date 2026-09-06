"""Execution-scoped runtime services.

Nothing in this package is part of a Skill or its frozen Interface Contract.
"""

from .runtime_io_mapping_planner import (
    DEFAULT_RUNTIME_IO_CAPABILITIES,
    RuntimeExecutionIOMapping,
    RuntimeIOMappingError,
    RuntimeIOMappingPlanner,
    adapt_runtime_output_before_validation,
    execute_runtime_io_mapping,
)

__all__ = [
    "DEFAULT_RUNTIME_IO_CAPABILITIES",
    "RuntimeExecutionIOMapping",
    "RuntimeIOMappingError",
    "RuntimeIOMappingPlanner",
    "adapt_runtime_output_before_validation",
    "execute_runtime_io_mapping",
]
