"""Tasks A2A em memoria, com o estado pausado guardado por Task.

O `requestState` do MCP fica aqui, ligado a Task, opaco: o agente guarda e ecoa,
nunca abre. Ele nao entra em nenhuma resposta A2A.
"""

from __future__ import annotations

import asyncio
import secrets
import sys
from dataclasses import dataclass, field
from typing import Any

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
CANCELED = "TASK_STATE_CANCELED"
FAILED = "TASK_STATE_FAILED"
TERMINAIS = {COMPLETED, CANCELED, FAILED}


def novo_id(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


@dataclass
class Pausa:
    """O que o agente precisa para retomar o `tools/call` original."""

    chave: str  # a chave do inputRequests, devolvida igual em inputResponses
    request_state: str  # opaco, ecoado sem modificacao
    argumentos: dict[str, Any]  # o pedido original, repetido no retry
    alternativas: list[str]  # o enum da elicitation, na ordem recebida


@dataclass
class Task:
    id: str
    context_id: str
    traceparent: str | None
    estado: str = SUBMITTED
    status_mensagem: dict[str, Any] | None = None
    historico: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    pausa: Pausa | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def terminal(self) -> bool:
        return self.estado in TERMINAIS

    def mensagem_do_usuario(self, mensagem: dict[str, Any]) -> None:
        self.historico.append(mensagem)

    def trabalhar(self) -> None:
        sys.stderr.write(f"[a2a] task={self.id} {self.estado} -> {WORKING}\n")
        sys.stderr.flush()
        self.estado = WORKING

    def responder(self, estado: str, texto: str) -> None:
        """Muda o estado e registra a mensagem do agente no status e no historico."""
        mensagem = {
            "messageId": novo_id("msg"),
            "role": "ROLE_AGENT",
            "parts": [{"text": texto}],
            "taskId": self.id,
            "contextId": self.context_id,
        }
        anterior = self.estado
        self.estado = estado
        self.status_mensagem = mensagem
        self.historico.append(mensagem)
        if self.terminal:
            self.pausa = None  # estado terminal e definitivo: o requestState nao e mais necessario
        sys.stderr.write(f"[a2a] task={self.id} {anterior} -> {estado}\n")
        sys.stderr.flush()

    def concluir_com_reserva(self, reserva: dict[str, Any], texto_artifact: str) -> None:
        self.artifacts.append({"artifactId": novo_id("art"), "name": "reserva", "parts": [{"text": texto_artifact}]})
        self.responder(COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")

    def para_wire(self) -> dict[str, Any]:
        status: dict[str, Any] = {"state": self.estado}
        if self.status_mensagem is not None:
            status["message"] = self.status_mensagem
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": list(self.historico),
            "artifacts": list(self.artifacts),
        }


class Tasks:
    def __init__(self) -> None:
        self._por_id: dict[str, Task] = {}

    def criar(self, traceparent: str | None) -> Task:
        task = Task(id=novo_id("task"), context_id=novo_id("ctx"), traceparent=traceparent)
        self._por_id[task.id] = task
        return task

    def obter(self, task_id: str) -> Task | None:
        return self._por_id.get(task_id)
