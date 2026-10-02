"""ai command: Claude reads a sample of company reports (paid; needs ANTHROPIC_API_KEY)."""

from __future__ import annotations

import argparse
import os
import sys
from contextlib import closing


def cmd_ai(args: argparse.Namespace) -> int:
    from trend_bot import ai_reader, db

    with closing(db.connect(args.db)) as con:
        if args.action == "summary":
            print(ai_reader.summary(con))
            return 0
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("ANTHROPIC_API_KEY is not set (add it as a GitHub secret).", file=sys.stderr)
            return 1
        import anthropic

        client = anthropic.Anthropic()
        pending = ai_reader.pending_batches(con)
        if args.action == "sample" and not pending:
            picks = ai_reader.sample(con, args.n)
            if picks.empty:
                print("No readable reports to sample yet (run the reports catch-up first).")
                return 0
            reqs = ai_reader.prepare(con, picks)
            # Upper bound: all text tokens plus the full output allowance, at batch prices.
            chars = sum(len(r["params"]["messages"][0]["content"]) + len(ai_reader.SYSTEM) for r in reqs)
            worst = (chars / 3 * ai_reader.PRICE_IN + len(reqs) * ai_reader.MAX_TOKENS * ai_reader.PRICE_OUT) / 1e6
            print(f"[ai] {len(reqs)} requests, at most ${worst:.2f} (the real cost is usually well below)")
            if worst > args.max_dollars:
                print(f"[ai] stopped: above the --max-dollars limit of ${args.max_dollars:.2f}", file=sys.stderr)
                return 1
            if not reqs:
                return 0
            pending = [ai_reader.submit(con, client, reqs)]
            print(f"[ai] batch {pending[0]} submitted")
        elif pending:
            print(f"[ai] collecting {len(pending)} earlier batch(es) first")
        for batch_id in pending:
            ai_reader.collect(con, client, batch_id, wait_seconds=args.wait)
        print()
        print(ai_reader.summary(con))
    return 0
