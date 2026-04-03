# MTG Scanner Data

Scryfall card database delta updates for the MTG Scanner iOS app.

## How it works

A GitHub Action runs every 2 hours and:
1. Downloads the full Scryfall `default_cards` bulk data (~500MB)
2. Compares against the previous version (stored as a Release manifest)
3. Generates a compressed delta JSON with only new/changed/removed cards
4. Publishes the delta as a GitHub Release asset

The MTG Scanner app checks this repo for delta releases and downloads only the changes (~1-5MB) instead of the full 500MB file.

## Release Assets

Each release contains:
- `delta.json.gz` — compressed delta with new/updated/removed cards
- `manifest.json` — card hash manifest for computing the next delta

## Delta Format

```json
{
    "version": "1.0",
    "scryfall_updated_at": "2026-04-03T...",
    "previous_updated_at": "2026-04-02T...",
    "new": [ {card_object}, ... ],
    "updated": [ {card_object}, ... ],
    "removed": [ "uuid1", "uuid2", ... ]
}
```

## License

Card data is from [Scryfall](https://scryfall.com/) and is used under their [API terms](https://scryfall.com/docs/api).
