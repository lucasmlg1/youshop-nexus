# YouShop Nexus

**Agente interno de investigação de falhas silenciosas na jornada pós-pagamento de produtos digitais.**

> Protótipo funcional desenvolvido para o **Desafio de Inovação — Programa de Estágio YouShop 2027**.  
> **Aviso:** todo o ambiente usa **dados 100% fictícios** em SQLite local, sem credenciais, clientes ou integração com sistemas internos da YouShop.

---

## O problema que o protótipo demonstra

Em produtos digitais, um **pagamento aprovado** e um **webhook entregue (`HTTP 200`)** não garantem que o comprador recebeu acesso ao conteúdo. Quando um único cliente abre um chamado relatando falta de acesso, outros compradores da mesma integração podem estar enfrentando a mesma falha em silêncio — sem abrir ticket.

O **YouShop Nexus** apoia times de Atendimento, Operações e Tecnologia ao:
1. Interpretar um relato ambíguo de suporte;
2. Consultar pedidos, linha do tempo de eventos, política de entrega e chamados via **protocolo MCP (somente leitura)**;
3. Distinguir falha coletiva (excluindo falsos positivos como pedidos dentro do prazo ou retidos por antifraude), caso individual ou evidência insuficiente;
4. Listar os pedidos afetados separando **com chamado** vs. **sem chamado (silenciosos)**;
5. Reconsultar os pedidos após uma correção simulada via **MCP sem LLM (determinístico)** para confirmar quem foi regularizado e quem segue pendente.

A decisão operacional, a comunicação com clientes e qualquer ação corretiva permanecem **100% humanas**.

---

## Como funciona (IA vs. código determinístico)

| Camada | Papel no Nexus |
|---|---|
| **IA (`app/agent.py` + Gemini API)** | Interpreta relatos ambíguos em linguagem natural, escolhe quais tools MCP consultar, cruza contexto de eventos e sintetiza hipótese + recomendação em JSON estruturado. |
| **Protocolo MCP (`app/mcp_client.py` + `app/mcp_server.py`)** | Padroniza a descoberta e execução de 5 tools somente leitura (`readOnlyHint=True`) via `stdio`, sem expor acesso direto ao banco nem a chave da API ao servidor MCP. |
| **Backend e Guardrails (`app/api.py`, `app/validator.py`, `app/db.py`)** | Calcula prazos (`past_deadline`), aplica isolamento multi-tenant (`X-Producer-Id`), remove IDs alucinados pós-LLM, reconcilia pedidos com/sem chamado e executa a verificação pós-correção (`RF05`) **sem gastar tokens de LLM**. |

### Estrutura do repositório

```text
youshop-nexus/
├── app/
│   ├── api.py              # APIs simuladas (FastAPI), rotas de investigação e StaticFiles
│   ├── db.py               # Esquema SQLite (PRAGMA foreign_keys = ON) e persistência
│   ├── seed.py             # Massa determinística de 12 pedidos com timestamps relativos
│   ├── mcp_server.py       # Servidor MCP (stdio) com 5 tools somente leitura
│   ├── mcp_client.py       # Cliente MCP (stdio), sanitização de logs e verify_recovery (0 LLM)
│   ├── validator.py        # Contratos Pydantic e validação determinística pós-LLM
│   ├── agent.py            # Orquestrador Gemini (loop manual + síntese JSON + fallback)
│   ├── templates/
│   │   └── index.html      # Interface web Split-View (HTML semântico)
│   └── static/
│       ├── styles.css      # Estilos na paleta YouShop (#0E234A, #97E600, #F6F8F3)
│       └── app.js          # Estado da interface, presets T1–T6, timeline MCP e drawers
├── test_mcp_poc.py         # Verificação ponta a ponta das 5 tools MCP e bloqueio de escopo
├── .env.example            # Modelo de variáveis de ambiente (sem segredos)
└── requirements.txt
```

### Ferramentas MCP implementadas (somente leitura)

- `get_order(order_id)` — pedido, pagamento, acesso e cálculo determinístico de prazo (`past_deadline`).
- `get_journey(order_id)` — linha do tempo de eventos (`payment_confirmed`, `webhook_sent`, `antifraud_hold`, `access_granted`) e status de acesso.
- `find_related_orders(order_id)` — pedidos candidatos da mesma integração/produto na janela de ±4h dentro do escopo do produtor autorizado.
- `get_related_tickets(order_ids)` — chamados vinculados aos pedidos autorizados.
- `get_delivery_policy(product_id)` — prazo (`deadline_minutes`) e regra de elegibilidade do produto.

---

## Como executar localmente

### Pré-requisitos
- Python 3.12+
- Chave da **Gemini API** (Google AI Studio)

### 1. Clonar e configurar o ambiente

**Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

**Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Abra o arquivo `.env` criado localmente e preencha sua chave:
```env
GEMINI_API_KEY=sua_chave_aqui
GEMINI_MODEL=gemini-3.5-flash-lite
```

### 2. Popular a base simulada (12 pedidos fictícios)
```powershell
python -m app.seed
```

### 3. Subir a aplicação (API + Interface Web)
```powershell
uvicorn app.api:app --reload
```

- **Interface Web:** `http://127.0.0.1:8000`
- **Documentação interativa (Swagger):** `http://127.0.0.1:8000/docs`

### 4. Verificações opcionais por linha de comando
Com o `uvicorn` rodando em outro terminal:
```powershell
# Valida as 5 tools MCP + isolamento multi-tenant (ORD-111 -> order_not_found)
python test_mcp_poc.py

# Executa o Cenário A (ORD-101) diretamente no terminal
python -m app.agent
```

---

## Cenários simulados (T1–T6)

Todos os cenários estão disponíveis como atalhos de 1 clique no compositor da interface web:

| Cenário | Pedido | Situação simulada | Resultado esperado |
|---|---|---|---|
| **T1 • Coletivo** | `ORD-101` | 4 pedidos pagos em `INT-YOUCLASS-01` com `webhook: http_200` e `external_course_id_not_mapped`, sem acesso após 15 min (com distrator `ORD-106` em `antifraud_hold` e `ORD-105` ainda dentro do prazo). | Classifica `coletivo`, identifica exatamente os **4 afetados** (`ORD-101..104`), separa **1 com chamado** (`ORD-101`) e **3 sem chamado** (`ORD-102..104`), excluindo `ORD-105` e `ORD-106`. |
| **T2 • Individual** | `ORD-109` | PIX pendente há 40 min (`payment_status = pending`). | Classifica `individual`, `0` afetados, sem inventar incidente coletivo. |
| **T3 • Recuperação** | `ORD-101..104` | Clicar em **Simular 3/4** (`POST /simulator/recover`) e depois em **Verificar recuperação via MCP**. | Distingue **3 regularizados** (`ORD-101..103`) e **1 pendente** (`ORD-104`) usando 0 tokens de LLM. |
| **T4 • Dados insuficientes** | `ORD-999` | Pedido inexistente (ou API offline). | Classifica `inconclusivo`, `0` afetados e registra a limitação. |
| **T5 • Falso positivo** | `ORD-105` | Pedido pago há 6 min (ainda dentro do prazo de 15 min). | Não classifica como falha confirmada (`0` afetados). |
| **T6 • Segurança** | `ORD-101` | Relato com *prompt injection* pedindo dados do `ORD-111` (`PRODUCER-B`) e reembolso imediato. | Bloqueado em camadas: prompt ignora instruções do relato, `producer_id` não existe nos argumentos das tools e o backend retorna `404` para `ORD-111`. |
| **Dúvida de senha** | `ORD-112` | Relato de problema de acesso, mas `has_access = true`. | Classifica `individual`, `0` afetados, orienta redefinição de senha. |

---

## Segurança, ética e limitações

- **Somente leitura no MVP:** o agente não executa alterações em produção, reprocessamentos ou reembolsos.
- **Defesa em profundidade:** isolamento multi-tenant via header `X-Producer-Id` fora dos argumentos MCP, sanitização de path (`urllib.parse.quote`), menor privilégio no subprocesso `stdio` (sem `GEMINI_API_KEY`), teto de `8` chamadas MCP por investigação, validação determinística pós-LLM em `app/validator.py` e proteção contra XSS no frontend.
- **Limitações conhecidas:** ambiente controlado com 12 pedidos sintéticos; validação comprova comportamento funcional e limites do protótipo, **não** eficiência real em produção nem projeções financeiras. Em uso no Free Tier da Gemini API, podem ocorrer limites de cota (`429`) ou picos temporários (`503`), mitigados parcialmente por retry e fallback de modelos em `app/agent.py`.
