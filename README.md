# PolyTrader

**Bot autônomo de copy-trading + engine de arbitragem matemática para Polymarket.** Identifica carteiras lucrativas no leaderboard, monitora suas operações em tempo real e replica posições. Em paralelo, varre todos os mercados ativos buscando ineficiências `YES + NO < $1` e captura o spread via `mergePositions` no CTF — lucro garantido sem dependência de alpha de baleias.

Python 3.12+ · 100% async · **203 testes** · ruff limpo · SQLite com WAL · `AsyncWeb3` nativo · stack leve (~80 deps)

**HFT Mode** (opcional via config): consumidor concorrente de signals, Optimistic Execution sem pre-flight REST, sizing proporcional ao patrimônio da baleia, rollback atômico, **DB INSERT fora do hot path**, **`post_order` 100% async via httpx + HMAC inline**, **WebSocket reconect agressivo + zombie detection**, **`GammaAPIClient` 3-tier cache (RAM 50ns → SQLite 500µs → REST 100ms)**, **`TradeEvent` dataclass com slots**, **drift correction via `currentSize` (full-exit override quando whale zera)**, **VWAP scanner (elimina falso-positivo Top of Book)**, **CTF `AsyncWeb3` nativo (zero `run_in_executor`) + EIP-1559 dinâmico via Polygon Gas Station**.

> ⚠️ **Aviso.** Copy-trading de mercados de predição envolve risco real de perda de capital. Este projeto é infraestrutura — não um conselho financeiro. Opere em `paper` por 7-14 dias antes de `live`, e mesmo em live comece com 10% do capital-alvo. Leia a seção [Segurança e Modos de Operação](#segurança-e-modos-de-operação).

---

## Sumário

1. [O que o bot faz](#o-que-o-bot-faz)
2. [Arquitetura](#arquitetura)
3. [Regime de operação](#regime-de-operação)
4. [As 3 leis de micro-estrutura do mercado](#as-3-leis-de-micro-estrutura-do-mercado)
5. [As 4 diretivas HFT](#as-4-diretivas-hft)
6. [Engine de arbitragem (Track A)](#engine-de-arbitragem-track-a)
7. [Hardening do copy-trader (Track B)](#hardening-do-copy-trader-track-b)
8. [Performance](#performance)
9. [Infraestrutura](#infraestrutura)
10. [Instalação](#instalação)
11. [Configuração](#configuração)
12. [Go-live](#go-live)
13. [Segurança e modos de operação](#segurança-e-modos-de-operação)
14. [Estrutura do projeto](#estrutura-do-projeto)
15. [Testes](#testes)
16. [Comandos úteis](#comandos-úteis)
17. [Troubleshooting](#troubleshooting)
18. [Licença e disclaimer](#licença-e-disclaimer)

---

## O que o bot faz

O PolyTrader opera em três atividades contínuas e paralelas:

### 1. **Scanner** — descobre quem copiar
A cada hora (configurável), puxa o **leaderboard** da Polymarket Data API para múltiplos períodos (7d, 30d), pontua cada carteira com um score composto (`pnl`, `win_rate`, `consistência`, `recência`, `diversificação de mercados`, `% de trades em mercados curtos`) e mantém um **pool ranqueado das top-N carteiras**. Carteiras que caem de desempenho ou se tornam inativas são automaticamente substituídas. Carteiras com padrão de **wash-trading** (volume alto, PnL ínfimo — típico de *airdrop farmers* na Polygon) recebem `score=0` e nunca entram no pool.

### 2. **Tracker** — vigia os trades delas
Mantém uma conexão WebSocket persistente ao RTDS da Polymarket (`wss://ws-live-data.polymarket.com`) escutando o tópico `activity/trades`. Cada pacote é filtrado por um `Set[str]` das carteiras rastreadas em **nanossegundos**: pacotes de carteiras desconhecidas são descartados antes de qualquer processamento. Quando detecta um trade de alguém do pool, cria um `TradeSignal` e o coloca numa `asyncio.Queue`.

### 3. **Executor** — replica as posições
Consome a fila de sinais e, para cada um:
- Aplica o **checklist do Risk Manager** (score mínimo, banda de preço, max posições, daily loss, drawdown, cap do portfólio).
- Verifica a **Regra 1 (Anti-Slippage Anchoring)** lendo o order book do CLOB.
- Para SELL, valida a **Regra 2 (Exit Syncing)** contra o cache de posições próprias em RAM.
- Quantiza preço ao `tick_size` do ativo (**Diretiva 1**) e roteia pelo exchange correto se o mercado for `neg_risk` (**Diretiva 4**).
- Envia ordem GTC com offset de preço para compensar os ~100ms de RTT NY→London. Se não preenche em 30s, faz fallback para FOK.
- Atualiza `bot_positions` e o cache RAM; notifica via Telegram.

### 4. **Notifier** — te avisa do que importa
Telegram fire-and-forget: trades executados, skips com motivo, alertas de risco/latência/geoblock, resumo diário às 22:00 UTC.

---

## Arquitetura

```
┌───────────────────────────────────────────────────────────────────┐
│                            POLYTRADER                              │
│                                                                    │
│  ┌─────────────┐     ┌─────────────┐     ┌──────────────────┐    │
│  │   SCANNER   │     │   TRACKER   │     │    EXECUTOR      │    │
│  │             │     │             │     │                  │    │
│  │ Leaderboard │     │ WS RTDS     │     │ Risk Manager     │    │
│  │ Profiler    │     │ (filtro Set)│ ──> │ Slippage Anchor  │    │
│  │ Scorer      │ ──> │ Dedup       │     │ Order Manager    │    │
│  │ WalletPool  │     │ SignalQueue │     │ Position Manager │    │
│  └─────────────┘     └─────────────┘     └──────────────────┘    │
│        │                    │                     │              │
│        ▼                    ▼                     ▼              │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │         INFRASTRUCTURE (shared async resources)          │    │
│  │  httpx AsyncClient (pooled)  │  aiosqlite conn (WAL)     │    │
│  │  InMemoryState (RAM cache)   │  BalanceCache (15s bg)    │    │
│  │  structlog JSON              │  Gamma cache (5min TTL)   │    │
│  │  HeartbeatWatchdog           │  orjson (zero-copy parse) │    │
│  └─────────────────────────────────────────────────────────┘    │
│        │                                          │              │
│        ▼                                          ▼              │
│  ┌─────────────┐                         ┌───────────────┐      │
│  │  NOTIFIER   │                         │   DASHBOARD   │      │
│  │  (Telegram) │                         │   (FastAPI)   │      │
│  └─────────────┘                         └───────────────┘      │
└───────────────────────────────────────────────────────────────────┘
```

### Fluxo por sinal (caminho crítico)

```
Trade na Polymarket
    │
    ▼
RTDS WebSocket  ──────────  orjson parse (Regra 4)
    │
    ▼ filtro Set[str] (maker ∈ tracked_wallets?)
    │
    ▼ (dropa em ns se não)
TradeMonitor.dedup
    │
    ▼
detect_signal (~78 μs mediana)
    ├─ filtro de idade
    ├─ filtro de tamanho mínimo USD
    ├─ gamma.get_market (5min cache)
    ├─ filtro de duração (>48h = HARD BLOCK)
    └─ Regra 2: se SELL, checa state cache RAM
    │
    ▼ TradeSignal → asyncio.Queue
CopyEngine.handle_signal
    ├─ RiskManager.evaluate (checklist 10 itens)
    ├─ Regra 1: check_slippage_or_abort (best_ask > whale*1.03?)
    ├─ build_draft (Diretiva 1 quantiza + Diretiva 4 neg_risk)
    ├─ CLOB.post_order (Diretiva 2 retry 425)
    └─ apply_fill → write-through bot_positions + state RAM
    │
    ▼
TelegramNotifier.notify_trade
```

---

## Regime de operação

### Mercados permitidos

| Filtro | Valor padrão | Motivo |
|---|---|---|
| Tempo até resolução **máximo** | 48h (hard block em 3 dias) | Capital travado em evento de semanas/meses = inaceitável |
| Tempo até resolução **mínimo** | 6min | Mercados fechando não preenchem ordens |
| Faixa preferencial | 1h – 24h | Sweet spot: liquidez boa, volatilidade previsível |
| Resolução indefinida | **REJEITAR** | Sem end_date = risco ilimitado |

**Exemplos:**
| Mercado | Ação |
|---|---|
| "Hawks vs Celtics tonight?" (4h) | ✅ COPIAR |
| "Fed rate decision tomorrow?" (22h) | ✅ COPIAR |
| "Bitcoin $110k by Friday?" (48h) | ✅ COPIAR (limite) |
| "Trump impeached by July?" (2160h) | 🚫 HARD BLOCK |
| "Will it rain in NYC tomorrow?" (~3min) | 🚫 TOO CLOSE |

### Carteiras elegíveis para cópia

Gates todos obrigatórios antes do score compor:
- PnL no período ≥ **$500** (configurável)
- Win rate ≥ **55%**
- Mínimo **10 trades** no período
- Mínimo **2 mercados distintos** (1 mercado só = possível insider)
- **≥ 50% dos trades em mercados curtos** (< 48h)
- `|pnl| / volume ≥ 0.05` — **filtro anti-wash-trading** (Regra 3)

### Sizing de ordem

Três modos configuráveis:
- `fixed`: sempre N USD por ordem
- `proportional`: `portfolio_value × proportional_factor` (padrão: 5%)
- `kelly`: Kelly simplificado usando `wallet_score` como proxy de edge

Sempre respeitando `max_position_usd` e `max_portfolio_usd`.

---

## As 3 leis de micro-estrutura do mercado

Estas regras não são dicas — são **invariantes de sobrevivência** num CLOB de baixa liquidez. Violá-las é como ligar um trader HFT sem *circuit breakers*.

### Lei 1 — Anti-Slippage Anchoring
**Problema:** quando a baleia compra, ela seca a liquidez. Em ~100ms (o RTT NY→London), o `best_ask` pode estar 5-10% acima do preço que ela executou. Se o bot copia pelo midpoint atual, vira liquidez de saída do mercado eufórico.

**Solução:** antes de assinar qualquer ordem, ler `/book` do CLOB. Se `best_ask > whale_execution_price × 1.03` (3% configurável), **ABORTAR**. Implementação: [src/executor/slippage.py](src/executor/slippage.py).

### Lei 2 — Exit Syncing (Espelhamento Rígido de Inventário)
**Problema:** se a baleia vende `YES`, copiar o SELL só faz sentido se o bot tem `YES` para vender. Bots ingênuos vendem cegamente e viram *short* em mercado que não permite short, ou lançam ordem que sempre rejeita.

**Solução:** o tracker mantém um **inventário em RAM** das carteiras seguidas (sincronizado via polling de `/positions`) e o executor só aceita SELL se `bot_positions` contém o token. O tamanho da venda é **proporcional** ao % que a baleia vendeu:

```
pct_sold = min(whale_sell_size / whale_prior_size, 1.0)
bot_sell_size = bot_holdings × pct_sold
```

Implementação: [src/core/state.py](src/core/state.py) + [src/tracker/signal_detector.py](src/tracker/signal_detector.py).

### Lei 3 — Detonação de Wash Traders
**Problema:** airdrop farmers na Polygon fazem 10.000 trades "falsos" comprando seus próprios book orders. Aparecem no top do leaderboard com métricas infladas. Copiar um wash trader é garantido prejuízo (eles operam contra si mesmos).

**Solução:** calcular `|PnL| / volume`. Uma carteira que movimenta $100.000 para gerar $500 de lucro tem ratio 0.005 → **não é um high-achiever, é ruído**. Carteiras com ratio < 0.05 recebem `score=0` no [src/scanner/scorer.py](src/scanner/scorer.py).

---

## As 4 diretivas HFT

Complementam as leis acima no nível da infraestrutura de execução.

### Diretiva 1 — Quantização de Preço e Tick Size
A Polymarket rejeita (HTTP 400) preços não alinhados ao `tick_size` do ativo. Arredondar com `round()` de float quebra silenciosamente por erro ULP (`0.1 + 0.2 ≠ 0.3`). Usamos `Decimal` com `ROUND_HALF_EVEN`.
Implementação: [src/core/quantize.py](src/core/quantize.py) + [src/api/order_builder.py](src/api/order_builder.py).

### Diretiva 2 — Exponential Backoff para HTTP 425
O matching engine da Polymarket faz restart rápido periodicamente e devolve `425 Too Early`. Retry determinístico: **1.5s → 3.0s → 6.0s**, no máximo 4 tentativas, só para 425 (outros 4xx propagam). Decorator em [src/api/retry.py](src/api/retry.py).

### Diretiva 3 — Heartbeat Watchdog (Cancel-on-Disconnect)
Se o WS L2 CLOB ficar silente, a Polymarket **cancela todas as ordens limit abertas** automaticamente. Um watchdog async dispara `postHeartbeat` a cada 10s; após N falhas consecutivas, força reconexão do stream.
Implementação: [src/api/heartbeat.py](src/api/heartbeat.py).

### Diretiva 4 — Roteamento Negative Risk
Mercados multi-outcome (ex: "Quem vence a eleição?" com 5+ candidatos) usam o exchange `neg_risk` da Polymarket, que aplica netting de posições complementares (Σ preços = 1.0). Ordens nesses mercados precisam do adapter correto com header apropriado. O `CLOBClient._pick_signer()` roteia automaticamente com base no flag `neg_risk` persistido em `market_metadata_cache`.
Implementação: [src/api/clob_client.py](src/api/clob_client.py) + [src/api/auth.py](src/api/auth.py).

Bonus — **filtragem orjson + Set**: o RTDS emite até 3k msgs/s em picos. `json` nativo entope CPU; `orjson` (Rust) é ~3× mais rápido. Antes de propagar, checamos se `maker ∈ tracked_wallets` (`Set` O(1)) — 99% dos pacotes são dropados em nanossegundos.

---

## Engine de arbitragem (Track A)

Copy-trading é uma estratégia direcional: depende da baleia ter alpha. Arbitragem é **matemática livre de risco direcional** — o lucro vem de uma identidade contratual, não de previsão.

### O edge: `YES + NO < $1`

Cada mercado binário da Polymarket é representado por dois ConditionalTokens (CTF) complementares: `YES` e `NO`. Por construção, **redimir 1 unidade de YES + 1 unidade de NO devolve $1 USDC** ao caller, via `ConditionalTokens.mergePositions`.

Logo, se `ask_yes + ask_no < 1.0`, comprar tamanho `N` em ambos os lados e dar merge é **lucro garantido**:

```
profit = N × (1 - ask_yes - ask_no - 2×fee)
```

A engine de arb roda **em paralelo** ao copy-trader, com banca isolada (`max_capital_usd`), risk profile próprio e zero acoplamento com o pipeline de sinais das baleias.

### Pipeline

```
┌────────────────────────────────────────────────────────────────────┐
│                       ARBITRAGE ENGINE                              │
│                                                                     │
│  ┌──────────────┐      ┌──────────────┐      ┌──────────────────┐ │
│  │   SCANNER    │      │   EXECUTOR   │      │   CTF CLIENT     │ │
│  │              │      │              │      │                  │ │
│  │ Gamma list   │      │ FOK buy YES  │      │ mergePositions   │ │
│  │ /book × 2    │ ───> │ FOK buy NO   │ ───> │ on Polygon       │ │
│  │ depth check  │      │ (parallel)   │      │ (web3 EIP-1559)  │ │
│  │ edge filter  │      │ rollback     │      │ → $1 / par       │ │
│  └──────────────┘      └──────────────┘      └──────────────────┘ │
│         │                      │                       │           │
│         ▼                      ▼                       ▼           │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  arb_opportunities  │  arb_executions  │  arb_bank_snap  │    │
│  └──────────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────────────┘
```

### Filtros aplicados pelo scanner

Para cada mercado binário ativo:

1. **Janela de duração:** `min_minutes_to_resolution ≤ TTL ≤ max_hours_to_resolution`. Mercados muito longos travam capital; muito curtos não fillam antes do close.
2. **Edge mínimo líquido:** `1 - ask_yes - ask_no - 2×fee_per_leg - safety_buffer_pct ≥ min_edge_pct`. Default conservador: 0.5% paper, 1% live.
3. **Profundidade de book:** soma de `price × size` em até 5 níveis em cada side ≥ `min_book_depth_usd`. Sem isso, 1 fill move o preço contra você.
4. **Cool-down:** mesmo `condition_id` não é re-emitido em menos de `same_market_cooldown_seconds` — evita repostagem enquanto executor processa.

### Atomicidade de execução

Postar 1 ordem CLOB e ficar esperando a outra é exposure direcional não-coberta. A engine posta **as duas legs como FOK em paralelo** via `asyncio.gather`. Resultados:

| YES | NO | Ação |
|---|---|---|
| filled | filled | merge no CTF (se `auto_merge=true`) → realiza lucro |
| filled | failed | **rollback**: vende a leg fillada via FOK no melhor bid |
| failed | filled | **rollback** simétrico |
| failed | failed | nenhuma exposure → nada a fazer |

### Modos da engine

| Mode | Comportamento |
|---|---|
| `paper` | Simula fills perfeitos no `ask`, calcula PnL teórico, persiste em `arb_executions`. Não chama CLOB nem on-chain. |
| `dry-run` | Detecta + grava + emite Telegram, sem postar nem simular. |
| `live` | Posta FOK reais no CLOB; chama `mergePositions` no CTF Polygon (EIP-1559); banca real. |

Todas as oportunidades, executions e snapshots de banca são gravados em três tabelas dedicadas: `arb_opportunities`, `arb_executions`, `arb_bank_snapshots`. PnL da arb é computado isoladamente do PnL do copy-trader.

### Limitações conhecidas

- **Neg-risk markets** (multi-outcome, Σ outcomes = 1 forçado pelo `NegRiskAdapter`) são skipados. Suporte requer adapter web3 separado — fase 2.
- **Auto-rollback** está como skeleton (marca `rolled_back`); a venda automática da leg pendurada será adicionada quando a primeira run live confirmar o pipeline.
- **Multi-outcome arbitrage** (sum de N outcomes < $1) também é fase 2 — flag `enable_multi_outcome` reservado.

Implementação: [src/arbitrage/](src/arbitrage/) (~600 LOC, 7 testes dedicados).

---

## Hardening do copy-trader (Track B)

Duas adições no pipeline de execução do copy-trader que aproximam o comportamento de uma mesa profissional:

### Maker pricing (post-only behavior via GTC)

Polymarket CLOB cobra fee por taker. Postar GTC com preço **dentro do spread** (não cruzando) torna a ordem maker — captura o spread em vez de pagá-lo. Implementado em [src/api/clob_client.py](src/api/clob_client.py) via `maker_price()`:

```python
# BUY: best_bid + offset_ticks × tick_size, mas nunca ≥ best_ask
# SELL: best_ask - offset_ticks × tick_size, mas nunca ≤ best_bid
```

Quando o preço maker cairia fora do spread (book muito apertado), o método retorna o nível seguro mais próximo sem cruzar. Caller controla quanto agressivo ser via `offset_ticks`.

### Depth-aware sizing

Anchor da Lei 1 valida slippage **versus o preço da baleia**. Mas isso não impede postar uma ordem $50 num book que tem $5 disponíveis — a ordem fica pendurada e o spread anda contra. [src/executor/depth_sizing.py](src/executor/depth_sizing.py) introduz uma simulação VWAP que percorre o book e calcula:

- `fillable_size_usd` — quanto USD cabe respeitando `max_impact_pct`
- `vwap_price` — preço médio efetivo do fill simulado
- `levels_consumed` — quantos níveis encheriam
- `impact_pct` — `(vwap - best) / best`

Quando habilitado em config, o copy-engine deve cortar o tamanho do trade pelo menor entre `whale_proportional_size` e `fillable_size_usd`. Isso bloqueia a ordem quando o book é mais raso do que o sizing teórico pediu.

```yaml
depth_sizing:
  enabled: true                # live: sempre on
  max_impact_pct: 0.015        # 1.5% impact máximo aceitável
  max_levels: 5                # quantos níveis percorrer
```

7 testes dedicados em [tests/test_depth_sizing.py](tests/test_depth_sizing.py) validam BUY/SELL, caps por impact, capacidade per-level e books vazios.

### HFT Mode — concorrência, optimistic execution, perfect-mirror real

Quatro mudanças cirúrgicas que aproximam o copy-engine de uma mesa profissional. Todas opcionais via config — defaults preservam comportamento conservador.

**1. Consumidor concorrente da fila de signals.** O `run_loop` antigo era estritamente sequencial — `await self.handle_signal(signal)` por sinal. Em rajadas (whale dispara 10 trades em 2s), a 9ª esperava as 8 anteriores fillarem. Refatorado para `asyncio.create_task` por sinal, limitado por `asyncio.Semaphore(max_concurrent_signals)`. Default 4 → ~40 ord/s com RTT 100ms NY→London, longe do rate limit Polymarket. Um `asyncio.Lock` guarda a seção de **decisioning** (risk gates + cash availability + market cap) para evitar over-subscription quando dois signals leem `cash_available` simultaneamente. O lock é fino: cobre só leitura+decisão; o `post_order` roda fora dele.

```yaml
executor:
  max_concurrent_signals: 4   # 4 ordens em flight, paralelo seguro
```

**2. Optimistic Execution (sem pre-flight REST).** O modo defensivo (`check_slippage_or_abort`) baixa `/book` antes de cada ordem para validar `best_ask ≤ whale × (1 + tolerance)`. Em NY→London isso custa ~80-130ms extra por trade. O modo otimista (`compute_optimistic_ref_price` em [src/executor/slippage.py](src/executor/slippage.py)) é uma função pura: embute a tolerance no `limit_price` e dispara FOK direto. O CLOB rejeita on-exchange se o livro andou — economizamos o round-trip e a decisão de match acontece no matching engine.

```yaml
executor:
  optimistic_execution: true   # FOK direto, sem /book pre-flight
```

Trade-off explícito: paga-se em rejeições FOK (que contam como `POST_FAIL`, não como exposure). Para cargas direcionais agressivas, o ganho de latência supera as rejeições; o operador vê isso em `trades_skipped{reason_class="POST_FAIL"}` versus a redução de `signal_to_fill_seconds`.

**3. Perfect-Mirror Sizing real.** O `paper_perfect_mirror` antigo usava `target = starting_bank × proportional_factor` — alocação cega de 10% da banca, ignorando o que a whale fez. A refactor lê `signal.whale_portfolio_usd` (vindo do `enrich`) e calcula `whale_pct = signal.usd_value / whale_portfolio_usd`. Em seguida aplica essa **mesma percentagem exata** à banca ativa do bot, multiplicada por `whale_sizing_factor` para amplificar/atenuar convicção. Quando `whale_portfolio_usd` está ausente, cai no fallback antigo. Loga `perfect_mirror_sized` com `whale_pct` e `sized` para auditoria.

**4. Rollback atômico em `arbitrage/executor.py`.** Se a leg YES filla via FOK e a NO falha (livro andou em ms), o bot lia best_bid via `clob.book(stuck_token_id)`, quantizava com `tick_size + neg_risk` corretos via `MarketSpec`, e dispara **FOK SELL** no best_bid. O FOK garante atomicidade: parcial deixaria resíduo direcional, exatamente o problema. Quando falha (book sem bid ou FOK rejeitado on-exchange), persiste como `rolled_back` com erro detalhado e dispara alerta Telegram para o operador. PnL realizado = `-(spread × size)` — dano contido em vez de exposure aberta.

Cobertura: 9 testes novos em [tests/test_optimistic_execution.py](tests/test_optimistic_execution.py) + [tests/test_concurrent_engine.py](tests/test_concurrent_engine.py).

### Hot Path Optimization — eliminação de I/O síncrono

Três mudanças que removem I/O síncrono do caminho crítico signal→fill:

**1. SQLite INSERT fora do hot path.** `build_draft` antes fazia `INSERT INTO copy_trades` antes de `clob.post_order`. WAL é rápido (~1-3ms) mas em batches de 10 signals isso vira 10-30ms de latência composta antes da primeira ordem ir pro CLOB. Refactor: `build_draft` agora é puro RAM (constrói `OrderDraft` + `CopyTrade` em memória, retorna em sub-millisecond). O caller (`copy_engine`) chama `clob.post_order` PRIMEIRO, depois despacha `asyncio.create_task(persist_trade_async(trade))` paralelo a `apply_fill`. Disco sai do caminho crítico. Ver [src/executor/order_manager.py](src/executor/order_manager.py).

**2. `post_order` 100% async via httpx — `run_in_executor` removido.** A SDK `py-clob-client` tem API síncrona (`requests` blocking). O fluxo antigo despachava signing+POST inteiro para `loop.run_in_executor` — thread context switch + GIL contention + `requests` blocking ~100ms NY→London. Em batches HFT, threads se acumulavam. Novo fluxo:

```
build_signed_order (CPU 5ms inline)  →  json.dumps canonical  →
build_l2_headers (HMAC, ~1µs)  →  httpx.AsyncClient.post (HTTP/2 keep-alive)
```

O signing fica inline (5ms é aceitável; trade-off vs 100ms RTT que economizamos). O HMAC L2 é construído pelo nosso [src/api/clob_l2_auth.py](src/api/clob_l2_auth.py), implementação 1:1 com a SDK oficial (testes de paridade no `test_hft_hot_path.py`). O POST vai pelo singleton `httpx.AsyncClient` com HTTP/2 multiplexado — múltiplas ordens em paralelo sobre 1 conexão TCP. Ver [src/api/clob_client.py:107](src/api/clob_client.py#L107).

**3. WebSocket reconect agressivo + zombie detection.** O exp-backoff antigo (até 60s) cega o bot por 1 minuto após qualquer blip de rede — em mercado de predição isso = perder rajada inteira de whale. Refactor:

- **Reconect instantâneo**: jitter constante 50-150ms (sem exponential). Polymarket aceita reconect frequente sem rate-limit.
- **Zombie detection**: `HeartbeatWatchdog` ganhou método `notify_message_received()` que registra timestamp do último frame recebido. Loop secundário verifica a cada 500ms — se silêncio > 3s, dispara `on_failure` e força reconnect. Polymarket emite ~3k msgs/s em pico; silêncio prolongado = TCP-zombie (conexão aceita keep-alive mas o stream parou). Sem essa detecção, o bot esperava o ping timeout de 10s+ cego.

Ver [src/api/heartbeat.py](src/api/heartbeat.py) e [src/api/websocket_client.py](src/api/websocket_client.py).

Cobertura: 10 testes novos em [tests/test_hft_hot_path.py](tests/test_hft_hot_path.py) — paridade HMAC com SDK oficial, build_draft sem disk I/O, `persist_trade_async` idempotente, silence detection trigger/reset/disabled.

### Camada de parsing & estado — `dict.get` → slots, SQLite → RAM

Três refatorações que removem custos invisíveis no hot path do `detect_signal` (chamado a cada trade RTDS — pico ~3k msgs/s, dos quais ~1% sobrevive ao filtro Set):

**1. `GammaAPIClient` 3-tier cache.** Antes, `get_market(condition_id)` fazia SELECT no SQLite (~500µs) a cada trade. Agora a pirâmide de latência é:

```
L1 RAM (OrderedDict, ~50ns)  →  L2 SQLite (~500µs)  →  L3 REST Gamma (~100ms)
```

`OrderedDict` permite eviction LRU O(1) via `popitem(last=False)`; cap de 1024 entradas com TTL 300s por entrada (mesmo TTL do SQLite cache). Hits são monotonic-clock-safe, evictar TTL-expired no read. SQLite hit promove pra RAM (próximas leituras viram L1). `_write_cache` mira RAM antes do disco. Métricas `hits/misses/size` expostas via `ram_stats`. Em workload steady (~500 condition_ids ativos), >99% das chamadas viram L1 hits após warm-up.

**2. Drift correction no Whale Inventory.** A Lei 2 antiga calculava `pct_sold = min(size / prior_whale, 1.0)` — vulnerável a `prior_whale` defasado por missed packet RTDS, AMM split/merge ou crash do bot. Refactor:

- Quando o payload contém `currentSize` (saldo da whale APÓS o trade — Polymarket emite em alguns canais), usa **delta exato**: `pct_sold = (prior - current) / prior`. Imune a drift.
- Caso especial **whale zerou** (`current == 0`): `adjusted_size = bot_size` — vendemos TUDO, ignorando o `size` do evento (que pode ser parcial vs evento final consolidado).
- Após cada SELL com payload válido, `state.whale_set(wallet, token, current)` sincroniza nosso RAM com o ground truth do payload — corrige drift cumulativo automaticamente.
- Fallback: se `currentSize` ausente, comportamento legado preservado.

Helper puro `_compute_exit_size` retorna `(adjusted_size, pct_sold, source)` onde `source ∈ {"event_close", "event_delta", "size_proxy"}` — auditável nos logs `exit_sync_resize`.

**3. `TradeEvent` dataclass com slots.** O `detect_signal` antes fazia 10+ `dict.get(...)` em cascata por evento. Refactor: novo módulo [src/core/trade_event.py](src/core/trade_event.py) com `@dataclass(frozen=True, slots=True)` `TradeEvent` + função pura `parse_trade_event(raw)`. UMA única passada extrai todos os campos com fallback chains documentadas (`maker → makerAddress → user`, `currentSize → balanceAfter → postSize → makerBalance`, etc.). Atributos slot lookup ~30% mais rápido que `dict.get`; sem alocação de `__dict__`. Bug de canto corrigido: `or` chain falha quando valor é `0` falsy (justamente o caso crítico `currentSize=0`) — substituído por cascata `is None` explícita.

`detect_signal` aceita parâmetro opcional `parsed: TradeEvent` para callers já-parsed (futuro: pre-parse no `websocket_client` antes de empurrar pra fila — economia adicional). Callers existentes seguem passando `dict` sem mudança.

Cobertura: 20 testes novos em [tests/test_trade_event.py](tests/test_trade_event.py), [tests/test_exit_sync_drift.py](tests/test_exit_sync_drift.py), [tests/test_gamma_ram_cache.py](tests/test_gamma_ram_cache.py) — parser paridade RTDS+API, 3 caminhos de `_compute_exit_size`, full-exit override quando whale zera, drift correction integration, LRU eviction, TTL drop, SQLite→RAM promotion.

### Quant + MEV — VWAP edge + AsyncWeb3 nativo + gas dinâmico

Última milha. Duas correções mergeantes para o motor de execução on-chain:

**1. VWAP scanner (correção matemática crítica).** O `_depth_usd` antigo retornava `(best_price, depth_total)` e o scanner usava `best_price` como input do edge: `edge = 1 - (best_yes + best_no)`. Isso é **falso-positivo manufaturado**: se o L1 do book tem $5 e queremos $500, vamos consumir L2/L3 a preços piores → execução real diverge do edge estimado, FOK reverte ou capital fica preso.

A nova função pura `_walk_vwap(book, side, target_tokens, max_levels)` caminha o livro nível-a-nível consumindo liquidez até preencher o `target_tokens`. Retorna o **VWAP real** do path:

```
VWAP = Σ(price_i × tokens_taken_i) / Σ(tokens_taken_i)
```

`_evaluate_market` agora faz **2-pass**:
1. **Depth survey** (`target=∞`): descobre `depth_yes_tokens` / `depth_no_tokens` totais
2. **Resolve**: `effective_target = min(max_per_op_usd / best_l1, depth_yes, depth_no) × 0.5`
3. Re-walk com `effective_target` → `vwap_yes`, `vwap_no` realistas
4. `edge_gross = 1 - (vwap_yes + vwap_no)` ← VWAP, **não** best_ask

`ArbOpportunity.ask_yes`/`ask_no` agora carregam VWAP (semântica explícita: "preço efetivo de execução do tamanho recomendado"). Mantém compat de schema.

Cenário canônico do bug: L1 30¢ + 30¢ = aparente 40% edge. Mas L1 só tem 10 tokens; pra 100 tokens consumimos 10@0.30 + 90@0.45 = VWAP 0.435 → edge real **13%**. Antes da correção, scanner emitiria. Agora bloqueia.

**2. CTF `AsyncWeb3` nativo + EIP-1559 dinâmico.** Dois bugs HFT:

a) `loop.run_in_executor(None, self._merge_sync, ...)` prendia uma thread do default `ThreadPoolExecutor` por até 60s no `wait_for_transaction_receipt`. Default tem ~32 threads — em rajada de arb, esgota.

b) `max_priority = 30 gwei` hardcoded. Em volatilidade (peak MEV), median priority na Polygon ultrapassa 50-100 gwei → tx cai do bloco / sofre MEV-sandwich.

Refactor:
- `Web3(HTTPProvider)` → `AsyncWeb3(AsyncHTTPProvider)` no `_ensure_initialized` (lazy init thread-safe via `asyncio.Lock`)
- Merge/redeem viram corotinas puras; todas as chamadas RPC são `await w3.eth.*`. `wait_for_transaction_receipt` da `AsyncWeb3` usa `asyncio.sleep` interno — não prende thread alguma
- Signing continua inline (`Account.sign_transaction` é CPU puro keccak+ECDSA, ~1ms)
- Novo módulo [src/arbitrage/gas_oracle.py](src/arbitrage/gas_oracle.py) com cascata:
  - **L1**: Polygon Gas Station v2 (`fast.maxPriorityFee` em gwei)
  - **L2**: `eth_feeHistory` percentil 99 dos últimos 20 blocos (mediana inter-blocos)
  - **L3**: hardcoded 30 gwei (preserva default histórico)
  - Cache TTL 5s + cap defensivo de 500 gwei contra outliers
- `maxFeePerGas = baseFee × 2 + maxPriorityFee` (2× baseFee como buffer contra spike entre estimação e inclusão)

Cobertura: 16 testes novos em [tests/test_arbitrage_vwap.py](tests/test_arbitrage_vwap.py) + [tests/test_gas_oracle.py](tests/test_gas_oracle.py) — VWAP math (5 cenários), false-positive elimination, scanner integration, gas oracle cascade (gas station / fee_history / hardcoded / cap / cache TTL / object-attr access).

### Risk Manager — SELL bypass + Kelly puro + anti-fragmentação

Três falhas perigosas no `RiskManager` corrigidas (vulnerabilidades de fundos quantitativos clássicas):

**1. Circuit Breaker SELL Bypass.** O `evaluate` antigo retornava `False` para todos os signals quando `_halted=True`. Em mercado caindo, a whale dispara SELL para estancar — bot rejeitava por estar "halted" e afundava com a posição. Refactor:

- Halt SELL bypass: `if self._halted and signal.side == "BUY": reject; else (SELL): pass`
- `daily_loss` / `drawdown` triggers ainda chamam `self.halt(...)` (mantém estado halted contra próximos BUYs), mas SELL no mesmo evento prossegue para o sizing pipeline
- `MAX_POSITIONS` e `PORTFOLIO_CAP` são BUY-only (SELL libera capital, não consome)
- `LOW_SCORE` e `PRICE_BAND` continuam validando ambos (proteção contra venda em mercados zerados)
- Logs `halted_buy_blocked` (bloqueado) vs `halted_sell_bypass` (executado) para auditoria

**2. Kelly Criterion com `win_rate` puro.** A fórmula `f* = p − q/odds` exige que `p` seja a probabilidade real de vitória. O código antigo usava `signal.wallet_score` — uma média ponderada de PnL+recência+diversidade+win_rate — distorcendo violentamente o sizing. Cenário canônico: whale com score composto 0.95 (top performer multi-fator) mas win_rate real 0.55 → antes do fix, Kelly calculava f*=0.90 (ALL-IN capped 25%); agora calcula f*=0.10 (realista). Refactor:

- `TradeSignal.whale_win_rate: float | None` adicionado, propagado paralelo a `wallet_score`
- Novo dict `wallet_win_rates: dict[str, float]` mantido pelo Scanner (mesmo padrão arquitetural de `wallet_scores`/`wallet_portfolios`)
- `_size_usd` Kelly: `win = signal.whale_win_rate if not None else signal.wallet_score` (fallback explícito + warning log para auditoria)
- Clamp defensivo: `win ∈ [0.01, 0.99]` (evita divisão por zero em odds extremas)

**3. Anti-fragmentação no `whale_proportional`.** CLOBs particionam ordens grandes em múltiplos fills (ordem $100k = 10× $10k). O cálculo antigo `conviction = signal.usd_value / whale_bank` calculava 10× uma convicção pequena → bot fazia 10 entries picadas. Refactor:

- `TradeSignal.whale_total_position_usd` carrega o **inventário total da whale no token APÓS o fill** (em USD), não o size do fill atomic
- `signal_detector.detect_signal` popula via `evt.current_whale_size × price` quando RTDS emite `currentSize`; fallback `None` quando ausente
- `_size_usd` whale_proportional: `whale_position_usd = signal.whale_total_position_usd or signal.usd_value` → `conviction = whale_position_usd / whale_bank`
- Bot agora "cresce" a posição conforme a whale completa a ordem (3º fill = 30% acumulado → 15 USD; 10º fill = posição final → 50 USD na nossa banca proporcional), em vez de 10× $5

Cobertura: 14 testes novos em [tests/test_risk_refactor.py](tests/test_risk_refactor.py) — SELL bypass em daily_loss/drawdown, BUY ainda bloqueado, MAX_POSITIONS/PORTFOLIO_CAP só pra BUY, LOW_SCORE+PRICE_BAND ainda validam SELL, Kelly com win_rate puro vs fallback, distortion regression demonstrada matematicamente, total_position vs fill_size, fragmentação corrigida em 10× fills, cap de outlier 0.25.

### Arquitetura — fund-lock fix + batch + background rate limit

Três correções de **infraestrutura crítica** que impactam fluxo de caixa real e estabilidade de IP:

**1. 🚨 FUND-LOCK FIX — redeem on-chain após resolução.** Bug fatal: quando um mercado resolvia a nosso favor (`won=True`), o bot atualizava o DB e **parava**. Mas Polymarket usa ConditionalTokens (CTF) — tokens vencedores valem 1 USDC face value mas **NÃO transferem automaticamente**. É preciso chamar `ConditionalTokens.redeemPositions(USDC, parentId, conditionId, indexSets)` on-chain. Sem isso, o capital fica preso para sempre no contrato CTF.

Fix:
- Migration `005_redeem_tx_hash.sql` adiciona coluna `bot_positions.redeem_tx_hash`
- `resolution_watcher` recebe `CTFClient` e dispara `asyncio.create_task(_dispatch_redeem(...))` quando `won=True`
- **Idempotência**: sentinel `redeem_tx_hash="dispatching"` reservado via UPDATE atomic `WHERE redeem_tx_hash IS NULL` antes de spawnar a task. Dois ticks consecutivos não disparam 2 redeems
- Estados rastreados: `NULL` (não dispatchado), `"dispatching"` (em vôo), `"0x<hex>"` (confirmado), `"paper-skip"` (não-live), `"lost-skip"` (perdedora — gas seria desperdício), `"failed:<msg>"` (retry no próximo tick)
- `indexSet` CTF binary bitmask: outcome 0 → `1`, outcome 1 → `2` (`1 << outcome_idx`)
- Falha persiste com mensagem; tick subsequente re-tenta
- Telegram alerta na primeira falha por position

**2. RATE-LIMIT FIX — batch /markets em vez de N requests.** O loop antigo fazia `gamma.get_market(cid, force_refresh=True)` por position aberta. Em 50 positions × tick de 60s = 50 reqs/min bypassando cache → ban Cloudflare/Polymarket.

Fix:
- Novo `gamma.get_markets_batch(condition_ids: list[str], chunk_size=50)` faz UMA request `/markets?condition_ids=ID1,ID2,...,IDn` por chunk
- Mira RAM + SQLite cache para todos os markets retornados
- Dedup case-insensitive de IDs
- `check_resolutions_once` refatorado: 1 SQL select positions abertas → coleta cids únicos → 1 chamada batch → loop local processa cada position contra dict resolvido → 1 commit DB. Eliminadas N − 1 requests por tick.

**3. ARQUITETURA — `BackgroundRateLimiter` separa hot path de auditoria.** Scanner enriquecendo perfis (22 whales × 3 endpoints = 66 reqs em paralelo) competia pelo MESMO `httpx.AsyncClient` singleton que o post_order do CLOB e a recovery RTDS. Posts de ordem ficavam atrás de fila de auditoria, perdendo ms críticos.

Fix:
- Novo módulo [src/api/background_limiter.py](src/api/background_limiter.py) com singleton `asyncio.Semaphore` (default 5 concurrent)
- API `async with bg.acquire(): ...` com timeout 30s + degradação graceful (timeout não bloqueia call, apenas loga)
- Wraps em `scanner/enrich.py`, `tracker/whale_inventory.py::snapshot_whale`, `executor/resolution_watcher.py` (chamada batch)
- Hot path (CLOB post_order, RTDS, gamma `get_market` do detect_signal) **NÃO** usa o limiter — sai sem fila. Garantia de que auditoria nunca rouba largura do trading
- Configurável via `executor.background_max_concurrent` (default 5)

Cobertura: 19 testes novos em [tests/test_resolution_redeem.py](tests/test_resolution_redeem.py) + [tests/test_background_limiter.py](tests/test_background_limiter.py) — indexSet bitmask para binary + multi-outcome, dispatch fire-and-forget, paper-skip / lost-skip / failed retry, sentinel idempotência, batch single call (regression test contra DDoS interno), Semaphore cap concorrência, FIFO serialização, GC pattern em exception, singleton replacement.

### Risk + Infra — stale cleanup death trap fix + price updater paralelo

Duas falhas que afetam liquidação de posições e estabilidade de IP:

**1. 🚨 Stale Cleanup Death Trap** — `_synthetic_sell_signal` antigo usava `avg_entry_price` como anchor de SELL forçado. Em token que despencou (compramos a 0.50, mercado virou 0.05), Regra 1 calculava `slippage = 90%` e abortava — posição ficava presa eternamente, contaminando `portfolio_cap`, `max_positions`, daily_loss tracking. Fix:

- `TradeSignal.bypass_slippage_check: bool = False` adicionado (default preserve behavior). Sinais reais de copy/arb NUNCA setam isso.
- `_find_stale` passa a SELECT `current_price` da coluna existente (populada pelo `price_updater`).
- `_synthetic_sell_signal` recebe `current_price` e usa como anchor (fallback ao `avg_price` se ausente). Sempre seta `bypass_slippage_check=True`.
- `copy_engine._make_decision` checa o flag ANTES de qualquer caminho de slippage (defensive REST `check_slippage_or_abort` ou optimistic). Se True, pula direto pra `ref_price = signal.price`. Loga `stale_force_sell_bypass_slippage` para auditoria.
- **Spread shield NÃO desligado**: book completamente vazio ainda aborta — só a Regra 1 (anchor whale) é bypassada.

Stale cleanup vira um **stop-loss puro de tempo**: após `max_position_age_hours` (default 48h), a posição é dumpada a qualquer custo de mercado. É a intenção — capital preso é pior que dump em loss conhecida.

**2. ⚙️ Price Updater DDoS sequencial + lock contention** — Loop antigo: 100 positions × `await clob.price()` sequencial (~100ms cada) = **10s blocked**. 100 UPDATEs individuais com lock por write. Fix:

- `asyncio.Semaphore(PRICE_FETCH_CONCURRENCY=10)` + `asyncio.gather` paralelizam fetches → **~1s total** (10× speedup; cap evita rate-limit Polymarket e reserva folga pro hot path)
- Falha individual num token NÃO contamina os demais (`_fetch_one_price` retorna None em exception, gather coleta apenas successes)
- `executemany` com lista de tuples `(current, unrealized, pid)` → **1 SQL statement + 1 commit**, eliminando 99 write locks SQLite
- Hot path `apply_fill` do copy_engine não compete por locks durante a janela

Cobertura: 15 testes novos em [tests/test_stale_cleanup_death_trap.py](tests/test_stale_cleanup_death_trap.py) + [tests/test_price_updater_parallel.py](tests/test_price_updater_parallel.py) — current_price anchor, fallback ao avg, fallback em current zero/negativo, copy_engine bypass integration (mock que falha se `check_slippage_or_abort` for chamado em stale signal), regression check de Regra 1 em sinais normais (bypass=False default), Semaphore cap, paralelização timing (20 fetches × 50ms = ~0.1s, não 1s sequencial), failure isolation, `executemany` UMA chamada não N, sanity zero positions.

---

## Performance

O projeto passou por otimização autônoma dirigida via [autoresearch](https://github.com/karpathy/autoresearch):

| Métrica | Baseline (P2) | Após otimização | Speedup |
|---|---:|---:|---:|
| `detect_signal` mean | 25,654 μs | **78 μs** | **329×** |
| `detect_signal` median | 24,520 μs | **71 μs** | **345×** |
| `rtds_parse_filter` | 45.63 μs | 43.75 μs | estável |
| **Total hot path** | **25,700 μs** | **140 μs** | **184×** |

### A 3k msgs/s (pico RTDS documentado):
- **Antes:** 77s de CPU por segundo de stream → saturação total, impossível operar
- **Depois:** 0.21s de CPU por segundo → ~15% de 1 core, com 6× de folga

### As 5 otimizações que valeram
1. **Conexão SQLite compartilhada** (iter 6, −93%) — uma `aiosqlite.Connection` para a vida do processo, injetada no tracker. Cada abertura custava ~10ms de thread-init no Windows.
2. **Fast-path em RAM** (Fase 4, −82%) — `InMemoryState` com `bot_positions_by_token` + `whale_inventory` em dicts. Exit Syncing lookup vira sub-μs.
3. **Merge de lookups na mesma conn** (iter 1, −52%) — `bot_positions` e `whale_inventory` consultados com a mesma conexão.
4. **Inline de funções + reuso de `now`** (iter 3, −10%) — eliminou 1 `datetime.now()` e 1 function call.
5. **`TradeSignal.model_construct`** (iter 8) — dados vindos do nosso próprio código não precisam de revalidação Pydantic.

Detalhes completos em [autoresearch/260413-2014-polytrader-perf/summary.md](autoresearch/260413-2014-polytrader-perf/summary.md).

---

## Infraestrutura

### Servidor de produção: QuantVPS — New York

| Spec | Valor |
|---|---|
| OS | Ubuntu 24.04 LTS (headless) |
| RAM mínima | 8 GB |
| Storage | NVMe SSD |
| Rede | 1 Gbps (burst 10 Gbps) |
| Uptime SLA | 99.999% |
| DDoS | incluso |

### Latência NY → London (onde roda o CLOB)

O matching engine da Polymarket roda em **AWS eu-west-2 (London)**. RTT típico:

| Rota | RTT |
|---|---|
| NY → London (CLOB/WS) | **70-130 ms** |
| Dublin → London | 0-2 ms |
| London → London | < 1 ms |

**Por que NY funciona mesmo assim?** Não fazemos HFT contra market makers — o edge é **replicar traders lucrativos**, não bater a velocidade do livro. 130ms após o trade da baleia, o preço raramente se moveu demais em mercados de curta duração. Compensamos com *limit orders* GTC com offset de 2% + fallback FOK.

### Geoblock (IPs dos EUA)

A Polymarket **internacional** bloqueia IPs americanos para trading. O bot verifica `https://polymarket.com/api/geoblock` no startup; se bloqueado e `exchange_mode=international`, faz fail-fast. Roteamento externo (VPN/proxy) é responsabilidade do operador.

---

## Instalação

### Pré-requisitos
- Python 3.12+
- [uv](https://astral.sh/uv) (package manager)
- SQLite (já vem no macOS/Linux)
- Conta Polymarket com carteira configurada (MetaMask ou similar)
- USDC.e (bridged) na Polygon, na carteira *funder*
- Bot do Telegram + chat ID (opcional mas recomendado)

### Dev local

```bash
git clone https://github.com/dougsls/polytrader.git
cd polytrader
uv sync                                   # cria .venv + instala deps
uv run python scripts/init_db.py          # aplica schema SQLite
uv run python -m pytest -q                # 46 testes em ~2s
```

### VPS (Ubuntu 24.04)

```bash
git clone https://github.com/dougsls/polytrader.git /opt/polytrader
cd /opt/polytrader
bash scripts/provision_vps.sh             # timezone UTC, deps, uv, UFW, systemd enable
```

Depois do provision:
```bash
sudo nano /opt/polytrader/.env            # preencher PRIVATE_KEY, FUNDER_ADDRESS, TELEGRAM_*
uv run python scripts/geoblock_check.py   # IP ok?
uv run python scripts/latency_test.py     # RTT < 150ms p95?
uv run python scripts/setup_wallet.py     # deriva L2 + confere saldo USDC.e
sudo systemctl start polytrader
sudo journalctl -u polytrader -f          # logs ao vivo
```

---

## Configuração

Duas camadas: `.env` (secrets) + `config.yaml` (comportamento).

### `.env` — ver [.env.example](.env.example)

| Variável | Descrição |
|---|---|
| `PRIVATE_KEY` | Chave privada da carteira de trading. **Nunca committar.** |
| `FUNDER_ADDRESS` | Endereço proxy (se `SIGNATURE_TYPE=1/2`); senão iguala ao address da privkey |
| `SIGNATURE_TYPE` | `0`=EOA, `1`=Email/Magic, `2`=Browser wallet |
| `EXCHANGE_MODE` | `international` (clob.polymarket.com) ou `us` (CFTC, invite-only) |
| `TELEGRAM_BOT_TOKEN` | Token do bot Telegram para alertas |
| `TELEGRAM_CHAT_ID` | Chat ID de destino (negativo para grupos) |
| `LATENCY_ALERT_THRESHOLD_MS` | Alerta se RTT > N ms (padrão 200) |
| `POLYGON_RPC_URL` | RPC Polygon mainnet (default `polygon-rpc.com`) — Track A only |
| `CTF_CONTRACT_ADDRESS` | ConditionalTokens Polygon (default `0x4D97...`) |
| `USDC_CONTRACT_ADDRESS` | USDC.e bridged (default `0x2791...`) |
| `NEG_RISK_ADAPTER_ADDRESS` | NegRisk adapter (reservado, fase 2) |

### `config.yaml` — nós principais

```yaml
scanner:
  max_wallets_tracked: 20
  min_profit_usd: 500
  min_win_rate: 0.55
  wash_trading_filter:
    min_volume_to_pnl_ratio: 0.05       # Regra 3

tracker:
  market_duration_filter:
    max_hours_to_resolution: 48         # mercados <= 48h
    hard_block_days: 3                  # >3 dias = NUNCA

executor:
  mode: "live"                          # live | paper | dry-run
  max_portfolio_usd: 500
  max_position_usd: 100
  max_positions: 15
  max_daily_loss_usd: 100
  max_drawdown_pct: 0.20
  whale_max_slippage_pct: 0.03          # Regra 1 — aborta se best_ask > whale*1.03
  limit_price_offset: 0.02              # GTC com 2% offset do midpoint
  fok_fallback_timeout_seconds: 30      # fallback para FOK após 30s
  min_confidence_score: 0.6             # só copia carteiras com score ≥ 0.6

# Track A — engine de arbitragem (banca isolada, edge matemático)
arbitrage:
  enabled: false                        # default off; ative após paper-validar
  mode: "paper"                         # paper | dry-run | live
  max_capital_usd: 200                  # banca dedicada (independente do copy)
  max_per_op_usd: 50                    # ticket size por oportunidade
  min_edge_pct: 0.005                   # 0.5% edge líquido mínimo (paper)
  fee_per_leg: 0.0                      # CLOB hoje cobra 0%; safety guard
  safety_buffer_pct: 0.003              # reserva contra book moving
  min_book_depth_usd: 20                # depth mínima por leg
  max_hours_to_resolution: 72
  scan_interval_seconds: 30
  auto_merge: true                      # mergePositions on-chain após 2 fills
  max_concurrent_ops: 3
  same_market_cooldown_seconds: 60

# Track B — sizing book-aware para o copy-trader
depth_sizing:
  enabled: false                        # ative em live
  max_impact_pct: 0.02                  # 2% de impacto máximo aceitável
  max_levels: 5                         # quantos níveis percorrer
```

---

## Go-live

### Sequência recomendada (não pule etapas)

**Semana 0 — Preparação**
1. Provisione QuantVPS NY, rode `provision_vps.sh`
2. Configure `.env` com credenciais reais
3. `geoblock_check.py` → não bloqueado
4. `latency_test.py` → p95 < 150ms
5. `setup_wallet.py` → L2 derivada + saldo USDC.e confirmado

**Semana 1-2 — Paper trading**
6. `executor.mode: "paper"` no `config.yaml`
7. `sudo systemctl start polytrader`
8. Observe por 7-14 dias: sinais detectados, filtros aplicados, PnL teórico, taxa de abort por slippage, taxa de block por duração de mercado

**Semana 3 — Live gradual**
9. `mode: "live"` + `max_portfolio_usd: 50` (10% do alvo)
10. `sudo systemctl restart polytrader`
11. Monitore por 3-5 dias antes de escalar

**Semana 4+ — Escala**
12. Conforme win rate e drawdown real convergirem com o paper, aumente `max_portfolio_usd` gradualmente até o alvo

### Rollback de emergência

```bash
sudo systemctl stop polytrader            # para imediatamente
# ordens abertas permanecem na Polymarket — cancele manualmente pela UI se necessário
```

---

## Segurança e modos de operação

### Três modos
- `dry-run`: detecta sinais, aplica filtros, mas **não constrói ordens**. Usado para validar pipeline.
- `paper`: detecta sinais, constrói `OrderDraft`, registra `CopyTrade` e atualiza `bot_positions` — **mas não envia ao CLOB**. Usado para medir PnL teórico com fricção realista.
- `live`: envia ordens reais via `py-clob-client` + EIP-712 signed. **Só ative após paper.**

### Gates de risco (bloqueiam qualquer trade)
- Score da carteira fonte < `min_confidence_score`
- Preço do outcome fora de `[min_price, max_price]`
- Posições abertas ≥ `max_positions`
- Daily loss excedeu `max_daily_loss_usd`
- Drawdown atingiu `max_drawdown_pct`
- Portfolio + proposta > `max_portfolio_usd`

Qualquer gate falhado → sinal marcado como `skipped` com `skip_reason` auditável em `trade_signals.skip_reason`.

### Halt global
Quando `daily_loss` ou `drawdown` são excedidos, o RiskManager seta `is_halted=True` e **rejeita todo sinal subsequente** até o operador intervir manualmente.

### Segredos
- `PRIVATE_KEY` nunca persistido em DB ou logs
- `L2Credentials` vivem apenas em RAM (derivadas uma vez no startup)
- `.gitignore` exclui `.env` e `data/*.db*`
- Dashboard (opcional) protegido por `DASHBOARD_SECRET`

---

## Estrutura do projeto

```
polytrader/
├── main.py                           # orquestrador asyncio.gather
├── pyproject.toml                    # uv + 19 deps runtime + 6 dev
├── config.yaml                       # comportamento do bot
├── .env.example                      # template de secrets
│
├── src/
│   ├── core/
│   │   ├── config.py                 # pydantic-settings + YAML loader
│   │   ├── database.py               # SQLite WAL + migrations runner
│   │   ├── exceptions.py             # PolyTraderError tree
│   │   ├── logger.py                 # structlog JSON → journalctl
│   │   ├── models.py                 # Pydantic v2 models
│   │   ├── quantize.py               # Diretiva 1 (Decimal)
│   │   ├── state.py                  # Fase 4: InMemoryState RAM cache
│   │   └── trade_event.py            # HFT — slots dataclass + parser (~30% faster)
│   │
│   ├── api/
│   │   ├── auth.py                   # EOA L2 prefetch
│   │   ├── background_limiter.py     # Arquitetura — Semaphore HFT vs auditoria
│   │   ├── balance.py                # USDC.e on-chain via web3
│   │   ├── clob_client.py            # CLOB 100% async httpx (post_order HFT)
│   │   ├── clob_l2_auth.py           # HMAC L2 headers (zero I/O, ~1µs)
│   │   ├── data_client.py            # Data API (leaderboard/positions/trades)
│   │   ├── gamma_client.py           # Gamma API 3-tier cache (RAM/SQLite/REST)
│   │   ├── heartbeat.py              # Diretiva 3 + zombie detection (3s silence)
│   │   ├── http.py                   # httpx AsyncClient singleton (HTTP/2)
│   │   ├── order_builder.py          # Diretivas 1+4 aplicadas
│   │   ├── retry.py                  # Diretiva 2 (@retry_on_425)
│   │   ├── startup_checks.py         # geoblock + latency baseline
│   │   └── websocket_client.py       # RTDS + reconect 50-150ms + zombie
│   │
│   ├── scanner/
│   │   ├── leaderboard.py            # fetch + agregação multi-período
│   │   ├── profiler.py               # WalletProfile + V/PnL ratio
│   │   ├── scorer.py                 # Regra 3 (wash filter) + score composto
│   │   └── wallet_pool.py            # top-N persistido + Set vivo para RTDS
│   │
│   ├── tracker/
│   │   ├── signal_detector.py        # Regra 2 (Exit Syncing) + filtro duração
│   │   ├── trade_monitor.py          # consume RTDS + dedup + enqueue
│   │   └── whale_inventory.py        # snapshot /positions → state
│   │
│   ├── executor/
│   │   ├── balance_cache.py          # background refresh 15s
│   │   ├── copy_engine.py            # pipeline central (risk → slippage → post)
│   │   ├── depth_sizing.py           # Track B — VWAP + max_impact cap
│   │   ├── order_manager.py          # build_draft + persist CopyTrade
│   │   ├── position_manager.py       # apply_fill + write-through state
│   │   ├── risk_manager.py           # checklist 10 itens + halt
│   │   └── slippage.py               # Regra 1 (Anti-Slippage Anchoring)
│   │
│   ├── arbitrage/                    # Track A — engine matemática paralela
│   │   ├── models.py                 # ArbOpportunity, ArbLegFill, ArbExecution
│   │   ├── scanner.py                # VWAP sweep YES+NO < 1 (2-pass)
│   │   ├── ctf_client.py             # AsyncWeb3 nativo + EIP-1559 dinâmico
│   │   ├── gas_oracle.py             # Polygon Gas Station + fee_history p99
│   │   └── executor.py               # FOK paralelo + rollback + auto-merge
│   │
│   └── notifier/
│       └── telegram.py               # fire-and-forget async
│
├── tests/                            # 100 testes pytest + pytest-benchmark
│   ├── test_scorer.py                # prova numérica Regra 3
│   ├── test_signal_detector.py       # prova Regra 2 + filtro duração
│   ├── test_slippage.py              # prova Regra 1 BUY/SELL
│   ├── test_risk_manager.py          # gates + halt
│   ├── test_state_cache.py           # RAM fast-path
│   ├── test_wallet_pool.py           # ranking + Set mutação in-place
│   ├── test_retry.py                 # Diretiva 2 (backoff 425)
│   ├── test_heartbeat.py             # Diretiva 3 (watchdog)
│   ├── test_quantize.py              # Diretiva 1 (Decimal ULP)
│   ├── test_order_builder.py         # Diretivas 1+4
│   ├── test_gamma_cache.py           # TTL cache
│   ├── test_config.py                # YAML + .env loader
│   └── benchmark.py                  # pytest-benchmark hot paths
│
├── migrations/
│   ├── 001_initial.sql               # 9 tabelas + índices
│   ├── 002_hft_resilience.sql        # tick_size + neg_risk columns
│   ├── 003_close_reason.sql          # close_reason + realized_pnl em bot_positions
│   ├── 004_arbitrage.sql             # arb_opportunities, arb_executions, bank snaps
│   └── 005_redeem_tx_hash.sql        # FUND-LOCK FIX — tx hash do redeem on-chain
│
├── scripts/
│   ├── init_db.py                    # aplica migrations
│   ├── geoblock_check.py             # verifica IP bloqueado
│   ├── latency_test.py               # 10 probes × 3 targets (p50/p95)
│   ├── setup_wallet.py               # deriva L2 + confere saldo
│   └── provision_vps.sh              # setup QuantVPS one-shot
│
├── deploy/
│   └── polytrader.service            # systemd unit (watchdog 120s)
│
└── autoresearch/                     # log da otimização autônoma
    └── 260413-2014-polytrader-perf/
        ├── overview.md
        ├── results.tsv               # 10 iterações
        └── summary.md                # 56× speedup documentado
```

---

## Testes

```bash
uv run python -m pytest -q               # 203 testes em ~7s
uv run python -m pytest tests/test_slippage.py -v   # prova da Regra 1
uv run python -m pytest tests/test_arbitrage_scanner.py tests/test_depth_sizing.py -v  # Tracks A+B
uv run python -m pytest tests/test_optimistic_execution.py tests/test_concurrent_engine.py -v  # HFT mode
uv run python -m pytest tests/test_hft_hot_path.py -v  # Hot path: HMAC, no disk I/O, zombie WS
uv run python -m pytest tests/test_trade_event.py tests/test_exit_sync_drift.py tests/test_gamma_ram_cache.py -v  # parsing layer
uv run python -m pytest tests/test_arbitrage_vwap.py tests/test_gas_oracle.py -v  # VWAP edge + dynamic gas
uv run python -m pytest tests/test_risk_refactor.py -v  # SELL bypass + Kelly + anti-fragmentação
uv run python -m pytest tests/test_resolution_redeem.py tests/test_background_limiter.py -v  # FUND-LOCK + batch + bg limiter
uv run python -m pytest tests/test_stale_cleanup_death_trap.py tests/test_price_updater_parallel.py -v  # death-trap + paralelo
uv run python -m pytest tests/benchmark.py -q       # benchmarks hot path
```

Cobertura das leis:
| Regra / Diretiva | Arquivo de teste |
|---|---|
| Regra 1 (Slippage) | `test_slippage.py` |
| Regra 2 (Exit Syncing) | `test_signal_detector.py`, `test_state_cache.py` |
| Regra 3 (Wash Trading) | `test_scorer.py` |
| Regra 4 (orjson+Set) | `benchmark.py::test_bench_rtds_parse_filter` |
| Diretiva 1 (Quantize) | `test_quantize.py`, `test_order_builder.py` |
| Diretiva 2 (Backoff 425) | `test_retry.py` |
| Diretiva 3 (Heartbeat) | `test_heartbeat.py` |
| Diretiva 4 (neg_risk) | `test_order_builder.py` |
| Track A (Arb scanner) | `test_arbitrage_scanner.py`, `test_arbitrage_models.py` |
| Track B (Depth sizing) | `test_depth_sizing.py` |
| HFT (Optimistic Exec) | `test_optimistic_execution.py` |
| HFT (Concurrent engine) | `test_concurrent_engine.py` |
| HFT (Hot path: HMAC + zombie WS) | `test_hft_hot_path.py` |
| HFT (TradeEvent parser) | `test_trade_event.py` |
| HFT (Exit sync drift correction) | `test_exit_sync_drift.py` |
| HFT (Gamma 3-tier RAM cache) | `test_gamma_ram_cache.py` |
| Quant (VWAP scanner edge math) | `test_arbitrage_vwap.py` |
| MEV (dynamic gas oracle cascade) | `test_gas_oracle.py` |
| Risk (SELL bypass + Kelly + fragmentation) | `test_risk_refactor.py` |
| Arch (FUND-LOCK redeem + batch DDoS fix) | `test_resolution_redeem.py` |
| Arch (BackgroundRateLimiter HFT vs auditoria) | `test_background_limiter.py` |
| Risk (Stale cleanup death-trap bypass) | `test_stale_cleanup_death_trap.py` |
| Infra (Price updater paralelo + executemany) | `test_price_updater_parallel.py` |

---

## Comandos úteis

### Operação diária (VPS)
```bash
sudo systemctl status polytrader
sudo systemctl restart polytrader
sudo journalctl -u polytrader -f                          # logs ao vivo
sudo journalctl -u polytrader -f | jq 'select(.level=="error")'   # só erros
sudo journalctl -u polytrader --since "1 hour ago" | jq .
```

### Diagnóstico
```bash
uv run python scripts/geoblock_check.py       # exit 0 = ok, 1 = blocked
uv run python scripts/latency_test.py         # p50/p95 RTT
sqlite3 data/polytrader.db "SELECT * FROM tracked_wallets WHERE is_active=1;"
sqlite3 data/polytrader.db "SELECT * FROM trade_signals ORDER BY detected_at DESC LIMIT 20;"
sqlite3 data/polytrader.db "SELECT * FROM risk_snapshots ORDER BY timestamp DESC LIMIT 1;"
```

### Backup do DB
```bash
# SQLite é um arquivo só — backup cron diário
cp data/polytrader.db "data/polytrader_$(date +%Y%m%d).db"
```

---

## Troubleshooting

### "EOA auth prefetch falhou"
Verifique se `PRIVATE_KEY` em `.env` é válida (hex `0x…` com 64 chars). Rode `scripts/setup_wallet.py` isolado para debug detalhado.

### "geoblock_detected"
Seu IP está nos EUA ou numa região bloqueada. Opções:
- Mover VPS para Europa/Ásia
- Usar proxy outbound (responsabilidade do operador)
- Esperar Polymarket US (CFTC) estar disponível publicamente

### Latência > 200ms persistente
Pode ser problema de rede da QuantVPS naquele momento. Checar:
```bash
uv run python scripts/latency_test.py
mtr --report clob.polymarket.com
```
Se persistir > 30min, abrir ticket na QuantVPS.

### Bot reinicia em loop
Ver último erro:
```bash
sudo journalctl -u polytrader --since "10 min ago" | grep -i error | tail -20
```
Comum: `.env` mal formatado, `PRIVATE_KEY` inválida, `config.yaml` com tipo errado (pydantic valida rigorosamente no startup).

### Sinais chegam mas todos viram `skipped`
Verifique motivo em `trade_signals.skip_reason`:
```sql
SELECT skip_reason, COUNT(*) FROM trade_signals
WHERE status='skipped' AND detected_at > datetime('now','-1 hour')
GROUP BY skip_reason;
```
Causas comuns: score mínimo alto demais, price_band estreito, todas as carteiras do pool operando mercados > 48h.

---

## Licença e disclaimer

**Uso por conta e risco.** Este código é fornecido "AS IS", sem garantias. Operar mercados de predição envolve risco real de perda total do capital. O autor e contribuintes não se responsabilizam por perdas financeiras, consequências regulatórias, ou qualquer outro dano decorrente do uso desta infraestrutura.

Antes de operar em `live`, certifique-se de:
- Conhecer as leis do seu país sobre mercados de predição
- Entender o modelo da Polymarket (CTF Exchange + neg_risk adapter)
- Ter testado em `paper` por ≥ 7 dias
- Estar confortável em perder 100% do capital alocado

Se você não entende uma seção deste README, **não rode em live**.

---

## Agradecimentos

- **Polymarket** pela API pública e pelo `py-clob-client`.
- **Andrej Karpathy** pelo padrão [autoresearch](https://github.com/karpathy/autoresearch) que norteou a otimização autônoma de performance.
- **QuantVPS** pelo datacenter em NY com SLA sólido e DDoS protection.
