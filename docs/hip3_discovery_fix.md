# HIP-3 Asset Discovery Fix

## Problem
The Hyperliquid SDK `meta_and_asset_ctxs()` method only returns the default (Native) asset context. It does not automatically aggregate assets from other builder-deployed Dexes (HIP-3), even if the `Info` client is initialized with them. As a result, `get_all_perp_assets` was only returning 224 Native assets and missing HIP-3 markets like those in the 'hyna' Dex.

## Solution
We implemented an iterator-based discovery mechanism in `HyperliquidAPI.get_all_perp_assets`.

### Implementation Details

1.  **Fetch Native Assets**:
    Call the default `meta_and_asset_ctxs()` to get the base Native universe.

2.  **Iterate Builder Dexes**:
    If `include_hip3` is enabled, we retrieve the list of all available Dexes using `self.info.perp_dexs()`.

3.  **Explicit Metadata Fetching**:
    For each non-native Dex (identified by name), we explicitly request its metadata by sending a POST request to `/info` with the payload `{"type": "metaAndAssetCtxs", "dex": "DEX_NAME"}`.
    
    ```python
    # Example logic
    for dex_obj in dex_list:
        dex_name = dex_obj.get('name')
        if dex_name:
             dex_meta_ctx = self.info.post("/info", {"type": "metaAndAssetCtxs", "dex": dex_name})
             # Process and append assets...
    ```

4.  **Result**:
    The method now aggregates assets from all enabled Dexes. Verification confirmed retrieval of 62 additional HIP-3 assets (including 9 from 'hyna') alongside the 224 Native assets.

## Status
Implemented and verified. The previous index-based assumption (that all assets were in one list) was incorrect and has been replaced by this iterator approach.
