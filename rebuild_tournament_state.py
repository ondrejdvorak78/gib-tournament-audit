#!/usr/bin/env python3
"""rebuild_tournament_state.py

Re-derive the canonical state of a gib.meme tournament from on-chain data.

The motivation: gib.meme's off-chain matchmaker / leaderboard / UI feeds
their own database, which can drift from the on-chain registry under heavy
mass-entry load. This tool reads the on-chain registry directly and emits a
ground-truth CSV/JSON of every confirmed registration. Drop it into the
gib.meme operations workflow and use it to:

  - Re-seed a corrupted matchmaker bracket from scratch.
  - Diff against your own database to find missing entries or ghost entries.
  - Audit a specific wallet's submissions (e.g., one wallet entered 200+
    decks and got a free pass in round 1).

The tool is read-only. It does NOT write to the chain, does NOT submit
transactions, does NOT touch your database directly. You consume its output.

Usage:
    python rebuild_tournament_state.py --tournament 86

    # With output paths
    python rebuild_tournament_state.py --tournament 86 \\
        --csv entries.csv --json entries.json

    # Diff against your existing database export
    python rebuild_tournament_state.py --tournament 86 \\
        --diff your_db_export.json --diff-output drift.json

    # Cap the scan depth (default: walk all sigs on the registry)
    python rebuild_tournament_state.py --tournament 86 --max-sigs 20000

Set HELIUS_API_KEY in the environment OR pass --rpc-url with your own
Solana RPC endpoint.

Stdlib only — no install required beyond Python 3.10+.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants (extracted from the gib.meme on-chain program + JS bundle)
# ---------------------------------------------------------------------------

PROGRAM_ID = "4zAxB3Q6VVV8msirodwkjCfaeZumKitkcvR7pUveSqSR"
BOARD = "BYYdh3UjeKF1Gfjb4vy2JJhjTUoQxKZ62mP9z5YA9Aou"
STORE = "HnXcGEL6KBqivrKJHSVEj26dkBoENVVXZRibHwh4RmPY"

# 8-byte Anchor discriminators
DISC_REGISTER_TO_TOURNAMENT = bytes.fromhex("19d84691f01e600b")
DISC_REGISTER_CARD = bytes.fromhex("21199a6f9b1f2d24")

# Binder account layout (bytes per card slot + header)
BINDER_HEADER_SIZE = 65
BINDER_CARD_SIZE = 152


# ---------------------------------------------------------------------------
# base58 + ed25519 on-curve (stdlib only; needed for PDA derivation)
# ---------------------------------------------------------------------------

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\x00"))) + out


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58.index(c)
    leading = len(s) - len(s.lstrip("1"))
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * leading + body


_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_MARKER = b"ProgramDerivedAddress"


def _is_on_curve(pk: bytes) -> bool:
    if len(pk) != 32:
        return False
    y = int.from_bytes(pk, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    yy = (y * y) % _P
    num = (yy - 1) % _P
    den = (_D * yy + 1) % _P
    try:
        xx = (num * pow(den, _P - 2, _P)) % _P
        x = pow(xx, (_P + 3) // 8, _P)
        if (x * x - xx) % _P != 0:
            x = (x * pow(2, (_P - 1) // 4, _P)) % _P
        if (x * x - xx) % _P != 0:
            return False
    except Exception:
        return False
    return True


def find_program_address(seeds, program_id: str) -> tuple[str, int]:
    pid = b58decode(program_id)
    for bump in range(255, -1, -1):
        h = hashlib.sha256()
        for s in seeds:
            h.update(s)
        h.update(bytes([bump]))
        h.update(pid)
        h.update(_MARKER)
        cand = h.digest()
        if not _is_on_curve(cand):
            return b58encode(cand), bump
    raise RuntimeError("no PDA exists for these seeds")


def tournament_pda(idx: int) -> tuple[str, int]:
    """PDA seed: ['tournament', board, u32_le(idx)]"""
    return find_program_address(
        [b"tournament", b58decode(BOARD), idx.to_bytes(4, "little")],
        PROGRAM_ID,
    )


def binder_pda(creator: str) -> tuple[str, int]:
    """PDA seed: ['binder', creator, board]"""
    return find_program_address(
        [b"binder", b58decode(creator), b58decode(BOARD)],
        PROGRAM_ID,
    )


# ---------------------------------------------------------------------------
# RPC client
# ---------------------------------------------------------------------------

class RpcClient:
    def __init__(self, url: str, timeout: int = 45, max_retries: int = 4):
        self.url = url
        self.timeout = timeout
        self.max_retries = max_retries

    def call(self, method: str, params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        for attempt in range(self.max_retries):
            try:
                req = urllib.request.Request(
                    self.url, data=body,
                    headers={"Content-Type": "application/json"},
                )
                resp = urllib.request.urlopen(req, timeout=self.timeout)
                return json.loads(resp.read())
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
                if attempt == self.max_retries - 1:
                    raise
                time.sleep(0.5 * (attempt + 1))


# ---------------------------------------------------------------------------
# Tx classification
# ---------------------------------------------------------------------------

def decode_register_args(data_b58: str) -> dict | None:
    """Decode register_to_tournament ix args.

    Layout: 8-byte disc + u16 deck_index + u32 cards_len + cards_len * u16 slot_idx
    """
    try:
        raw = b58decode(data_b58)
    except Exception:
        return None
    if len(raw) < 8 or raw[:8] != DISC_REGISTER_TO_TOURNAMENT:
        return None
    if len(raw) < 14:
        return None
    deck_index = struct.unpack_from("<H", raw, 8)[0]
    cards_len = struct.unpack_from("<I", raw, 10)[0]
    if cards_len > 64 or len(raw) < 14 + cards_len * 2:
        return None
    cards = [struct.unpack_from("<H", raw, 14 + i * 2)[0] for i in range(cards_len)]
    return {"deck_index": deck_index, "card_slots": cards}


def classify_tx(rpc: RpcClient, sig: str, blocktime: int) -> dict | None:
    """Fetch a single tx and extract registration data."""
    try:
        r = rpc.call("getTransaction", [
            sig,
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
             "commitment": "confirmed"},
        ])
    except Exception:
        return None
    decoded = r.get("result")
    if not decoded:
        return None
    meta = decoded.get("meta") or {}
    err = meta.get("err")
    tx = decoded.get("transaction", {})
    msg = tx.get("message", {})
    accs = msg.get("accountKeys", []) or []
    ixs = msg.get("instructions", []) or []
    signer = None
    for a in accs:
        if isinstance(a, dict) and a.get("signer"):
            signer = a.get("pubkey")
            break
    if not signer:
        return None
    for ix in ixs:
        if ix.get("programId") != PROGRAM_ID:
            continue
        args = decode_register_args(ix.get("data", ""))
        if not args:
            continue
        return {
            "signature": sig,
            "block_time": blocktime,
            "signer": signer,
            "deck_index": args["deck_index"],
            "card_slots": args["card_slots"],
            "err": err,
            "tx_status": "failed" if err else "ok",
        }
    return None


# ---------------------------------------------------------------------------
# Binder resolution (slot index -> on-chain asset hash)
# ---------------------------------------------------------------------------

def fetch_binder(rpc: RpcClient, creator: str) -> dict | None:
    addr, _ = binder_pda(creator)
    info = rpc.call("getAccountInfo", [addr, {"encoding": "base64"}])
    val = info.get("result", {}).get("value")
    if not val:
        return None
    raw = base64.b64decode(val["data"][0])
    return parse_binder(raw)


def parse_binder(raw: bytes) -> dict:
    """Return {slot: {asset_hash, available, status, locks}} for every slot."""
    stored = struct.unpack_from("<H", raw, 61)[0]
    if BINDER_HEADER_SIZE + stored * BINDER_CARD_SIZE > len(raw):
        raise ValueError(f"binder truncated: stored={stored} but data is {len(raw)}B")
    slots = {}
    for i in range(stored):
        off = BINDER_HEADER_SIZE + i * BINDER_CARD_SIZE
        card = raw[off:off + BINDER_CARD_SIZE]
        slots[i] = {
            "asset_hash": b58encode(card[2:34]),
            "available": bool(card[0]),
            "status": bool(card[1]),
        }
    return slots


def resolve_asset_memes(rpc: RpcClient, asset_ids: list[str]) -> dict[str, str]:
    """Batch-resolve asset_id -> meme name via Helius DAS getAssetBatch."""
    out: dict[str, str] = {}
    # getAssetBatch caps at 1000 per call
    BATCH = 1000
    for i in range(0, len(asset_ids), BATCH):
        chunk = asset_ids[i:i + BATCH]
        try:
            r = rpc.call("getAssetBatch", {"ids": chunk})
        except Exception:
            continue
        for entry in r.get("result", []) or []:
            aid = entry.get("id", "")
            meme = None
            for attr in entry.get("content", {}).get("metadata", {}).get("attributes", []):
                if attr.get("trait_type") == "Meme":
                    meme = attr["value"]
                    break
            if meme:
                out[aid] = meme
    return out


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def paginate_sigs(rpc: RpcClient, addr: str, max_sigs: int = 50_000,
                  page_limit: int = 1000) -> list[dict]:
    out: list[dict] = []
    before = None
    seen = set()
    page = 0
    while len(out) < max_sigs:
        page += 1
        params = [addr, {"limit": min(page_limit, max_sigs - len(out))}]
        if before:
            params[1]["before"] = before
        r = rpc.call("getSignaturesForAddress", params)
        res = r.get("result", [])
        if not res:
            break
        new = [s for s in res if s["signature"] not in seen]
        if not new:
            break
        for s in new:
            seen.add(s["signature"])
        out.extend(new)
        before = res[-1]["signature"]
        if len(res) < params[1]["limit"]:
            break
        oldest_age_h = (time.time() - res[-1].get("blockTime", 0)) / 3600 if res[-1].get("blockTime") else -1
        print(f"  sig page {page}: +{len(new)}  cum={len(out)}  oldest_age={oldest_age_h:.1f}h", flush=True)
    return out


def scan_tournament(rpc: RpcClient, tournament_index: int,
                    max_sigs: int = 50_000, workers: int = 8,
                    resolve_memes: bool = True) -> dict:
    pda, _ = tournament_pda(tournament_index)
    print(f"Tournament {tournament_index} PDA: {pda}", flush=True)

    print(f"\nPaginating signatures (cap {max_sigs})...", flush=True)
    sigs = paginate_sigs(rpc, pda, max_sigs=max_sigs)
    print(f"Total signatures on registry PDA: {len(sigs)}", flush=True)

    print(f"\nClassifying transactions ({workers} workers)...", flush=True)
    entries: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(classify_tx, rpc, s["signature"], s.get("blockTime", 0)): s["signature"]
                for s in sigs}
        done = 0
        for fut in as_completed(futs):
            done += 1
            r = fut.result()
            if r:
                entries.append(r)
            if done % 500 == 0:
                print(f"  {done}/{len(sigs)} ({len(entries)} register entries so far)", flush=True)

    # Deduplicate by (signer, deck_index) — keep latest by block_time. The
    # registry receives both the original entry and any survival-round writes;
    # we want exactly the register entries.
    by_key: dict[tuple[str, int], dict] = {}
    for e in entries:
        if e["tx_status"] != "ok":
            continue
        key = (e["signer"], e["deck_index"])
        if key not in by_key or e["block_time"] > by_key[key]["block_time"]:
            by_key[key] = e
    entries = list(by_key.values())
    entries.sort(key=lambda e: (e["signer"], e["deck_index"]))

    # Per-signer aggregation
    by_signer: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        by_signer[e["signer"]].append(e)

    print(f"\nUnique entries: {len(entries)} (across {len(by_signer)} wallets)", flush=True)

    # Resolve slot -> asset_hash per signer via binder PDA, then meme via DAS.
    print(f"\nResolving binder slots for {len(by_signer)} wallets...", flush=True)
    signer_to_binder: dict[str, dict] = {}

    def _fetch(wallet: str):
        try:
            return wallet, fetch_binder(rpc, wallet)
        except Exception:
            return wallet, None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_fetch, w) for w in by_signer.keys()]
        done = 0
        for fut in as_completed(futs):
            done += 1
            w, b = fut.result()
            if b:
                signer_to_binder[w] = b
            if done % 50 == 0:
                print(f"  {done}/{len(by_signer)} binders", flush=True)

    # Collect all referenced asset_ids
    all_asset_ids: set[str] = set()
    for e in entries:
        b = signer_to_binder.get(e["signer"], {})
        for s in e["card_slots"]:
            slot = b.get(s)
            if slot:
                all_asset_ids.add(slot["asset_hash"])

    meme_map: dict[str, str] = {}
    if resolve_memes and all_asset_ids:
        print(f"\nResolving {len(all_asset_ids)} unique asset_ids -> meme names via DAS...", flush=True)
        meme_map = resolve_asset_memes(rpc, list(all_asset_ids))
        print(f"  resolved {len(meme_map)} meme names", flush=True)

    # Annotate each entry with asset_hashes + meme labels
    for e in entries:
        b = signer_to_binder.get(e["signer"], {})
        e["asset_hashes"] = []
        e["memes"] = []
        e["card_resolved"] = True
        for s in e["card_slots"]:
            slot = b.get(s)
            if slot:
                e["asset_hashes"].append(slot["asset_hash"])
                e["memes"].append(meme_map.get(slot["asset_hash"]))
            else:
                e["asset_hashes"].append(None)
                e["memes"].append(None)
                e["card_resolved"] = False

    return {
        "tournament_index": tournament_index,
        "tournament_pda": pda,
        "scanned_signatures": len(sigs),
        "register_entries": len(entries),
        "distinct_wallets": len(by_signer),
        "entries": entries,
        "wallet_summary": [
            {"wallet": w, "deck_count": len(ents),
             "first_deck_block_time": min(e["block_time"] for e in ents),
             "last_deck_block_time": max(e["block_time"] for e in ents)}
            for w, ents in by_signer.items()
        ],
    }


def write_csv(out_path: Path, entries: list[dict]) -> None:
    cols = ["tournament_signature", "block_time", "wallet", "deck_index",
            "card_slot_0", "card_slot_1", "card_slot_2",
            "asset_hash_0", "asset_hash_1", "asset_hash_2",
            "meme_0", "meme_1", "meme_2",
            "card_resolved"]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for e in entries:
            slots = e["card_slots"] + [None] * (3 - len(e["card_slots"]))
            hashes = e["asset_hashes"] + [None] * (3 - len(e["asset_hashes"]))
            memes = e["memes"] + [None] * (3 - len(e["memes"]))
            w.writerow([e["signature"], e["block_time"], e["signer"], e["deck_index"],
                        *slots, *hashes, *memes, e["card_resolved"]])
    print(f"CSV  -> {out_path}", flush=True)


def write_json(out_path: Path, scan: dict) -> None:
    out_path.write_text(json.dumps(scan, indent=2))
    print(f"JSON -> {out_path}", flush=True)


# ---------------------------------------------------------------------------
# Diff against an external database export
# ---------------------------------------------------------------------------

def diff_against_export(scan: dict, export_path: Path) -> dict:
    """Compare the on-chain scan against an off-chain JSON export.

    Expected export format (loose):
      [{wallet: str, deck_index: int, signature: str?, ... }, ...]
    OR a dict with a top-level 'entries' list.

    Returns:
      {
        only_on_chain: [{wallet, deck_index, signature, ...}],
        only_in_db:    [{wallet, deck_index, ...}],
        in_both:       [{wallet, deck_index, on_chain_sig, db_sig}]
      }
    """
    raw = json.loads(export_path.read_text())
    db_entries = raw if isinstance(raw, list) else raw.get("entries", [])
    db_keys = {(e.get("wallet") or e.get("signer"), e.get("deck_index"))
               for e in db_entries if e.get("wallet") or e.get("signer")}
    db_lookup = {((e.get("wallet") or e.get("signer")), e.get("deck_index")): e
                 for e in db_entries}

    chain_keys = {(e["signer"], e["deck_index"]) for e in scan["entries"]}
    chain_lookup = {(e["signer"], e["deck_index"]): e for e in scan["entries"]}

    only_chain = sorted(chain_keys - db_keys)
    only_db = sorted(db_keys - chain_keys)
    both = sorted(chain_keys & db_keys)

    return {
        "tournament_index": scan["tournament_index"],
        "stats": {
            "on_chain_entries": len(chain_keys),
            "db_entries": len(db_keys),
            "only_on_chain": len(only_chain),
            "only_in_db": len(only_db),
            "in_both": len(both),
        },
        "only_on_chain": [chain_lookup[k] for k in only_chain],
        "only_in_db": [db_lookup[k] for k in only_db],
        "in_both_sample": [
            {"wallet": k[0], "deck_index": k[1],
             "on_chain_sig": chain_lookup[k]["signature"],
             "db_sig": db_lookup[k].get("signature")}
            for k in both[:20]
        ],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="\n".join(__doc__.splitlines()[1:]))
    p.add_argument("--tournament", type=int, required=True,
                   help="Tournament index to scan")
    p.add_argument("--rpc-url", type=str, default=None,
                   help="Solana RPC URL (default: Helius mainnet via HELIUS_API_KEY env var)")
    p.add_argument("--max-sigs", type=int, default=50_000,
                   help="Cap signature scan depth (default 50000)")
    p.add_argument("--workers", type=int, default=8,
                   help="Parallel RPC workers (default 8)")
    p.add_argument("--csv", type=str, default=None,
                   help="Output CSV path (default: tournament_<N>_entries.csv)")
    p.add_argument("--json", dest="json_out", type=str, default=None,
                   help="Output JSON path (default: tournament_<N>_state.json)")
    p.add_argument("--no-resolve-memes", action="store_true",
                   help="Skip DAS meme-name resolution (faster; CSV gets nulls in meme_* columns)")
    p.add_argument("--diff", type=str, default=None,
                   help="Path to existing DB JSON export to diff against")
    p.add_argument("--diff-output", type=str, default=None,
                   help="Write diff result here (default: tournament_<N>_drift.json)")
    args = p.parse_args()

    rpc_url = args.rpc_url
    if not rpc_url:
        key = os.environ.get("HELIUS_API_KEY")
        if not key:
            print("error: set HELIUS_API_KEY or pass --rpc-url", file=sys.stderr)
            sys.exit(1)
        rpc_url = f"https://mainnet.helius-rpc.com/?api-key={key}"

    rpc = RpcClient(rpc_url)

    scan = scan_tournament(rpc, args.tournament,
                           max_sigs=args.max_sigs,
                           workers=args.workers,
                           resolve_memes=not args.no_resolve_memes)

    csv_path = Path(args.csv) if args.csv else Path(f"tournament_{args.tournament}_entries.csv")
    json_path = Path(args.json_out) if args.json_out else Path(f"tournament_{args.tournament}_state.json")

    write_csv(csv_path, scan["entries"])
    write_json(json_path, scan)

    # Per-wallet summary printout for visual scan
    by_count = sorted(scan["wallet_summary"], key=lambda x: -x["deck_count"])
    print(f"\n=== Top 20 wallets by deck count ===")
    for ws in by_count[:20]:
        print(f"  {ws['wallet']} : {ws['deck_count']:4d} decks")
    print(f"\nTotal: {scan['register_entries']} entries from {scan['distinct_wallets']} wallets")

    if args.diff:
        diff_path = Path(args.diff_output) if args.diff_output else \
                    Path(f"tournament_{args.tournament}_drift.json")
        diff_result = diff_against_export(scan, Path(args.diff))
        diff_path.write_text(json.dumps(diff_result, indent=2))
        print(f"\nDIFF -> {diff_path}")
        print(f"  on-chain entries: {diff_result['stats']['on_chain_entries']}")
        print(f"  db entries:       {diff_result['stats']['db_entries']}")
        print(f"  only on-chain (missing from db):  {diff_result['stats']['only_on_chain']}")
        print(f"  only in db (ghost entries):       {diff_result['stats']['only_in_db']}")
        print(f"  in both:                          {diff_result['stats']['in_both']}")


if __name__ == "__main__":
    main()
