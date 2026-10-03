"""Servidor MCP da Central de Salas (Streamable HTTP, stateless, spec 2026-07-28).

Tres tools, um resource e o ciclo de MRTR na reserva. O `requestState` e selado
pelo proprio SDK com a chave de `REQUEST_STATE_SECRET`.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Annotated, Any, Literal

import uvicorn
from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.server.request_state import RequestStateSecurity
from mcp_types import ElicitResult
from pydantic import BaseModel, Field, create_model

import dominio

HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORTA = int(os.environ.get("MCP_PORT", "7301"))
CAMINHO = os.environ.get("MCP_PATH", "/mcp")
VALIDADE_DO_REQUEST_STATE = 10 * 60  # segundos: dentro da faixa de 5 a 30 minutos do enunciado


def _chave_de_integridade() -> bytes:
    segredo = os.environ.get("REQUEST_STATE_SECRET", "")
    try:
        chave = bytes.fromhex(segredo)
    except ValueError:
        chave = b""
    if len(chave) < 32:
        sys.stderr.write(
            "REQUEST_STATE_SECRET ausente ou curto demais: precisa de no minimo 32 bytes em hex.\n"
            'Gere com: python -c "import secrets; print(secrets.token_hex(32))"\n'
        )
        sys.exit(2)
    return chave


mcp = MCPServer(
    name="central-de-salas",
    version="1.0.0",
    request_state_security=RequestStateSecurity(keys=[_chave_de_integridade()], ttl=VALIDADE_DO_REQUEST_STATE),
)


# --- log de cada request recebido, no stderr ----------------------------------


def _registrar(corpo: bytes) -> None:
    """Uma linha por request JSON-RPC: metodo, id, nome, capability, retry e traceparent."""
    try:
        mensagem = json.loads(corpo)
    except ValueError:
        sys.stderr.write("[mcp] corpo ilegivel (nao e JSON)\n")
        sys.stderr.flush()
        return
    for item in mensagem if isinstance(mensagem, list) else [mensagem]:
        if not isinstance(item, dict):
            continue
        params = item.get("params") if isinstance(item.get("params"), dict) else {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        capabilities = meta.get("io.modelcontextprotocol/clientCapabilities")
        elicitation = (
            "form"
            if isinstance(capabilities, dict)
            and isinstance(capabilities.get("elicitation"), dict)
            and "form" in capabilities["elicitation"]
            else "-"
        )
        nome = params.get("name") or params.get("uri") or ""
        retry = " retry=sim" if params.get("requestState") else ""
        sys.stderr.write(
            f"[mcp] method={item.get('method')} id={item.get('id')} name={nome} "
            f"elicitation={elicitation}{retry} traceparent={meta.get('traceparent', '-')}\n"
        )
    sys.stderr.flush()


class LogDeRequests:
    """Middleware ASGI: registra todo POST antes de o SDK decidir se aceita ou rejeita."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        partes: list[bytes] = []

        async def receive_logando() -> dict[str, Any]:
            mensagem = await receive()
            if mensagem["type"] == "http.request":
                partes.append(mensagem.get("body", b""))
                if not mensagem.get("more_body"):
                    _registrar(b"".join(partes))
            return mensagem

        await self.app(scope, receive_logando, send)


# --- contratos de saida ------------------------------------------------------


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


class Escolha(BaseModel):
    sala: str


# --- resource ----------------------------------------------------------------


@mcp.resource("politica://uso", name="politica-de-uso", mime_type="text/markdown")
def politica_de_uso() -> str:
    return dominio.politica_texto()


# --- tools -------------------------------------------------------------------


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**s.model_dump()) for s in dominio.SALAS.values()])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    try:
        intervalo = dominio.validar_pedido(sala, inicio, fim)
    except dominio.ErroDeDominio as e:
        raise ToolError(str(e)) from None
    em_conflito = dominio.conflitos(sala, intervalo)
    return Disponibilidade(
        sala=sala,
        livre=not em_conflito,
        conflitos=[ConflitoOut(id=r.id, inicio=r.inicio, fim=r.fim, responsavel=r.responsavel) for r in em_conflito],
    )


def escolha_de_sala(sala: str, inicio: str, fim: str) -> Elicit[Escolha] | None:
    """Resolver do MRTR: pergunta a alternativa somente quando ha conflito.

    Roda antes do corpo da tool, em todas as rodadas. Sem conflito devolve None e
    a reserva segue direto. Com conflito devolve um `Elicit`, que o SDK transforma
    em `input_required` com o `requestState` selado. No retry o SDK reaproveita a
    resposta do cliente que veio em `inputResponses`.
    """
    try:
        intervalo = dominio.validar_pedido(sala, inicio, fim)
    except dominio.ErroDeDominio as e:
        raise ToolError(str(e)) from None
    if not dominio.conflitos(sala, intervalo):
        return None
    opcoes = dominio.alternativas(sala, intervalo)
    if not opcoes:
        raise ToolError(dominio.MSG_SEM_ALTERNATIVAS)
    schema = create_model(
        "Escolha",
        __base__=Escolha,
        sala=(Literal[tuple(opcoes)], Field(description="Sala alternativa escolhida")),
    )
    return Elicit(dominio.MSG_CONFLITO, schema)


def _recusa_no_retry(ctx: Context) -> str | None:
    """Motivo da recusa quando o cliente respondeu decline/cancel num retry.

    Cobre o caso em que o conflito sumiu entre a pausa e o retry (por exemplo,
    apos um restart do servidor): o resolver nao pergunta de novo e o SDK nao
    consulta a resposta, mas a recusa do usuario continua valendo.
    """
    for resposta in (ctx.input_responses or {}).values():
        if isinstance(resposta, ElicitResult) and resposta.action in ("decline", "cancel"):
            return "recusado" if resposta.action == "decline" else "cancelado"
    return None


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[Escolha], Resolve(escolha_de_sala)],
    ctx: Context,
) -> ReservaOut:
    if not isinstance(escolha, AcceptedElicitation):
        return ReservaOut(reservado=False, motivo="recusado" if escolha.action == "decline" else "cancelado")
    try:
        intervalo = dominio.validar_pedido(sala, inicio, fim)
    except dominio.ErroDeDominio as e:
        raise ToolError(str(e)) from None
    if escolha.data is None:
        # Sem conflito: o resolver nao perguntou nada. Se isto e um retry com recusa, honra a recusa.
        motivo = _recusa_no_retry(ctx)
        if motivo is not None:
            return ReservaOut(reservado=False, motivo=motivo)
        sala_final = sala
    else:
        sala_final = escolha.data.sala
    reserva = dominio.reservar_se_livre(sala_final, intervalo, inicio, fim, responsavel)
    if reserva is None:
        raise ToolError(dominio.MSG_SEM_ALTERNATIVAS)
    return ReservaOut(
        reserva=reserva.id,
        reservado=True,
        sala=reserva.sala,
        inicio=reserva.inicio,
        fim=reserva.fim,
        responsavel=reserva.responsavel,
        politica=dominio.politica_versao(),
    )


if __name__ == "__main__":
    sys.stderr.write(f"[mcp] central-de-salas em http://{HOST}:{PORTA}{CAMINHO}\n")
    app = LogDeRequests(
        mcp.streamable_http_app(streamable_http_path=CAMINHO, json_response=True, stateless_http=True, host=HOST)
    )
    uvicorn.run(app, host=HOST, port=PORTA, log_level="warning")
