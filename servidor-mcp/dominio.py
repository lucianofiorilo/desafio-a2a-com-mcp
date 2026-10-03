"""Dominio da Central de Salas: dados, politica de uso e reservas em memoria.

Tudo aqui e deliberadamente simples. O foco do desafio e protocolo, entao este
modulo so carrega os arquivos de `dados/`, aplica as tres regras da politica e
mantem a lista de reservas na memoria do processo.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel

RAIZ = Path(__file__).resolve().parents[1]
DADOS = RAIZ / "dados"

FUSO_SAO_PAULO = timezone(timedelta(hours=-3))
JANELA_INICIO = 8
JANELA_FIM = 20
DURACAO_MAXIMA = timedelta(hours=2)
MAXIMO_DE_ALTERNATIVAS = 3

MSG_SALA_INEXISTENTE = "Sala inexistente: {sala}"
MSG_FORA_DA_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
MSG_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
MSG_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
MSG_SEM_ALTERNATIVAS = "Sem alternativas disponiveis no intervalo"
MSG_CONFLITO = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."


class ErroDeDominio(Exception):
    """Violacao de regra de negocio. A mensagem e o texto exato do enunciado."""


class Sala(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class Reserva(BaseModel):
    id: str
    sala: str
    inicio: str
    fim: str
    responsavel: str


class Intervalo(BaseModel):
    inicio: datetime
    fim: datetime


def _carregar_json(nome: str) -> list[dict]:
    with open(DADOS / nome, encoding="utf-8") as f:
        return json.load(f)


SALAS: dict[str, Sala] = {s["id"]: Sala(**s) for s in _carregar_json("salas.json")}
RESERVAS: list[Reserva] = [Reserva(**r) for r in _carregar_json("reservas.json")]


def politica_texto() -> str:
    return (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")


def politica_versao() -> str:
    primeira = politica_texto().splitlines()[0]
    return primeira.split(":", 1)[1].strip()


def _parse(iso: str) -> datetime:
    # Python 3.10 nao aceita o sufixo "Z" em fromisoformat.
    texto = iso.strip()
    if texto.endswith("Z"):
        texto = texto[:-1] + "+00:00"
    try:
        valor = datetime.fromisoformat(texto)
    except ValueError:
        raise ErroDeDominio(MSG_INTERVALO) from None
    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=FUSO_SAO_PAULO)
    return valor


def validar_pedido(sala: str, inicio: str, fim: str) -> Intervalo:
    """Aplica as validacoes compartilhadas por consultar_disponibilidade e reservar_sala."""
    if sala not in SALAS:
        raise ErroDeDominio(MSG_SALA_INEXISTENTE.format(sala=sala))
    ini, end = _parse(inicio), _parse(fim)
    if end <= ini:
        raise ErroDeDominio(MSG_INTERVALO)
    ini_sp, end_sp = ini.astimezone(FUSO_SAO_PAULO), end.astimezone(FUSO_SAO_PAULO)
    fim_no_limite = end_sp.hour == JANELA_FIM and end_sp.minute == 0 and end_sp.second == 0
    if ini_sp.hour < JANELA_INICIO or ini_sp.hour >= JANELA_FIM or (end_sp.hour >= JANELA_FIM and not fim_no_limite):
        raise ErroDeDominio(MSG_FORA_DA_JANELA)
    if end - ini > DURACAO_MAXIMA:
        raise ErroDeDominio(MSG_DURACAO)
    return Intervalo(inicio=ini, fim=end)


def conflitos(sala: str, intervalo: Intervalo) -> list[Reserva]:
    return [
        r
        for r in RESERVAS
        if r.sala == sala and _parse(r.inicio) < intervalo.fim and _parse(r.fim) > intervalo.inicio
    ]


def alternativas(sala: str, intervalo: Intervalo) -> list[str]:
    """Salas livres no intervalo com capacidade >= a da sala pedida.

    No maximo tres, ordenadas por capacidade crescente e, em empate, por id.
    """
    pedida = SALAS[sala]
    candidatas = [
        s
        for s in SALAS.values()
        if s.id != sala and s.capacidade >= pedida.capacidade and not conflitos(s.id, intervalo)
    ]
    candidatas.sort(key=lambda s: (s.capacidade, s.id))
    return [s.id for s in candidatas[:MAXIMO_DE_ALTERNATIVAS]]


_ESCRITA = threading.Lock()


def reservar_se_livre(sala: str, intervalo: Intervalo, inicio: str, fim: str, responsavel: str) -> Reserva | None:
    """Confere o conflito e cria a reserva num unico passo, sob lock.

    As tools sincronas rodam em thread no SDK, entao a checagem e a escrita
    precisam ser atomicas para dois pedidos simultaneos nao receberem o mesmo id.
    Devolve None se a sala ficou ocupada nesse meio tempo.
    """
    with _ESCRITA:
        if conflitos(sala, intervalo):
            return None
        reserva = Reserva(
            id=f"res-{len(RESERVAS) + 1:04d}",
            sala=sala,
            inicio=inicio,
            fim=fim,
            responsavel=responsavel,
        )
        RESERVAS.append(reserva)
        return reserva
