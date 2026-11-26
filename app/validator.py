"""Contratos de saída e validação determinística pós-LLM (zero I/O)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field


class InvestigationReport(BaseModel):
    """Contrato estruturado do diagnóstico final produzido pelo agente."""

    status: Literal["coletivo", "individual", "inconclusivo"] = Field(
        description="Classificação final: coletivo, individual ou inconclusivo."
    )
    resumo: str = Field(
        description="Resumo objetivo do diagnóstico em português para o analista."
    )
    hipotese: str = Field(
        description="Causa provável sustentada pelas evidências coletadas."
    )
    evidencias: list[str] = Field(
        default_factory=list,
        description="Lista de evidências concretas citando IDs de pedidos, eventos e estados.",
    )
    affected_order_ids: list[str] = Field(
        default_factory=list,
        description=(
            "IDs dos pedidos com falha de entrega confirmada. Deve ficar vazio quando "
            "não houver falha confirmada (ex.: pendente, dentro do prazo ou acesso já ativo)."
        ),
    )
    reported_order_ids: list[str] = Field(
        default_factory=list,
        description="Subconjunto de affected_order_ids que já possui chamado de suporte.",
    )
    unreported_order_ids: list[str] = Field(
        default_factory=list,
        description="Subconjunto de affected_order_ids sem chamado registrado.",
    )
    recomendacao: str = Field(
        description="Próximos passos recomendados para a equipe humana (Suporte/Tech)."
    )
    limitacoes: list[str] = Field(
        default_factory=list,
        description="Limitações dos dados, indisponibilidades ou ressalvas do diagnóstico.",
    )


class ToolCallRecord(BaseModel):
    """Registro sanitizado de uma chamada MCP para auditoria (RF02)."""

    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


def dedupe_preserve_order(items: list[str]) -> list[str]:
    """Remove duplicados mantendo a ordem original."""
    return list(dict.fromkeys(items))


@dataclass
class EvidenceTracker:
    """Acumula fatos reais retornados pelas tools MCP para auditar a saída do LLM."""

    trigger_order_id: str
    observed_orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    observed_ticket_order_ids: set[str] = field(default_factory=set)
    tickets_checked: bool = False
    primary_error: str | None = None
    service_unavailable: bool = False
    hit_tool_limit: bool = False

    def record(
        self, tool_name: str, arguments: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        """Extrai IDs reais e estados objetivos de cada retorno MCP."""
        error = payload.get("error")
        if error:
            if (
                tool_name in ("get_order", "get_journey")
                and arguments.get("order_id") == self.trigger_order_id
            ):
                self.primary_error = error
            if error == "service_unavailable":
                self.service_unavailable = True
            return

        if tool_name == "get_order" and "order_id" in payload:
            self.observed_orders[payload["order_id"]] = payload
        elif tool_name == "get_journey" and isinstance(payload.get("order"), dict):
            order_data = payload["order"]
            if "order_id" in order_data:
                self.observed_orders.setdefault(order_data["order_id"], {}).update(
                    order_data
                )
                self.observed_orders[order_data["order_id"]]["has_access"] = (
                    payload.get("has_access")
                )
        elif tool_name == "find_related_orders":
            for item in payload.get("related_orders", []):
                if isinstance(item, dict) and "order_id" in item:
                    self.observed_orders[item["order_id"]] = item
        elif tool_name == "get_related_tickets":
            self.tickets_checked = True
            for ticket in payload.get("tickets", []):
                if isinstance(ticket, dict) and "order_id" in ticket:
                    self.observed_ticket_order_ids.add(ticket["order_id"])


def validate_report(
    report: InvestigationReport,
    evidence: EvidenceTracker,
    *,
    max_tool_calls: int = 8,
) -> InvestigationReport:
    """Valida determinísticamente IDs, contagens e status contra evidências reais."""
    limitacoes = list(report.limitacoes)
    status = report.status

    # 1. Pedido principal inexistente, fora de escopo ou backend indisponível sem dados.
    if evidence.primary_error and evidence.trigger_order_id not in evidence.observed_orders:
        note = (
            "Pedido de referência não encontrado no escopo autorizado."
            if evidence.primary_error == "order_not_found"
            else "Serviço de consulta indisponível durante a investigação."
        )
        if note not in limitacoes:
            limitacoes.append(note)
        return report.model_copy(
            update={
                "status": "inconclusivo",
                "affected_order_ids": [],
                "reported_order_ids": [],
                "unreported_order_ids": [],
                "limitacoes": dedupe_preserve_order(limitacoes),
            }
        )

    # 2. Remove IDs alucinados (que nunca apareceram nas respostas reais das tools MCP).
    raw_affected = dedupe_preserve_order(report.affected_order_ids)
    invalid_ids = [oid for oid in raw_affected if oid not in evidence.observed_orders]
    valid_affected = [oid for oid in raw_affected if oid in evidence.observed_orders]
    if invalid_ids:
        limitacoes.append(
            "IDs descartados na validação do backend por ausência de evidência nas "
            f"ferramentas: {', '.join(invalid_ids)}."
        )

    # 3. Remove falsos positivos objetivos (pendente, acesso já ativo ou dentro do prazo).
    filtered_affected: list[str] = []
    excluded_reasons: list[str] = []
    for oid in valid_affected:
        order = evidence.observed_orders[oid]
        if order.get("payment_status") != "paid":
            excluded_reasons.append(f"{oid} (payment_status={order.get('payment_status')})")
        elif order.get("has_access") is True:
            excluded_reasons.append(f"{oid} (acesso já concedido)")
        elif order.get("past_deadline") is False:
            excluded_reasons.append(f"{oid} (dentro do prazo)")
        else:
            filtered_affected.append(oid)

    if excluded_reasons:
        limitacoes.append(
            "Pedidos removidos da lista de falha confirmada pela validação do backend: "
            + ", ".join(excluded_reasons)
            + "."
        )

    # 4. Garante coerência entre classificação coletiva e quantidade mínima de afetados.
    if status == "coletivo" and len(filtered_affected) < 2:
        status = "individual" if evidence.observed_orders else "inconclusivo"
        limitacoes.append(
            "Classificação ajustada pelo backend: falha coletiva exige ao menos 2 pedidos afetados confirmados."
        )

    # 5. Reconcilia pedidos com chamado vs. sem chamado (sempre disjuntos).
    if evidence.tickets_checked:
        known_reported = evidence.observed_ticket_order_ids | {evidence.trigger_order_id}
        reported = [oid for oid in filtered_affected if oid in known_reported]
        unreported = [oid for oid in filtered_affected if oid not in known_reported]
    else:
        reported = [
            oid
            for oid in dedupe_preserve_order(report.reported_order_ids)
            if oid in filtered_affected
        ]
        if (
            evidence.trigger_order_id in filtered_affected
            and evidence.trigger_order_id not in reported
        ):
            reported.insert(0, evidence.trigger_order_id)
        reported_set = set(reported)
        unreported = [oid for oid in filtered_affected if oid not in reported_set]
        if len(filtered_affected) > 1:
            limitacoes.append(
                "Chamados dos pedidos relacionados não foram consultados via ferramenta; "
                "separação com/sem chamado baseada apenas no chamado atual."
            )

    if evidence.service_unavailable:
        limitacoes.append(
            "Uma ou mais chamadas às ferramentas MCP retornaram erro de indisponibilidade."
        )
    if evidence.hit_tool_limit:
        limitacoes.append(
            f"Investigação atingiu o limite máximo de {max_tool_calls} chamadas MCP."
        )

    return report.model_copy(
        update={
            "status": status,
            "affected_order_ids": filtered_affected,
            "reported_order_ids": reported,
            "unreported_order_ids": unreported,
            "limitacoes": dedupe_preserve_order(limitacoes),
        }
    )
