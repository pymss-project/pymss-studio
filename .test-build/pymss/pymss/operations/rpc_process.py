"""Experimental JSON-RPC process adapter, sharing command file publication."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from . import OperationContext
from .command import CommandOperation, read_commands, register_process_operations
from ._command_process import run_command_process
from ._rpc_session import RpcSession


@dataclass(frozen=True)
class RpcOperation(CommandOperation):
    permissions: frozenset[str] = frozenset()
    host_timeout: float = 5

    def invoke(self, context: OperationContext, program: Path | tuple[str, ...], files: Mapping[str, Path],
               params: Mapping[str, Any], output: Path, stage: Path) -> None:
        output_name = next(iter(self.spec.outputs))
        session = RpcSession(context=context, plugin_id=self.plugin_id, operation_id=self.spec.id, inputs=files,
            params=params, output_name=output_name, output=output, permissions=self.permissions,
            host_timeout=self.host_timeout, handshake_timeout=min(self.timeout, 10))
        arguments = list(program) if isinstance(program, tuple) else [str(program)]
        run_command_process([*arguments, *self.arguments], cwd=stage, context=context, timeout=self.timeout, protocol=session)


def register_rpc_processes(plugin, directory: str | Path) -> None:
    register_process_operations(plugin, read_commands(directory, adapter="rpc-process"))
