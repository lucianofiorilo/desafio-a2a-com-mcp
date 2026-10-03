"""O agente por fora: servidor A2A v1.0 (binding JSON-RPC) da Central de Salas.

Publica o Agent Card, aceita `SendMessage` e `GetTask`, e costura a ponte: o
`input_required` do MCP vira `TASK_STATE_INPUT_REQUIRED`, e a resposta do cliente
A2A vira o retry do `tools/call` com `inputResponses` e o `requestState` ecoado.
Nao ha LLM: o pedido tem formato fixo e cada decisao e um `if`.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from typing import Any

import mcp_types as types
import uvicorn
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import tarefas
from cliente_mcp import HostMCP
from tarefas import Task, Tasks

HOST = os.environ.get("A2A_HOST", "127.0.0.1")
PORTA = int(os.environ.get("A2A_PORT", "7300"))
URL_PUBLICA = os.environ.get("A2A_URL", f"http://{HOST}:{PORTA}/a2a")
MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")

PEDIDO = re.compile(r"^\s*reservar\s+sala=(\S+)\s+inicio=(\S+)\s+fim=(\S+)\s+responsavel=(.+?)\s*$")
ESCOLHA = re.compile(r"^\s*escolha=(\S+)\s*$")
RECUSAR = "recusar"

# Codigos de erro JSON-RPC e A2A
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TASK_NOT_FOUND = -32001
UNSUPPORTED_OPERATION = -32004

AGENT_CARD: dict[str, Any] = {
    "name": "Central de Salas",
    "description": "Reserva salas de reuniao da Hill Valley Tech.",
    "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
    "version": "1.0.0",
    "supportedInterfaces": [{"url": URL_PUBLICA, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
    "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "reservar-sala",
            "name": "Reservar sala",
            "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
            "tags": ["salas", "agenda"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
            "examples": [
                "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
            ],
        }
    ],
}

host = HostMCP(MCP_URL)
tasks = Tasks()


class ErroA2A(Exception):
    def __init__(self, codigo: int, mensagem: str) -> None:
        super().__init__(mensagem)
        self.codigo = codigo


def log(texto: str) -> None:
    sys.stderr.write(f"[a2a] {texto}\n")
    sys.stderr.flush()


# --- helpers de wire ---------------------------------------------------------


def texto_da_mensagem(mensagem: dict[str, Any]) -> str:
    partes = mensagem.get("parts")
    if not isinstance(partes, list):
        return ""
    return " ".join(p["text"] for p in partes if isinstance(p, dict) and isinstance(p.get("text"), str))


def texto_do_resultado(resultado: types.CallToolResult) -> str:
    return " ".join(c.text for c in resultado.content if isinstance(c, types.TextContent))


def alternativas_da_elicitation(pedido: types.InputRequiredResult) -> tuple[str, list[str]]:
    """Le a unica entrada do inputRequests e devolve (chave, alternativas na ordem do enum)."""
    requests = pedido.input_requests or {}
    if len(requests) != 1:
        raise RuntimeError(f"esperava uma unica entrada em inputRequests, vieram {len(requests)}")
    chave, request = next(iter(requests.items()))
    if not isinstance(request, types.ElicitRequest):
        raise RuntimeError(f"inputRequest inesperado: {request.method}")
    schema = request.params.model_dump(by_alias=True).get("requestedSchema") or {}
    campo = (schema.get("properties") or {}).get("sala") or {}
    if "enum" in campo:
        return chave, list(campo["enum"])
    if "const" in campo:
        return chave, [campo["const"]]
    raise RuntimeError("a elicitation nao restringe a sala a um enum")


def linha_de_alternativas(alternativas: list[str]) -> str:
    return "alternativas: " + ", ".join(alternativas)


# --- a ponte -----------------------------------------------------------------


def aplicar_resultado(task: Task, resultado: types.CallToolResult | types.InputRequiredResult, argumentos: dict[str, Any]) -> None:
    """Traduz o resultado do `tools/call` em transicao de estado da Task."""
    if isinstance(resultado, types.InputRequiredResult):
        # AQUI A PONTE PAUSA: input_required do MCP vira TASK_STATE_INPUT_REQUIRED.
        chave, alternativas = alternativas_da_elicitation(resultado)
        if not resultado.request_state:
            raise RuntimeError("input_required sem requestState")
        task.pausa = tarefas.Pausa(
            chave=chave, request_state=resultado.request_state, argumentos=argumentos, alternativas=alternativas
        )
        task.responder(tarefas.INPUT_REQUIRED, linha_de_alternativas(alternativas))
        return

    task.pausa = None
    if resultado.is_error:
        task.responder(tarefas.FAILED, texto_do_resultado(resultado))
        return

    dados = resultado.structured_content if isinstance(resultado.structured_content, dict) else {}
    if dados.get("reservado") is False:
        task.responder(tarefas.CANCELED, f"Reserva nao realizada: {dados.get('motivo') or 'recusada'}.")
        return
    if dados.get("reservado") is not True or not dados.get("reserva"):
        raise RuntimeError("resultado complete sem structuredContent de reserva")

    reserva = {
        "reserva": dados.get("reserva"),
        "sala": dados.get("sala"),
        "inicio": dados.get("inicio"),
        "fim": dados.get("fim"),
        "responsavel": dados.get("responsavel"),
        "politica": host.politica_versao,
    }
    task.concluir_com_reserva(reserva, json.dumps(reserva, ensure_ascii=False))


async def abrir_task(mensagem: dict[str, Any], traceparent: str | None) -> Task:
    task = tasks.criar(traceparent)
    task.mensagem_do_usuario(mensagem)
    texto = texto_da_mensagem(mensagem)
    casado = PEDIDO.match(texto)
    if not casado:
        task.responder(tarefas.FAILED, "Pedido invalido: use reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>")
        return task
    sala, inicio, fim, responsavel = casado.groups()
    argumentos = {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel}
    task.trabalhar()
    try:
        resultado = await host.reservar(argumentos, task.traceparent)
        aplicar_resultado(task, resultado, argumentos)
    except Exception as e:  # falha de transporte ou de protocolo, nao de dominio
        log(f"task={task.id} falhou ao chamar o MCP: {e!r}")
        task.responder(tarefas.FAILED, f"Falha ao falar com o servidor MCP: {e}")
    return task


async def continuar_task(task: Task, mensagem: dict[str, Any], traceparent: str | None) -> Task:
    if task.terminal:
        raise ErroA2A(UNSUPPORTED_OPERATION, f"Task {task.id} esta em estado terminal {task.estado}")
    if task.estado != tarefas.INPUT_REQUIRED or task.pausa is None:
        raise ErroA2A(UNSUPPORTED_OPERATION, f"Task {task.id} nao esta aguardando input (estado {task.estado})")
    task.mensagem_do_usuario(mensagem)
    if task.traceparent is None:
        task.traceparent = traceparent  # a primeira mensagem veio sem trace; adota o da continuacao
    pausa = task.pausa
    casado = ESCOLHA.match(texto_da_mensagem(mensagem))
    escolha = casado.group(1) if casado else None

    if escolha is None or (escolha != RECUSAR and escolha not in pausa.alternativas):
        # Fora do enum: continua pausada e repete a lista.
        task.responder(tarefas.INPUT_REQUIRED, linha_de_alternativas(pausa.alternativas))
        return task

    if escolha == RECUSAR:
        resposta = types.ElicitResult(action="decline")
    else:
        resposta = types.ElicitResult(action="accept", content={"sala": escolha})

    # AQUI A PONTE RETOMA: o tools/call original e repetido com id novo, levando
    # inputResponses com a mesma chave e o requestState ecoado sem modificacao.
    task.trabalhar()
    try:
        resultado = await host.reservar(
            pausa.argumentos,
            task.traceparent,
            input_responses={pausa.chave: resposta},
            request_state=pausa.request_state,
        )
        aplicar_resultado(task, resultado, pausa.argumentos)
    except MCPError as e:
        # Erro de protocolo definitivo (por exemplo requestState expirado, -32602): a Task falha.
        log(f"task={task.id} o MCP recusou o retry: {e.error.code} {e.error.message}")
        task.responder(tarefas.FAILED, f"O servidor MCP recusou a continuacao: {e.error.message}")
    except Exception as e:
        # Falha transitoria (MCP fora do ar, timeout): a pausa continua valida, o cliente pode tentar de novo.
        log(f"task={task.id} falha transitoria no retry ao MCP, Task volta a aguardar input: {e!r}")
        task.responder(tarefas.INPUT_REQUIRED, linha_de_alternativas(pausa.alternativas))
    return task


# --- JSON-RPC ----------------------------------------------------------------


async def send_message(params: dict[str, Any], traceparent: str | None) -> dict[str, Any]:
    mensagem = params.get("message")
    if not isinstance(mensagem, dict) or not isinstance(mensagem.get("parts"), list) or not mensagem["parts"]:
        raise ErroA2A(INVALID_PARAMS, "params.message com parts e obrigatorio")
    task_id = mensagem.get("taskId") or params.get("taskId")
    if task_id is not None and not isinstance(task_id, str):
        raise ErroA2A(INVALID_PARAMS, "taskId deve ser string")
    if task_id:
        task = tasks.obter(task_id)
        if task is None:
            raise ErroA2A(TASK_NOT_FOUND, f"Task {task_id} nao encontrada")
        async with task.lock:
            task = await continuar_task(task, mensagem, traceparent)
    else:
        task = await abrir_task(mensagem, traceparent)
    return {"task": task.para_wire()}


async def get_task(params: dict[str, Any]) -> dict[str, Any]:
    task_id = params.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise ErroA2A(INVALID_PARAMS, "params.id deve ser uma string nao vazia")
    task = tasks.obter(task_id)
    if task is None:
        raise ErroA2A(TASK_NOT_FOUND, f"Task {task_id} nao encontrada")
    return {"task": task.para_wire()}


def erro_jsonrpc(id_: Any, codigo: int, mensagem: str, status: int = 200) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": codigo, "message": mensagem}}, status_code=status)


async def endpoint_a2a(request: Request) -> JSONResponse:
    try:
        corpo = await request.json()
    except Exception:
        return erro_jsonrpc(None, PARSE_ERROR, "Parse error", 400)
    if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0" or not isinstance(corpo.get("method"), str):
        return erro_jsonrpc(corpo.get("id") if isinstance(corpo, dict) else None, INVALID_REQUEST, "Invalid Request", 400)
    id_ = corpo.get("id")
    metodo = corpo["method"]
    params = corpo.get("params")
    if params is None:
        params = {}
    traceparent = request.headers.get("traceparent")
    log(f"method={metodo} id={id_} traceparent={traceparent or '-'}")
    try:
        if not isinstance(params, dict):
            raise ErroA2A(INVALID_PARAMS, "params deve ser um objeto")
        if metodo == "SendMessage":
            resultado = await send_message(params, traceparent)
        elif metodo == "GetTask":
            resultado = await get_task(params)
        else:
            raise ErroA2A(METHOD_NOT_FOUND, f"Metodo nao suportado: {metodo}")
    except ErroA2A as e:
        return erro_jsonrpc(id_, e.codigo, str(e))
    except Exception as e:  # nunca devolver 500 cru para um cliente JSON-RPC
        log(f"erro interno em {metodo}: {e!r}")
        return erro_jsonrpc(id_, INTERNAL_ERROR, "Internal error")
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": resultado})


async def agent_card(request: Request) -> JSONResponse:
    return JSONResponse(AGENT_CARD)


@contextlib.asynccontextmanager
async def ciclo_de_vida(app: Starlette):
    await host.abrir()
    log(f"agente em http://{HOST}:{PORTA}/a2a, servidor MCP em {MCP_URL}")
    try:
        yield
    finally:
        await host.fechar()


app = Starlette(
    routes=[
        Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
        Route("/a2a", endpoint_a2a, methods=["POST"]),
    ],
    lifespan=ciclo_de_vida,
)

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORTA, log_level="warning")
