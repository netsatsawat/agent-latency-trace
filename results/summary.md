# Results: gpt-6.2-sol, task `checkout`, 20 runs

Medians with 95 percent bootstrap intervals in brackets. The share rows and the cache hit rate are totals over all the runs. Seconds have 2 decimal places and speeds are whole tokens a second. analyze.py wrote this file from `data/raw/2026-11-26-gpt-6.2-sol-checkout.jsonl`. Do not edit it by hand.

| | medium | xhigh |
|---|---|---|
| runs that passed the tests | 10 of 10 | 10 of 10 |
| task time | 9.26 s (9.01 to 9.90) | 11.24 s (10.92 to 11.76) |
| model calls per task | 5 (4 to 5.5) | 4 (4 to 4) |
| wait for first token, per call | 1.08 s (1.05 to 1.18) | 1.34 s (1.05 to 1.55) |
| wait for first token, first call | 1.59 s (1.52 to 1.82) | 1.53 s (1.46 to 1.58) |
| writing speed | 95 tokens/s (90 to 105) | 100 tokens/s (83 to 105) |
| reasoning tokens per task | 0 (0 to 0) | 137 (116 to 183) |
| visible tokens per task | 337.5 (313 to 502) | 505 (498 to 505) |
| cache hit rate after the first call | 99% | 92% |
| time waiting for first tokens | 61% | 60% |
| time writing | 37% | 38% |
| time in tools | 0.6% | 0.5% |
| other time: the end of each stream and the script | 1.7% | 1.2% |
| cost per task (estimate) | $0.0049 | $0.0087 |

Paired by run number in the same file, xhigh minus medium: 1.96 s (0.83 to 2.59). 10 of 10 pairs were slower at the higher effort.

Over all 20 runs, a task made 4 to 6 model calls, a call wrote 14 to 398 visible tokens, and the estimated cost came to $0.1396 in all.

Wait for the first token against reasoning tokens, 68 calls after the first: 1.06 s plus 15.12 ms per reasoning token. The thinking speed comes out at about 66 reasoning tokens a second (60 to 72). Only 10 of those calls had any reasoning tokens.
