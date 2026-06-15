# tournament-rebuild-tool

A small standalone Python script for gib.meme operators (or anyone running a
similar on-chain tournament) to **re-derive the canonical state of a
tournament directly from the on-chain registry** and emit a CSV/JSON the
matchmaker / leaderboard / UI can ingest.

Built in response to an observation that off-chain matchmaker state can
drift from the on-chain registry under heavy mass-entry load — symptom is
"player gets a free pass in round 1" or "opponent side of the matchup card
is blank" — both of which indicate the off-chain DB has either lost entries
or has ghost entries the chain doesn't back.

The tool is **read-only**. It does not write to the chain. It does not
touch your database. You consume its output.

## What it does

1. Derives the tournament's on-chain PDA from `(BOARD, tournament_index)`.
2. Walks every signature on that PDA's history (up to `--max-sigs`).
3. Filters for `register_to_tournament` instructions (Anchor discriminator
   `19d84691f01e600b`).
4. For each tx: extracts the signer wallet, deck index, and the binder slot
   indices that compose the deck.
5. Cross-references the slot indices against each signer's binder PDA to
   resolve them to concrete cNFT asset hashes.
6. Optionally resolves asset hashes -> meme names via Helius DAS.
7. Emits:
   - **CSV** — one row per entry, with columns
     `tournament_signature, block_time, wallet, deck_index, card_slot_0..2,
     asset_hash_0..2, meme_0..2, card_resolved`.
   - **JSON** — same data structured, plus per-wallet summary.
8. Optional: **diff against your DB export** — pass `--diff <your_db_export.json>`
   and the tool will report which entries are on-chain-only (your DB
   missed them) vs DB-only (ghost entries with no on-chain backing) vs
   both.

## Install

Python 3.10+. Stdlib only. No `pip install` needed.

```bash
git clone <this-repo>
cd tournament-rebuild-tool
```

## Set up RPC access

The tool needs a Solana mainnet RPC URL. Two options:

- **Helius** (recommended; free tier works): get a key at
  https://helius.dev, then `export HELIUS_API_KEY=<your-key>`.
- **Your own RPC**: pass `--rpc-url https://my-rpc.example/...`.

The tool calls `getSignaturesForAddress`, `getTransaction`,
`getAccountInfo`, and `getAssetBatch` (DAS extension). Any
DAS-compatible Helius-compatible RPC works (Helius, Triton, Quicknode,
Shyft).

## Usage

```bash
# Scan the current tournament + emit CSV + JSON
python rebuild_tournament_state.py --tournament 86

# Use your own RPC instead of Helius
python rebuild_tournament_state.py --tournament 86 \
    --rpc-url https://my-mainnet-rpc.example/

# Specify output paths
python rebuild_tournament_state.py --tournament 86 \
    --csv t86_entries.csv --json t86_state.json

# Diff against your existing DB export
python rebuild_tournament_state.py --tournament 86 \
    --diff your_db_export.json --diff-output t86_drift.json

# Skip meme-name resolution (faster; saves DAS calls)
python rebuild_tournament_state.py --tournament 86 --no-resolve-memes

# Cap the sig scan depth (default 50000)
python rebuild_tournament_state.py --tournament 86 --max-sigs 10000

# More workers (faster on a fast RPC; default 8)
python rebuild_tournament_state.py --tournament 86 --workers 16
```

## DB-export format for `--diff`

The tool accepts either:

```json
[
  {"wallet": "<pubkey>", "deck_index": 0, "signature": "<optional>"},
  {"wallet": "<pubkey>", "deck_index": 1, "signature": "<optional>"},
  ...
]
```

or a dict with the entries under `entries`:

```json
{
  "entries": [
    {"wallet": "<pubkey>", "deck_index": 0, ...},
    ...
  ]
}
```

The diff identifies tuples by `(wallet, deck_index)`. The `signature` field
is optional in both inputs — when present, the diff output includes both
the on-chain and DB-side signatures for in-both entries so you can spot
signature-mismatch (which would indicate a deeper indexer bug).

## What you do with the output

### Case 1 — Matchmaker shows ghost decks (opponent side blank)

The matchmaker has stale state that points to entries that aren't on-chain.
Run with `--diff your_matchmaker_dump.json` and the `only_in_db` list is
what to evict.

### Case 2 — Wallet got "free pass in round 1" without an opponent

Find the wallet in the per-wallet summary printout (or grep the CSV). If
the entry-count is unusually high, the matchmaker likely couldn't fit them
into a balanced bracket and gave some byes. Two paths:

- **Mass-entry policy:** consider rate-limiting per-wallet entries per
  window at registration time. The on-chain program does not do this; it's
  off-chain policy.
- **Bracket-fit fix:** re-seed the bracket from the CSV with whatever
  bye-allocation policy you prefer (e.g., spread byes across multiple
  wallets, not concentrate them on the largest entrant).

### Case 3 — Your DB shows fewer entries than on-chain

Run with `--diff`. The `only_on_chain` list is the gap; ingest those
entries into your DB to catch up.

### Case 4 — Just want a clean state to seed a fresh bracket

Use the JSON output. The `entries` list is per-tx, deduplicated by
`(wallet, deck_index)`. Sort by `block_time` for chronological replay or
by `wallet` for per-user audit.

## Performance notes

- A tournament with ~10 000 entries takes ~2-4 minutes on Helius free
  tier with 8 workers.
- Sig-scan dominates; tx-classification parallelizes well.
- Binder resolution (one `getAccountInfo` per unique wallet) is ~50-200ms
  per wallet — large tournaments with many distinct wallets bottleneck
  here. Increase `--workers` if your RPC tier allows.
- Meme resolution via DAS `getAssetBatch` is one call per 1 000 unique
  asset hashes — usually 1-3 calls total.

## Limitations

- The tool does NOT decode tournament rule fields (cards-per-deck,
  duration, prize tier). It assumes the standard 3-cards-per-deck shape;
  this is hardcoded in the on-chain program at present.
- The tool does NOT discover the bot vs human distinction for entries.
  Add per-tx ALT classification if you want to spot bot-style mass entries.
- DAS meme-name resolution depends on Helius DAS being current. If a
  newly-minted meme isn't yet indexed, its `meme_*` columns will be null.
- The on-chain registry only records that a wallet submitted a deck. It
  does NOT record match outcomes (those live in survival-round ixs which
  are a separate Anchor instruction, discriminator `ff0071ef66757232`).
  This tool intentionally ignores those.

## License

MIT.
