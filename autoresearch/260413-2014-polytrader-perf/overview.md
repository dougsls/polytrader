# Autoresearch Run — polytrader-perf

**Start:** 2026-04-13 20:14 UTC
**Baseline commit:** 3fbc152

## Config

| | |
|---|---|
| Goal | Acelerar `detect_signal` + RTDS parse/filter (lower is better) |
| Scope | `src/**/*.py`, `tests/**/*.py` |
| Verify | `uv run pytest tests/benchmark.py -q` (extrai mean μs) |
| Metric | `total_us` = mean(detect_signal) + mean(rtds_filter) |
| Direction | **Lower is better** |
| Guard | `uv run pytest tests/ --ignore=tests/benchmark.py -q` (42 tests) |
| Iterations | 25 (bounded) |

## Baseline (iter 0)

| Benchmark | Mean (μs) |
|---|---|
| `detect_signal` | 25,654.51 |
| `rtds_parse_filter` (100 msgs) | 45.63 |
| **total** | **25,700.14** |

Hot path óbvio: `detect_signal` domina (99.8% do total). Cada chamada abre
3 conexões aiosqlite via `@asynccontextmanager`, que inicia thread nova.
Essa é a grande oportunidade.

## Hypothesis queue (para o loop)

1. **Reduzir conexões SQLite em `detect_signal`**: hoje `_bot_position_size` + `get_whale_size` abrem 2 conexões. Merge numa query só (JOIN ou 2 selects na mesma conexão).
2. **Short-circuit: BUY não precisa tocar DB.** Atualmente percorre todo o fluxo. Mover verificação de side antes das queries.
3. **Cache de gamma já é bom** (AsyncMock 0 IO) — foco no DB.
4. **RTDS filter já em ~460 ns/msg** — ganho marginal. Última prioridade.
5. **`asyncio.run()` per call no benchmark** cria event loop novo. No prod não é issue, mas afeta baseline. Não modificar benchmark — manter honesto.

## Loop log

Cada iteração escreve uma linha em `results.tsv` com decisão (keep/discard/crash).
