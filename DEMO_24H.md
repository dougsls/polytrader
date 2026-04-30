# 24h DEMO — Paper Realista de US$ 200

Esta demo simula **execução com livro real do CLOB**. Não há fakefill no
preço da whale. Cada decisão é auditada no `demo_journal`. O dashboard
calcula um **Readiness Score 0-100** com regras objetivas de GO/NO-GO.

---

## Premissas operacionais

- **Banca inicial**: US$ 200
- **Modo**: `paper` com `paper_perfect_mirror: false`
- **Filtros aplicados** (idênticos ao live):
  - `whale_max_slippage_pct: 0.03` (3%)
  - `max_spread: 0.02` (2¢)
  - `depth_sizing.enabled: true` + `max_impact_pct: 0.02`
  - `max_positions: 8`
  - `max_position_pct_of_bank: 0.05` (5% da banca por mercado)
  - `max_tag_exposure_pct: 0.30` (30% por tag/categoria)
  - `enforce_market_duration: true`
  - `avoid_resolved_markets: true`
  - `copy_buys: true`, `copy_sells: true`
- Quando `bypass_slippage_check=true` (stale cleanup / emergency exit):
  ignora cap de spread+slippage **mas** ainda exige book não-vazio.
- **NUNCA** fakefill no preço da whale — sempre VWAP do book.

---

## Como iniciar a demo na VPS

```bash
ssh root@SEU_IP

cd /opt/polytrader
git pull origin master

# Backup do config atual + ativar demo
cp config.yaml config.yaml.backup-$(date +%Y%m%d)
cp config.demo.yaml config.yaml

# Aplica migrations novas (006_demo_journal etc.)
.venv/bin/python -c "import asyncio; from src.core.database import init_database; asyncio.run(init_database())"

# Restart do bot
systemctl restart polytrader
journalctl -u polytrader -f
```

---

## URL do dashboard

```
http://SEU_IP:8080
```

Login (Basic Auth):
- **Usuário**: valor de `DASHBOARD_USER` no `.env` (default `operator`)
- **Senha**: valor de `DASHBOARD_SECRET` no `.env`

---

## Métricas que você deve olhar primeiro

### 1. Topbar (sempre visível)
- **Bank** — deve subir/cair organicamente, sem saltos artificiais.
- **PnL** — só conta fills via book real.
- **DD** — drawdown peak-to-trough. Se passar 5%, atenção.
- **Score** — readiness 0-100. NO-GO automático em DD > 10%.

### 2. Tela "Overview"
- Bank inicial vs Bank atual.
- Win rate consistente (≥50% após ≥10 trades fechados).
- Open positions ≤ 8.
- Latências p50: CLOB <200ms, Gamma <300ms, Data API <300ms.

### 3. Tela "Execution Quality"
- Avg slippage BUY/SELL **<2%** (ideal <1%).
- Avg spread **<1.5¢**.
- Skip reasons: maioria deve ser `DEPTH_TOO_THIN`/`SLIPPAGE_HIGH` (book real ruim, não bug).
- Whale-vs-Bot: ΔCents pequeno (<2¢ na maioria).

### 4. Tela "Trader Attribution"
- Pelo menos 3 traders com `STRONG` ou `WATCH`.
- Nenhum trader com `UNDER` dominando o PnL negativo.

### 5. Tela "Risk"
- Exposição por mercado balanceada (nenhum >10% da banca).
- Stale positions zero ou perto disso.

### 6. Tela "Trade Journal"
- Toda linha com `skip_reason` preenchido (auditoria 100%).
- Ratio executed:skipped saudável (10-50% executed; muito > 50% sugere
  filtros frouxos demais; muito < 10% sugere filtros apertados demais).

### 7. Tela "Readiness"
- Score evolui ao longo das 24h.
- Verdict final: GO (≥75) / EXTEND_PAPER (50-74) / NO-GO (<50 ou flag).
- Nenhuma flag NO-GO ativa.

---

## Checklist GO/NO-GO para colocar dinheiro real

Você só deve passar para LIVE com dinheiro real se TODOS os critérios
abaixo passarem após 24h de demo:

### Critérios obrigatórios (qualquer NÃO = NO-GO)

- [ ] **ROI 24h positivo** ou neutro (>0% acumulado)
- [ ] **Drawdown máximo < 10%** (NO-GO automático se >10%)
- [ ] **Slippage médio < 2%** em BUY e SELL
- [ ] **Spread médio < 2¢**
- [ ] **Zero erros críticos de API** (CLOB/Gamma/RTDS)
- [ ] **Audit completo**: todo skip tem `skip_reason` no journal
- [ ] **Sample size ≥ 20 sinais** processados na janela
- [ ] **PnL não-concentrado**: maior trade < 80% do PnL total absoluto
- [ ] **Estado consistente**: `bot_positions` (DB) bate com `state RAM`

### Critérios desejáveis (não-bloqueantes, mas importantes)

- [ ] Win rate ≥ 50% após ≥ 10 trades fechados
- [ ] Pelo menos 2 traders com verdict `STRONG`
- [ ] Skip rate entre 10-80% (filtros calibrados)
- [ ] Latências dentro do esperado (CLOB p95 < 300ms)
- [ ] Nenhum mercado consumindo > 10% da banca
- [ ] Nenhuma stale position > 24h

### Sinais de NO-GO automático

- 🚨 Drawdown máximo > 10%
- 🚨 Slippage médio > 2%
- 🚨 Mais de 50 erros API/WS na janela
- 🚨 PnL positivo dependente de 1 outlier (>80% do total)
- 🚨 Posições no DB divergem do `state` RAM (estado inconsistente)
- 🚨 Sinais sem motivo de skip auditável (`skip_reason IS NULL`)

---

## Como reverter para paper observação ou live

```bash
ssh root@SEU_IP
cd /opt/polytrader

# Voltar pro paper antigo (observação bruta)
cp config.yaml.backup-YYYYMMDD config.yaml

# Ou ativar live (CUIDADO — só após GO)
cp config.live.yaml config.yaml

systemctl restart polytrader
```

---

## Troubleshooting da demo

| Sintoma | Diagnóstico | Solução |
|---------|-------------|---------|
| Score travado em 60 | Sample size < 20 | Aguardar mais sinais (24h é mínimo) |
| Score baixo (<50) sem flag NO-GO | Componentes neutros | Ver `/api/demo/readiness-score` |
| Equity curve vazio | Snapshot loop não rodou | `journalctl -u polytrader -f \| grep demo_snapshot` |
| Trades viram fills perfeitos | `paper_perfect_mirror=true` | Setar `false` em config |
| Nenhum sinal copiado em 1h+ | Filtros muito apertados ou pool vazio | Conferir Scanner + signal_detector logs |
| API errors ≥ 10/h | RTDS down ou Geoblock | Conferir `/api/demo/readiness` health |
