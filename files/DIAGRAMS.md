# CWT Crypto Predictions Agent — System Flow Diagrams

> All diagrams use Mermaid syntax.
> **View options:**
> - GitHub: renders automatically in any `.md` file
> - VS Code: install "Markdown Preview Mermaid Support" extension → Ctrl+Shift+V
> - Online: paste into https://mermaid.live

---

## 1. System Architecture Overview

```mermaid
graph TD
    subgraph ENTRY["Entry Points"]
        RF[run_flow.py\nheadless / cron]
        HM[Hermes CLI\n/crypto-flow skill]
    end

    subgraph CORE["core/ — Business Logic"]
        PL[pipeline.py\nrun_once]
        MKT[markets/\npolymarket.py\nkalshi.py]
        DATA[data/\napify_ohlcv.py]
        PRED[predict/\nkronos_model.py]
        RISK[risk/\nkelly.py]
        FB[feedback/\nscoring.py]
        ARB[scale/\narbitrage.py]
    end

    subgraph EXTERNAL["External APIs"]
        PM[Polymarket\nGamma + CLOB API]
        KL[Kalshi\nTrade API v2]
        AP[Apify\nBinance klines actor]
        HF[HuggingFace Hub\nKronos-mini weights]
        OR[OpenRouter\nFree LLM]
    end

    subgraph STORAGE["Storage"]
        DB[(SQLite\ncwt.db)]
        CACHE[.cache/\nOHLCV JSON]
        LOGS[logs/\ncwt.log]
    end

    subgraph OUTPUT["Output"]
        TABLE[Prediction Table\nterminal]
        DASH[Streamlit\nDashboard]
    end

    RF --> PL
    HM --> |plugin tools| PL
    PL --> MKT
    PL --> DATA
    PL --> PRED
    PL --> RISK
    PL --> FB

    MKT --> PM
    MKT --> KL
    DATA --> AP
    DATA --> CACHE
    PRED --> HF
    HM --> OR

    PL --> DB
    PL --> LOGS
    RISK --> DB
    FB --> DB

    DB --> DASH
    PL --> TABLE
    ARB --> TABLE
```

---

## 2. The 5-Agent Pipeline (One Cycle)

```mermaid
flowchart TD
    START([Start: run_once]) --> SCORE

    SCORE["🔄 FEEDBACK FIRST\nAgent 5: score_predictions\nResolve matured OPEN predictions"]
    SCORE --> SCORE_Q{Any\nmatured?}
    SCORE_Q -->|Yes| RESOLVE["Lookup OHLCV bar\nactual = UP if close > open\nWrite outcomes table\nUpdate calibration\nAdjust kelly_multiplier"]
    SCORE_Q -->|No| ASSET_LOOP
    RESOLVE --> ASSET_LOOP

    ASSET_LOOP(["For each ASSET\n[BTC, ETH]"])

    ASSET_LOOP --> DATA["📊 Agent 2: fetch_ohlcv\nApify → Binance klines\n1000 bars × 5m interval\nNormalize + deduplicate\nPersist to ohlcv table"]

    DATA --> PRED["🧠 Agent 3: predict_move\nLoad Kronos-mini singleton\nSlice last 400 bars\nMonte Carlo × 30 samples\nCount close > last_close\np_up = count / 30"]

    PRED --> VENUE_LOOP(["For each VENUE\n[polymarket, kalshi]"])

    VENUE_LOOP --> MKT["📈 Agent 1: find_market\nPolymarket: slug → Gamma API → CLOB\nKalshi: series → events → ATM market\nGet implied_up, window_close_ts"]

    MKT --> MKT_Q{Market\nfound?}
    MKT_Q -->|No, timeout/error| SKIP[Log warning\nGraceful skip\nAdd to errors]
    MKT_Q -->|Yes| KELLY

    KELLY["💰 Agent 4: size_position\nCalculate edge (UP and DOWN)\nedge = p - c - fee\nf* = p-c / 1-c\nstake = clamp(0.25 × f*, 0, 10%)"]

    KELLY --> KELLY_Q{Edge > 0\nafter fee?}
    KELLY_Q -->|No| NONE["Side = NONE\nStake = $0"]
    KELLY_Q -->|Yes, UP| UP["Side = UP\nStake = kelly_fraction × $1000"]
    KELLY_Q -->|Yes, DOWN| DOWN["Side = DOWN\nStake = kelly_fraction × $1000"]

    NONE --> PERSIST
    UP --> PERSIST
    DOWN --> PERSIST
    SKIP --> NEXT_VENUE

    PERSIST["💾 INSERT INTO predictions\nstatus = OPEN"]
    PERSIST --> NEXT_VENUE{More\nvenues?}
    NEXT_VENUE -->|Yes| VENUE_LOOP
    NEXT_VENUE -->|No| NEXT_ASSET{More\nassets?}
    NEXT_ASSET -->|Yes| ASSET_LOOP
    NEXT_ASSET -->|No| REPORT

    REPORT["📋 RunReport\nPrint table:\nAsset | Venue | Model P | Market P | Edge | Side | Stake"]
    REPORT --> END([End cycle])
```

---

## 3. Polymarket Market Discovery (Sequence)

```mermaid
sequenceDiagram
    participant PL as pipeline.py
    participant PM as polymarket.py
    participant GA as Gamma API
    participant CL as CLOB API

    PL->>PM: find_market("BTC", "5m")

    Note over PM: window_start = floor(now/300) × 300
    Note over PM: slug = "btc-updown-5m-{window_start}"

    PM->>GA: GET /events?slug=btc-updown-5m-1782991500
    GA-->>PM: [{id, slug, markets:[{outcomes, clobTokenIds, endDateIso}]}]

    alt Slug not indexed yet (boundary latency)
        PM->>GA: GET /events?slug=btc-updown-5m-{prev_window}
        GA-->>PM: [{...previous window event...}]
    end

    Note over PM: Parse markets[0]
    Note over PM: outcomes = json.loads('["Up","Down"]') → index 0 = Up
    Note over PM: token_ids = json.loads('["111","222"]') → up_token = "111"

    PM->>CL: GET /midpoint?token_id=111
    CL-->>PM: {"mid": "0.43"}

    PM->>CL: GET /midpoint?token_id=222
    CL-->>PM: {"mid": "0.57"}

    Note over PM: implied_up = float("0.43") = 0.43
    Note over PM: window_close_ts = iso_to_ts(endDateIso)

    PM-->>PL: MarketData(asset="BTC", venue="polymarket",\nimplied_up=0.43, window_close_ts=...)
```

---

## 4. Kalshi Market Discovery (Sequence)

```mermaid
sequenceDiagram
    participant PL as pipeline.py
    participant KL as kalshi.py
    participant API as Kalshi API

    PL->>KL: find_market("BTC", "5m")

    KL->>API: GET /series?category=Crypto
    API-->>KL: {series: [{ticker:"KXBTCD",...},{ticker:"KXETHD",...}]}

    Note over KL: Find "KXBTCD" in ASSET_SERIES["BTC"]

    KL->>API: GET /events?series_ticker=KXBTCD&status=open
    API-->>KL: {events: [{event_ticker:"KXBTCD-26JUL03", strike_date:"2026-07-03T21:00:00Z",...}]}

    Note over KL: Sort by strike_date → pick soonest

    KL->>API: GET /markets?event_ticker=KXBTCD-26JUL03
    API-->>KL: {markets: [{ticker, yes_bid_dollars:"0.45", yes_ask_dollars:"0.55"}, ...]}

    Note over KL: For each market: mid = (bid + ask) / 2
    Note over KL: Pick market where |mid - 0.5| is smallest
    Note over KL: → "At the money" = most informative P(up)

    Note over KL: implied_up = (0.45 + 0.55) / 2 = 0.50
    Note over KL: close_time = market["close_time"]

    KL-->>PL: MarketData(asset="BTC", venue="kalshi",\nimplied_up=0.50, horizon="daily",...)

    Note over PL: Log horizon mismatch: requested 5m, Kalshi has daily
```

---

## 5. Database Entity-Relationship Diagram

```mermaid
erDiagram
    PREDICTIONS {
        int id PK
        int ts
        string asset
        string venue
        string horizon
        float model_p_up
        float market_p_up
        float edge
        string side
        float kelly_fraction
        float stake_paper
        int window_close_ts
        string status
        string created_at
    }

    OUTCOMES {
        int prediction_id PK_FK
        string resolved_at
        string actual_direction
        int won
        float pnl_paper
    }

    OHLCV {
        string asset PK
        string interval PK
        int open_time PK
        float open
        float high
        float low
        float close
        float volume
        float amount
        string source
    }

    MARKETS {
        int id PK
        int ts_window
        string asset
        string venue
        string horizon
        string up_ref
        string down_ref
        float implied_up
        float implied_down
        int window_close_ts
        string fetched_at
    }

    CALIBRATION {
        string asset PK
        int n
        float brier
        float hit_rate
        float kelly_multiplier
        string updated_at
    }

    RUNS {
        int id PK
        string started_at
        string finished_at
        int n_markets
        int n_predictions
        string notes
    }

    PREDICTIONS ||--o| OUTCOMES : "resolves to"
    PREDICTIONS }o--|| CALIBRATION : "scored per asset"
    PREDICTIONS }o--|| OHLCV : "resolved using"
```

---

## 6. Kelly Criterion Decision Tree

```mermaid
flowchart TD
    INPUT([Inputs:\np_up from Kronos\nimplied_up from market\nimplied_down from market\nfee = 0.01])

    INPUT --> EDGE_UP["Compute UP edge\nedge_up = p_up − implied_up − fee"]
    INPUT --> EDGE_DOWN["Compute DOWN edge\nedge_down = 1-p_up − implied_down − fee"]

    EDGE_UP --> Q1{edge_up > 0?}
    EDGE_DOWN --> Q2{edge_down > 0?}

    Q1 -->|No| F_UP_ZERO[f_up = 0]
    Q1 -->|Yes| CALC_UP["f_up = p_up − implied_up\n         1 − implied_up"]

    Q2 -->|No| F_DOWN_ZERO[f_down = 0]
    Q2 -->|Yes| CALC_DOWN["f_down = 1−p_up − implied_down\n          1 − implied_down"]

    F_UP_ZERO --> BOTH_Q
    F_DOWN_ZERO --> BOTH_Q
    CALC_UP --> BOTH_Q
    CALC_DOWN --> BOTH_Q

    BOTH_Q{Both edges\n≤ 0?}
    BOTH_Q -->|Yes| NONE["Decision: NONE\nkelly_fraction = 0\nstake = $0"]
    BOTH_Q -->|No| PICK_MAX{edge_up ≥ edge_down?}

    PICK_MAX -->|Yes| USE_UP[side = UP\nraw_f = f_up\nedge = edge_up]
    PICK_MAX -->|No| USE_DOWN[side = DOWN\nraw_f = f_down\nedge = edge_down]

    USE_UP --> CLAMP
    USE_DOWN --> CLAMP

    CLAMP["Apply fractional Kelly:\nclamped = min(kelly_multiplier × raw_f, f_max)\nclamped = max(clamped, 0)\nstake = clamped × bankroll"]

    CLAMP --> OUTPUT([Decision:\nside, edge\nkelly_fraction\nstake_paper])
```

---

## 7. Feedback Loop & Calibration

```mermaid
flowchart TD
    START([Cycle N+1 begins]) --> FIND["Find OPEN predictions\nwhere window_close_ts ≤ now"]

    FIND --> LOOP(["For each matured prediction"])

    LOOP --> OHLCV["Lookup OHLCV bar\nopen_time = window_close_ts − 300s"]

    OHLCV --> DIR_Q{close > open?}
    DIR_Q -->|Yes| UP_DIR[actual = UP]
    DIR_Q -->|No| DOWN_DIR[actual = DOWN]

    UP_DIR --> WON_Q
    DOWN_DIR --> WON_Q

    WON_Q{our side\n== actual?}
    WON_Q -->|Yes| WIN["won = 1\npnl = stake × (1-c)/c"]
    WON_Q -->|No| LOSE["won = 0\npnl = -stake"]

    WIN --> WRITE
    LOSE --> WRITE

    WRITE["INSERT INTO outcomes\nUPDATE predictions → RESOLVED"]

    WRITE --> MORE{More\nmatured?}
    MORE -->|Yes| LOOP
    MORE -->|No| CAL

    CAL["For each asset, compute:\nn = total resolved predictions\nbrier = mean((p_up − actual)²)\nhit_rate = wins / n"]

    CAL --> BRIER_Q{Brier > 0.30?}
    BRIER_Q -->|Yes, poor calibration| SHRINK["kelly_multiplier × 0.80\n(floor = 0.05)\n→ BET SMALLER"]
    BRIER_Q -->|No| RECOVER_Q{Brier < 0.22\nAND hit_rate > 0.50?}
    RECOVER_Q -->|Yes, well calibrated| RECOVER["kelly_multiplier × 1.10\n(cap = config default 0.25)\n→ BET LARGER"]
    RECOVER_Q -->|No| HOLD[kelly_multiplier unchanged]

    SHRINK --> UPSERT
    RECOVER --> UPSERT
    HOLD --> UPSERT

    UPSERT["UPSERT calibration table\n(asset, n, brier, hit_rate,\nkelly_multiplier, updated_at)"]

    UPSERT --> NEXT["Next cycle uses\nupdated kelly_multiplier\n→ Closed feedback loop ✓"]
```

---

## 8. Two Entry Points Architecture

```mermaid
graph LR
    subgraph USER["User triggers"]
        CMD1["python run_flow.py\n--cache / --loop / --demo"]
        CMD2["hermes\n> /crypto-flow"]
    end

    subgraph EP1["Entry Point 1: Headless"]
        RF[run_flow.py]
        RF_NOTE["✓ No LLM needed\n✓ Always works\n✓ Demo / cron\n✓ Free ($0)"]
    end

    subgraph EP2["Entry Point 2: Hermes"]
        SK[SKILL.md\n/crypto-flow recipe]
        PG[plugin/__init__.py\nregister tools]
        TL[tools.py\nthin handlers]
        EP2_NOTE["✓ LLM orchestrates\n✓ Natural language output\n✓ post_tool_call logging\n✗ Needs Hermes CLI\n✗ LLM can rate-limit"]
    end

    subgraph CORE["core/ — Shared Logic"]
        PL[pipeline.py]
        MK[markets/]
        DA[data/]
        PR[predict/]
        RI[risk/]
        FB[feedback/]
    end

    CMD1 --> RF
    CMD2 --> SK
    SK --> PG
    PG --> TL

    RF -->|run_once| PL
    TL -->|find_markets\nfetch_ohlcv\npredict_move\nsize_position\nscore_predictions| PL

    PL --> MK
    PL --> DA
    PL --> PR
    PL --> RI
    PL --> FB

    style CORE fill:#e8f5e9,stroke:#4caf50
    style EP1 fill:#e3f2fd,stroke:#2196f3
    style EP2 fill:#fff3e0,stroke:#ff9800
```

---

## 9. Apify OHLCV Fetch & Normalize

```mermaid
flowchart LR
    subgraph INPUT["Input"]
        CFG[asset = BTC\nsymbol = BTCUSDT\ninterval = 5m\nlimit = 1000]
    end

    subgraph CACHE_CHECK["Cache Check"]
        CC{.cache/ohlcv_\nBTCUSDT_5m.json\nexists?}
    end

    subgraph APIFY["Apify Call"]
        AC["ApifyClient.actor(\n'parseforge/binance-\nprices-scraper').call({\nmode: klines,\nsymbol: BTCUSDT,\ninterval: 5m,\nlimit: 1000\n})"]
        DS["client.dataset(\nrun.defaultDatasetId\n).iterate_items()"]
    end

    subgraph NORMALIZE["Normalize"]
        N1["Rename columns:\nopenTime → open_time\nOpen → open\nHigh → high etc."]
        N2["Convert types:\nopen_time: ms → seconds\nif value > 4B: ÷ 1000\nprices: to float"]
        N3["Compute amount:\namount = close × volume\nif column missing"]
        N4["Sort ascending\nby open_time"]
    end

    subgraph PERSIST["Persist"]
        P1["INSERT OR IGNORE\nINTO ohlcv\n(dedup by PK:\nasset+interval+open_time)"]
        P2["Write .cache/\nohlcv_BTCUSDT_5m.json"]
    end

    OUT[/"DataFrame\n1000 rows\n[open_time, open,\nhigh, low, close,\nvolume, amount]"/]

    CFG --> CC
    CC -->|Yes, use_cache=True| P2
    CC -->|No| AC
    AC --> DS
    DS --> N1
    P2 --> N1
    N1 --> N2
    N2 --> N3
    N3 --> N4
    N4 --> P1
    N4 --> OUT
    P1 --> OUT
```

---

## 10. Kronos Monte Carlo P(up)

```mermaid
flowchart TD
    START([df: 1000 OHLCV rows]) --> SLICE

    SLICE["Slice last 400 rows\nlookback = df.tail(cfg.kronos.lookback)\nlast_close = lookback.close.iloc[-1]"]

    SLICE --> TS["Build timestamps\nx_ts = pd.to_datetime(open_times, unit='s')\ny_ts = pd.to_datetime([last_open + 300s], unit='s')"]

    TS --> INIT["ups = 0\nN = 30 (mc_samples)"]

    INIT --> LOOP{i < N?}

    LOOP -->|Yes| INFER["predictor.predict(\n  df=lookback[ohlcv cols],\n  x_timestamp=x_ts,\n  y_timestamp=y_ts,\n  pred_len=1,\n  T=1.0, top_p=0.9,\n  sample_count=1\n)"]

    INFER --> CMP{pred.close.iloc-1\n>\nlast_close?}

    CMP -->|Yes| INC["ups += 1"]
    CMP -->|No| NEXT["i += 1"]
    INC --> NEXT
    NEXT --> LOOP

    LOOP -->|No, done| CALC["p_up = ups / N"]

    CALC --> OUT(["Return p_up ∈ [0, 1]\nExample: 22/30 = 0.73"])

    subgraph NOTE["Why Monte Carlo?"]
        N1["Kronos uses top-p sampling\n= stochastic, not deterministic\nSingle run = one possible future\n30 runs = empirical probability"]
    end
```

---

## 11. Cross-Venue Arbitrage Signal

```mermaid
flowchart TD
    INPUT(["markets: list of MarketData\nfee = 0.02 per side"])

    INPUT --> GROUP["Group by asset\nPM = polymarket MarketData\nKL = kalshi MarketData"]

    GROUP --> SPREAD["spread = |PM.implied_up − KL.implied_up|"]

    SPREAD --> NET["net_edge = spread − 2 × fee\n(round-trip cost = buy one side +\n sell the other = 2 × fee)"]

    NET --> Q{net_edge > 0?}

    Q -->|No| SKIP[No signal\nSpread within normal fees]

    Q -->|Yes| DIRECTION{PM.implied_up\n>\nKL.implied_up?}

    DIRECTION -->|Yes, PM prices UP higher| BUY_KL["BUY on Kalshi (cheaper UP)\nSELL on Polymarket (expensive UP)\nExpected edge = net_edge"]

    DIRECTION -->|No, KL prices UP higher| BUY_PM["BUY on Polymarket (cheaper UP)\nSELL on Kalshi (expensive UP)\nExpected edge = net_edge"]

    BUY_KL --> SIG[ArbSignal returned]
    BUY_PM --> SIG

    subgraph EXAMPLE["Live Example (real data)"]
        E1["BTC: Polymarket=0.38, Kalshi=0.675\nspread = 0.295\nnet_edge = 0.295 − 0.04 = 0.255\n→ BUY polymarket, note Kalshi overpricing"]
    end
```

---

## 12. Testing Architecture

```mermaid
graph TD
    subgraph REAL["Real External Services"]
        PM_API[Polymarket API]
        KL_API[Kalshi API]
        AP_API[Apify API]
        KR_MDL[Kronos Model\nHuggingFace]
        OR_API[OpenRouter LLM]
    end

    subgraph FAKES["Test Fakes / Mocks"]
        RESP[responses library\nHTTP-level mock\nfor PM + Kalshi]
        FAK_AP[FakeApifyClient\nreturns fixture JSON]
        FAK_PR[FakePredictor\nalways_up=True/False]
        TMP_DB[tmp_path SQLite\nfresh DB per test]
    end

    subgraph TESTS["Test Files"]
        T1[test_skeleton.py\nconfig + schema]
        T2[test_polymarket_slug.py\nslug math + mocked HTTP]
        T3[test_kalshi.py\ndiscovery + parsing]
        T4[test_apify_ohlcv.py\nnormalize + fake client]
        T5[test_kronos.py\nMC logic + fake predictor]
        T6[test_kelly.py\npure math]
        T7[test_pipeline_fakes.py\nend-to-end green gate]
        T8[test_feedback.py\nresolve + calibration]
        T9[test_arbitrage.py\narb math]
    end

    RESP -.->|replaces| PM_API
    RESP -.->|replaces| KL_API
    FAK_AP -.->|replaces| AP_API
    FAK_PR -.->|replaces| KR_MDL
    TMP_DB -.->|replaces| T7

    T2 --> RESP
    T3 --> RESP
    T4 --> FAK_AP
    T5 --> FAK_PR
    T7 --> FAK_AP
    T7 --> FAK_PR
    T7 --> RESP
    T7 --> TMP_DB

    style REAL fill:#ffebee,stroke:#f44336
    style FAKES fill:#e8f5e9,stroke:#4caf50
    style TESTS fill:#e3f2fd,stroke:#2196f3
```
