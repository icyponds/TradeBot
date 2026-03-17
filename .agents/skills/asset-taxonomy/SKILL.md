---
name: asset-taxonomy
description: Guidelines on how the codebase manages symbol mapping, taxonomy, and formatting for Spot, Native Perps (HIP-2), and Builder DEX (HIP-3) assets within the Hyperliquid API wrapper.
---

# Asset Taxonomy and Symbol Mapping

The Hyperliquid SDK and API refer to different asset classes in distinct ways. This skill explains how `TradeBot` normalizes and queries these symbols, specifically inside `src/api/hyperliquid_api.py`.

## 1. Spot Assets
- **Internal Formatting**: `[COIN]_SPOT` (e.g., `BTC_SPOT`, `PURR_SPOT`).
- **Network Resolution**: Spot assets cannot be queried directly using their human-readable strings via the API execution endpoints. 
- **Mapping Logic**: They are mapped to a numerical API ID prepended with an `@` (e.g., `UBTC` translates to something like `@109`).
- **Code Ref**: When making SDK calls for Spot, `TradeBot` uses `get_spot_api_name(api_token_name)` to resolve the true numerical `@` index representation. For example, `BTC_SPOT` -> `UBTC` -> `@109`.

## 2. Native Perps (HIP-2)
- **Internal Formatting**: Standard uppercase coin tickers (e.g., `BTC`, `SOL`).
- **Context/DEX Name**: Native perps reside on the core Hyperliquid DEX, represented internally array-wise or in SDK queries as an empty string `""`.
- **Mapping Logic**: Because they belong to the default namespace, they do not require a prefix when requesting user state or matching symbols.

## 3. Builder DEX Assets (HIP-3)
- **Internal Formatting**: Prefixed with the originating DEX name: `[ExchangeName]:[Ticker]` (e.g., `Hypurr:PLTR`, `Hypurr:MSTR`).
- **Context/DEX Name**: Unlike Native Perps, HIP-3 assets are deployed by community builders. They belong to custom execution contexts (like `"Hypurr"`).
- **Mapping Logic**: When reading position payloads or fetching quotes, `TradeBot` explicitly prefixes the coin string with `dex_name:` (e.g., `f"{dex_name}:{coin}"`) if `dex_name` is present.

## General Guideline for Developers
When interacting with `HyperliquidAPI`:
- **Do not** manually construct the `@` formatting for Spot assets outside of the dedicated `MarketInterface`. Use `get_spot_api_name()` and `normalize_symbol()`.
- **Remember** that an empty string `""` DEX signifies Native Perps, while populated strings (e.g., `"Hypurr"`) refer to HIP-3 assets and require a colon mapping (`[DEX]:[COIN]`) when referencing symbol names in our internal model dictionaries like `self.positions`.
