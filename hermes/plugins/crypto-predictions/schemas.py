"""Tool schemas — what the LLM sees for each tool in the plugin."""

SCHEMAS = {
    "find_markets": {
        "name": "find_markets",
        "description": (
            "Find the current up/down prediction markets for a crypto asset "
            "on Polymarket and Kalshi. Returns implied probabilities and window close time."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "asset": {
                    "type": "string",
                    "enum": ["BTC", "ETH"],
                    "description": "Crypto asset to find markets for.",
                },
                "horizon": {
                    "type": "string",
                    "default": "5m",
                    "description": "Market horizon: '5m' or '15m'.",
                },
            },
            "required": ["asset"],
        },
    },
    "fetch_ohlcv": {
        "name": "fetch_ohlcv",
        "description": (
            "Fetch the last N OHLCV bars for a crypto asset via Apify/Binance. "
            "Returns row count persisted."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "asset": {
                    "type": "string",
                    "enum": ["BTC", "ETH"],
                },
                "interval": {
                    "type": "string",
                    "default": "5m",
                    "description": "Kline interval e.g. '5m', '1m'.",
                },
                "limit": {
                    "type": "integer",
                    "default": 1000,
                    "description": "Number of bars (max 1000).",
                },
            },
            "required": ["asset"],
        },
    },
    "predict_move": {
        "name": "predict_move",
        "description": (
            "Run Kronos Monte-Carlo forecasting on cached OHLCV data to estimate "
            "P(up) for the next bar."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "asset": {
                    "type": "string",
                    "enum": ["BTC", "ETH"],
                },
            },
            "required": ["asset"],
        },
    },
    "size_position": {
        "name": "size_position",
        "description": (
            "Apply the Kelly criterion to model probability vs market price to "
            "decide side (UP/DOWN/NONE) and paper stake. Persists to DB."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "asset": {
                    "type": "string",
                    "enum": ["BTC", "ETH"],
                },
                "venue": {
                    "type": "string",
                    "enum": ["polymarket", "kalshi"],
                },
            },
            "required": ["asset", "venue"],
        },
    },
    "score_predictions": {
        "name": "score_predictions",
        "description": (
            "Resolve matured OPEN predictions: check actual direction from OHLCV, "
            "compute PnL, update Brier score and Kelly multiplier per asset."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}
