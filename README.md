# MTG Scanner Data

Scryfall card database delta updates and bulk price snapshots for the [MTG Scanner](https://github.com/cheokjiade/mtg-scanner) iOS app.

## How it works

A GitHub Action runs every 2 hours and:
1. Downloads the full Scryfall `default_cards` bulk data (~500MB)
2. Compares against the previous version (stored as a Release manifest)
3. Generates a compressed delta JSON with only new/changed/removed cards (~1-5MB)
4. Extracts all price data (USD, USD Foil, EUR, EUR Foil) into a separate bulk price file
5. Extracts set data for any new/updated sets
6. Publishes everything as GitHub Release assets

The MTG Scanner app checks this repo for delta releases and downloads only the changes instead of the full 500MB file. Price data is separated from card hashes so daily price fluctuations don't inflate the delta size.

## Release Assets

Each release (`scryfall-delta-YYYY-MM-DD`) contains:

| File | Description | Size |
|------|-------------|------|
| `delta.json.gz` | Compressed delta with new/updated/removed cards | ~1-5MB |
| `prices.json.gz` | Bulk price snapshot (USD + USD Foil + EUR + EUR Foil) | ~3-5MB |
| `manifest.json` | Card hash manifest for computing the next delta | ~15MB |
| `sets.json` | Complete Scryfall set data | ~100KB |

## Delta Format

```json
{
    "version": "1.0",
    "scryfall_updated_at": "2026-04-03T...",
    "previous_updated_at": "2026-04-02T...",
    "new": [ {card_object}, ... ],
    "updated": [ {card_object}, ... ],
    "removed": [ "uuid1", "uuid2", ... ],
    "sets": [ {set_object}, ... ]
}
```

Price changes are excluded from the card hash, so only actual card data changes (new printings, oracle text updates, legality changes) generate delta entries. This keeps deltas small (~1-5MB) even though prices change daily for most cards.

## Price Format

```json
{
    "generated_at": "2026-04-03T...",
    "prices": {
        "card-uuid-1": {
            "usd": "1.23",
            "usd_foil": "4.56",
            "eur": "1.10",
            "eur_foil": "3.80"
        }
    }
}
```

## App Integration

The app's `DeltaUpdateService` handles:
- **Delta chaining**: If the user hasn't updated in 10 days, downloads and applies all 10 daily deltas sequentially
- **Fallback**: If more than 30 deltas are needed, falls back to full Scryfall download
- **Bulk price update**: Downloads `prices.json.gz` and updates all cached prices in one pass
- **Set sync**: Imports any new or updated sets from the delta

## License

Card data is from [Scryfall](https://scryfall.com/) and is used under their [API terms](https://scryfall.com/docs/api).
