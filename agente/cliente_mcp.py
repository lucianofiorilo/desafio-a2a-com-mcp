"""O agente por dentro: host MCP falando com o servidor de salas por Streamable HTTP.

Toda chamada passa pela sessao do SDK com `allow_input_required=True`, para que o
`input_required` chegue cru ao agente. O SDK estampa em cada request os campos
obrigatorios de `_meta` (protocolVersion, clientInfo, clientCapabilities) e os
headers `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name`. O agente acrescenta o
`traceparent`.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

import mcp_types as types
from mcp.client.client import Client
from mcp.client.session import ClientRequestContext

VERSAO_DO_PROTOCOLO = "2026-07-28"
URI_POLITICA = "politica://uso"
TOOL_RESERVAR = "reservar_sala"


async def _elicitation_nunca_chamada(
    context: ClientRequestContext, params: types.ElicitRequestParams
) -> types.ElicitResult | types.ErrorData:
    """Existe apenas para o SDK declarar a capability de elicitation no `_meta`.

    Na revisao 2026-07-28 o servidor nao abre canal de volta: a pergunta chega como
    `InputRequiredResult`, que o agente trata sozinho. Este callback nunca roda.
    """
    return types.ElicitResult(action="cancel")


TRACEPARENT = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


def com_span_novo(traceparent: str | None) -> str | None:
    """Mesmo trace-id do cliente A2A, span-id novo para este request MCP.

    Um header malformado (ou com trace-id zerado, invalido pelo W3C) e descartado
    em vez de propagado.
    """
    if not traceparent:
        return None
    casado = TRACEPARENT.match(traceparent.strip().lower())
    if not casado or casado.group(2) == "0" * 32:
        return None
    versao, trace_id, _, flags = casado.groups()
    return f"{versao}-{trace_id}-{secrets.token_hex(8)}-{flags}"


def _meta(traceparent: str | None) -> types.RequestParamsMeta | None:
    tp = com_span_novo(traceparent)
    return {"traceparent": tp} if tp else None


class HostMCP:
    """Um cliente MCP vivo durante toda a vida do agente, sem estado de protocolo."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._client: Client | None = None
        self.tools: dict[str, types.Tool] = {}
        self.politica_versao: str | None = None

    async def abrir(self) -> None:
        self._client = Client(
            self.url,
            mode=VERSAO_DO_PROTOCOLO,
            client_info=types.Implementation(name="agente-central-de-salas", version="1.0.0"),
            elicitation_callback=_elicitation_nunca_chamada,
        )
        await self._client.__aenter__()

    async def fechar(self) -> None:
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None

    @property
    def sessao(self):
        assert self._client is not None, "HostMCP nao foi aberto"
        return self._client.session

    async def descobrir(self, traceparent: str | None) -> None:
        """`tools/list` antes da primeira chamada, e leitura do resource da politica."""
        if not self.tools:
            listagem = await self.sessao.list_tools(params=types.PaginatedRequestParams(_meta=_meta(traceparent)))
            self.tools = {t.name: t for t in listagem.tools}
        if self.politica_versao is None:
            lido = await self.sessao.read_resource(URI_POLITICA, meta=_meta(traceparent), allow_input_required=True)
            if isinstance(lido, types.InputRequiredResult):
                raise RuntimeError("o resource da politica pediu input, o que nao esta previsto")
            texto = next((c.text for c in lido.contents if isinstance(c, types.TextResourceContents)), "")
            self.politica_versao = texto.splitlines()[0].split(":", 1)[1].strip()

    async def reservar(
        self,
        argumentos: dict[str, Any],
        traceparent: str | None,
        *,
        input_responses: types.InputResponses | None = None,
        request_state: str | None = None,
    ) -> types.CallToolResult | types.InputRequiredResult:
        """`tools/call` de reservar_sala. Cada chamada ganha um id JSON-RPC novo no SDK."""
        await self.descobrir(traceparent)
        if TOOL_RESERVAR not in self.tools:
            raise RuntimeError(f"o servidor MCP nao expoe a tool {TOOL_RESERVAR}")
        return await self.sessao.call_tool(
            TOOL_RESERVAR,
            argumentos,
            input_responses=input_responses,
            request_state=request_state,
            meta=_meta(traceparent),
            allow_input_required=True,
        )
