# A Ponte: Central de Salas

Entrega do desafio "A Ponte: um agente A2A com MCP por dentro" (MBA Engenharia de Software com IA, curso de MCP e A2A).

Dois processos separados, em Python, falando por HTTP:

- `servidor-mcp/`: servidor MCP em Streamable HTTP (stateless, spec `2026-07-28`) na porta `7301`, com as tools `listar_salas`, `consultar_disponibilidade` e `reservar_sala`, o resource `politica://uso` e o ciclo de MRTR na reserva.
- `agente/`: o agente da Central de Salas. Por dentro e host MCP (descobre as tools por `tools/list`, le o resource, chama `reservar_sala`). Por fora e servidor A2A v1.0 (binding JSON-RPC) na porta `7300`, com Agent Card, `SendMessage` e `GetTask`. Nao usa LLM: o pedido tem formato fixo e cada decisao e um `if`.

Stack: Python 3.10+ e o SDK oficial `mcp==2.3.0` (versao travada em `servidor-mcp/pyproject.toml` e `agente/pyproject.toml`). O lado A2A usa Starlette e uvicorn, que ja vem como dependencias do SDK.

## Como rodar

A partir de um clone limpo, na raiz do repositorio. Os comandos abaixo sao para PowerShell no Windows; a versao para bash vem logo depois.

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -e .\servidor-mcp -e .\agente
```

Instalar em modo editavel (`-e`) apenas resolve as dependencias travadas nos dois `pyproject.toml`; os processos rodam direto dos arquivos `.py` do repositorio.

Gere a chave de integridade do `requestState` (32 bytes aleatorios em hex) e exporte na variavel `REQUEST_STATE_SECRET`. Nunca a coloque no codigo nem no repositorio.

```powershell
$env:REQUEST_STATE_SECRET = (python -c "import secrets; print(secrets.token_hex(32))")
```

Terminal 1, servidor MCP (deixe o stderr visivel, e o log de cada request):

```powershell
powershell -ExecutionPolicy Bypass -File .\subir-mcp.ps1
```

Terminal 2, agente (nao precisa do segredo):

```powershell
powershell -ExecutionPolicy Bypass -File .\subir-agente.ps1
```

O `-ExecutionPolicy Bypass` e necessario porque a politica padrao do Windows bloqueia scripts `.ps1`. Se preferir nao usar os scripts, os comandos equivalentes sao `.\.venv\Scripts\python servidor-mcp\servidor.py` e `.\.venv\Scripts\python agente\agente.py`.

Terminal 3, validador:

```powershell
python validador\validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Rode o validador sempre com os dois processos recem-iniciados: as reservas criadas numa execucao mudam o resultado da seguinte.

No bash (Linux, macOS ou Git Bash):

```bash
python3 -m venv .venv
.venv/bin/pip install -e ./servidor-mcp -e ./agente  # no Git Bash: .venv/Scripts/pip
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
sh subir-mcp.sh                                        # terminal 1
sh subir-agente.sh                                     # terminal 2
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Os scripts apenas chamam `python servidor-mcp/servidor.py` e `python agente/agente.py` com o Python da `.venv`. Portas e caminhos podem ser trocados por variavel de ambiente (`MCP_HOST`, `MCP_PORT`, `MCP_PATH`, `A2A_HOST`, `A2A_PORT`, `A2A_URL`, `MCP_URL`), com os padroes do enunciado. Se subir o agente em `A2A_HOST=0.0.0.0`, defina tambem `A2A_URL` com a URL que os clientes alcancam, porque e ela que vai no Agent Card.

Para reiniciar o servidor MCP e conferir que o `requestState` continua valido, exporte o mesmo `REQUEST_STATE_SECRET` nos dois processos do servidor: a chave e o unico vinculo entre eles.

## Onde a ponte acontece

**A pausa.** Em `agente/agente.py`, a funcao `aplicar_resultado` recebe o retorno cru do `tools/call` que o host MCP fez em `cliente_mcp.py` (`HostMCP.reservar`, sempre com `allow_input_required=True`, para o SDK nao responder a elicitation sozinho). Quando o retorno e um `InputRequiredResult`, ela le a unica entrada do `inputRequests`, extrai o `enum` (ou `const`) da propriedade `sala` do `requestedSchema`, guarda num objeto `Pausa` ligado a Task a chave do `inputRequests`, o `requestState` opaco, os argumentos originais e a lista de alternativas, e muda a Task para `TASK_STATE_INPUT_REQUIRED` com a mensagem `alternativas: <ids na ordem do enum>`. O `requestState` fica em `tarefas.py`, dentro da Task, e nao entra em nenhuma resposta A2A.

**A retomada.** Ainda em `agente/agente.py`, a funcao `continuar_task` trata o `SendMessage` que referencia a Task pausada. `escolha=<id>` dentro do enum vira `{"action": "accept", "content": {"sala": "<id>"}}`; `escolha=recusar` vira `{"action": "decline"}`; qualquer outra coisa mantem a Task pausada e repete a lista. Em seguida ela chama de novo `HostMCP.reservar` com os mesmos argumentos originais, `input_responses` sob a mesma chave que veio no `inputRequests` e o `requestState` ecoado sem modificacao. O SDK atribui um id de JSON-RPC novo a cada request, entao o retry nunca repete o id do request inicial (no log do servidor aparecem como `id=3` e `id=5`, por exemplo, e o retry e marcado com `retry=sim`). O resultado volta para `aplicar_resultado`, que conclui a Task em `COMPLETED` com o artifact `reserva`, em `CANCELED` na recusa, ou em `FAILED` quando a tool devolve `isError`.

**Do lado do servidor**, em `servidor-mcp/servidor.py`, o MRTR nasce no resolver `escolha_de_sala`, ligado ao parametro `escolha` da tool `reservar_sala` por `Annotated[ElicitationResult[Escolha], Resolve(escolha_de_sala)]`. O resolver roda antes do corpo da tool em todas as rodadas: valida o pedido, e se ha conflito devolve `Elicit(...)` com um modelo pydantic criado na hora, cuja propriedade `sala` e um `Literal` das alternativas calculadas pela regra do enunciado. O SDK transforma isso em `resultType: "input_required"`, com a chave `__main__:escolha_de_sala` no `inputRequests` e o `requestState` selado. No retry o SDK confere a integridade do estado, valida o `inputResponses` contra o schema e injeta a escolha; so entao o corpo da tool roda e cria a reserva na sala escolhida.

## Decisoes tecnicas

**Protecao do `requestState`.** Uso o utilitario do proprio SDK: `RequestStateSecurity(keys=[chave], ttl=600)` passado ao `MCPServer`. Ele sela o estado com AES-256-GCM (chave derivada por HKDF do segredo) e carrega, dentro do envelope, a expiracao, o metodo, o nome da tool e um digest SHA-256 dos argumentos originais. Na volta, o SDK rejeita com `-32602` ("Invalid or expired requestState") qualquer token adulterado, expirado ou apresentado com argumentos diferentes dos selados. Por isso um retry com argumentos adulterados nao produz reserva: o servidor rejeita o estado, que e um dos dois caminhos aceitos pelo enunciado. A chave vem de `REQUEST_STATE_SECRET` (hex, minimo 32 bytes); o processo se recusa a subir sem ela. Validade: 10 minutos.

**Nada em memoria entre a pausa e o retry.** O estado vive no proprio token: o SDK guarda nele a resposta ja validada da rodada anterior e, no retry, o resolver recalcula o conflito e as alternativas a partir dos argumentos. Um retry depois de reiniciar o servidor funciona desde que a mesma chave esteja no ambiente (testado: `input_required`, restart, retry com o mesmo `requestState`, `resultType: "complete"`).

**Estado das Tasks.** Em memoria, num dicionario por id em `agente/tarefas.py`. Cada Task carrega id, `contextId`, estado, mensagem de status, historico, artifacts, o `traceparent` recebido do cliente A2A e, quando pausada, o objeto `Pausa` com o `requestState`. Estado terminal e definitivo: `SendMessage` numa Task `COMPLETED`, `CANCELED` ou `FAILED` recebe erro JSON-RPC `-32004`. Task inexistente recebe `-32001`. Nao sobrevive a restart, como permite o enunciado.

**Reservas.** Em memoria, numa lista carregada de `dados/reservas.json` na subida (`servidor-mcp/dominio.py`). Reservas criadas ficam visiveis para as consultas seguintes do mesmo processo.

**Descoberta e `_meta`.** O agente mantem um `Client` do SDK aberto durante toda a vida do processo, pinado na revisao `2026-07-28`. Na primeira Task ele faz `tools/list` e `resources/read` de `politica://uso` (de onde extrai a versao `2026-11-01` que vai no artifact), e so chama `reservar_sala` se ela apareceu na listagem. O SDK estampa em cada request o `_meta` obrigatorio e os headers `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name`. O `traceparent` e acrescentado pelo agente com o mesmo trace-id do header recebido no A2A e um span-id novo por request.

**Capability de elicitation.** O SDK Python so declara `elicitation` no `_meta` quando o cliente registra um `elicitation_callback`, e entao declara `{"elicitation": {"form": {}, "url": {}}}`. O agente registra um callback inerte, que nunca roda na revisao `2026-07-28` porque a pergunta chega como resultado e nao como request do servidor. A chave `url` extra vem do SDK (`mcp/client/session.py`, metodo `_build_capabilities`), e o servidor exige apenas `form`. Evidencia no SDK:

```python
elicitation = (
    types.ElicitationCapability(form=types.FormElicitationCapability(), url=types.UrlElicitationCapability())
    if self._elicitation_callback is not _default_elicitation_callback
    else None
)
```

**Log em stderr.** Um middleware ASGI fino, em volta do app Streamable HTTP do SDK, le o corpo de todo POST antes de o SDK decidir se aceita ou rejeita, e registra cada request JSON-RPC com metodo, id, nome da tool ou URI, se o cliente declarou elicitation em form mode, se e retry, e o `traceparent`. Assim os requests rejeitados por `_meta` incompleto (`-32602`, HTTP 400) tambem aparecem. Trecho de uma execucao do validador, com o trace-id que ele imprimiu (`f681323e...`):

```
[mcp] method=tools/list id=1 name= elicitation=form traceparent=00-f681323e192399455c4b889fc82c3cf0-51d89990f3b9a44c-01
[mcp] method=resources/read id=2 name=politica://uso elicitation=form traceparent=00-f681323e192399455c4b889fc82c3cf0-be97352fc61296b6-01
[mcp] method=tools/call id=3 name=reservar_sala elicitation=form traceparent=00-f681323e192399455c4b889fc82c3cf0-a7f7b8ff29f45fb8-01
[mcp] method=tools/call id=4 name=reservar_sala elicitation=form traceparent=00-f681323e192399455c4b889fc82c3cf0-1180b7e60e2522dd-01
[mcp] method=tools/call id=5 name=reservar_sala elicitation=form retry=sim traceparent=00-f681323e192399455c4b889fc82c3cf0-66e463a889ff747a-01
```

O agente tambem loga no stderr cada request A2A e cada transicao de estado de Task (`[a2a] task=... TASK_STATE_SUBMITTED -> TASK_STATE_WORKING`), ja que `SUBMITTED` e `WORKING` duram so o tempo da chamada MCP e nao sao observaveis por `GetTask` de fora.

**Retry e falhas.** Na continuacao de uma Task pausada, um erro de protocolo vindo do servidor MCP (por exemplo `requestState` expirado, `-32602`) termina a Task em `FAILED`. Uma falha transitoria (servidor fora do ar, timeout) devolve a Task a `TASK_STATE_INPUT_REQUIRED` com a mesma lista de alternativas, porque a pausa continua valida e o cliente pode tentar de novo. Ao entrar em estado terminal a Task descarta o `requestState`.

**Recusa quando o conflito sumiu.** O resolver roda de novo no retry. Se nesse meio tempo o conflito deixou de existir (so acontece apos um restart do servidor, que perde as reservas criadas em runtime), o resolver nao pergunta nada e o SDK nao consulta o `inputResponses`. Para a recusa do usuario continuar valendo, a tool `reservar_sala` recebe o `Context` e, quando nao ha escolha injetada, olha `ctx.input_responses`: um `decline` ou `cancel` conclui sem reservar.

**Mensagens de erro.** Os erros de execucao sao `ToolError` com o texto exato do enunciado. O SDK 2.3.0 prefixa o bloco de texto com `Error executing tool <nome>: ` (por exemplo `Error executing tool reservar_sala: Sala inexistente: sala-delorean`), forma que o enunciado aceita explicitamente. O agente repassa esse texto integral para o historico da Task em `FAILED`.

## Saida do validador

Ultima execucao, com os dois processos recem-iniciados:

```
trace-id desta execucao: f681323e192399455c4b889fc82c3cf0
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```

Codigo de saida 0.

## Estrutura

```
.
├── README.md
├── dados/                 (nao alterado)
├── validador/             (nao alterado)
├── exemplos/              (nao alterado)
├── servidor-mcp/
│   ├── pyproject.toml
│   ├── servidor.py        MCPServer, tools, resource, resolver do MRTR, middleware de log
│   └── dominio.py         dados, politica, conflitos, alternativas, reservas em memoria
├── agente/
│   ├── pyproject.toml
│   ├── agente.py          Agent Card, /a2a, SendMessage, GetTask, a ponte
│   ├── cliente_mcp.py     host MCP: Client do SDK, tools/list, resource, tools/call cru
│   └── tarefas.py         Tasks em memoria, estado pausado por Task
├── subir-mcp.ps1 / .sh
└── subir-agente.ps1 / .sh
```
