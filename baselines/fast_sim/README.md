# fast_sim — Track 3 participant simulator

## Executive summary

`fast_sim` is a **bit-faithful accelerator of the pinned ABIDES baseline**, not a
new matching engine. The kernel, limit-order book, agents, oracle and latency
model stay the pinned ABIDES objects. Speed comes from work that does **not**
appear in the scored traces, a compiled (Cython) OrderBook / Kernel hot path,
and the Phase 3–6 cuts of the Python tax each profiler pass named.
Phase 6 compiled the remaining hubs and then hit diminishing returns
(<10% on both throughput units). Championship step 1 replaced the
Python tuple heap with a C `EventQueue`. Step 2 inlines PriceLevel
ops and compiles `cancel_order` inside the same Cython book.
Step 3 compact hops were reverted (after-close extras). Step 4
compiled MM / Value / Momentum `act()`. Step 5 retries compact
internal hops + a columnar ledger, with `QuerySpreadResponse.mkt_closed
= current_time > mkt_close`. Step 6 starts the single-C-process cut
with a bit-exact C `MT19937` and NoiseTrader draws. Step 7 ports
latency lognormal and ValueTrader `observe_price` onto that stream.
Step 8 dispatches wakeup / act / exchange inside Cython and keeps
`LimitOrder` off the hop (field tuples). Step 9 puts a C `COrder`
in the book and writes trace / ledger from C arrays. Step 10
makes the hop a C `Event` and keeps agent working orders in a C
map. Step 11 is a from-scratch C kernel (`fast_sim._native`) that
replaces `Kernel.run` for the four Track-3 agents + exchange; the
hybrid stays as `FAST_SIM_NATIVE=0`. Step 12 moves latency
`uniform` onto C `MT19937` and writes parquet from pyarrow
Tables. Step 13 ports ValueTrader `observe_price` (OU +
megashock + scheduled jump) onto C after a lock-step proof
against the Python oracle. A follow-up writes message
ledgers through pandas ``Int64`` so official ``pd.read_parquet``
does not promote ``t_send_ns`` to float64 (batch isolation).
No GPU.

GPU is unused. CUDA / `sm_100` is not compiled; the default path is CPU.

## Why Cython (not Rust / C++)

Agents still `isinstance` Python `Message` / `LimitOrder` objects, and
`message_id` is assigned in `Message.__post_init__` in construction order.
Cython replaces `execute_order` / `handle_limit_order` / `Kernel.runner` /
`Kernel.send_message` while sharing those objects.

`deepcopy` on a partial fill is snapshotting: the book copy is mutated in
place after the fill snapshot; the accept message keeps the working residual.

## What Phase 5 actually was hot (after Phase 4)

Unprofiled split of the Phase 4 binary on this host:

| Unit | kernel | extract_trace | extract_msg | kernel-only ev/s | full ev/s |
|---|---|---|---|---|---|
| `as06_throughput_fast` | 0.68s (80%) | 0.13s | 0.04s | 109k | 86.9k |
| `gb_mega_throughput` | 14.6s (81%) | **2.67s (15%)** | 0.80s (4%) | 80.2k | 64.8k |

cProfile (gb_mega, Phase 4): Exchange send/recv wrappers (Cython
`send_message` time lands on the Python caller), `LimitOrder.__init__` /
`Message.__post_init__`, NoiseTrader `randint` + `act`/`wakeup`,
`extract_trace` walking dict logs.

Phase 5: fast `LimitOrder.__init__` (no Order ABC) and `__new__` messages
with the same `message_id` order; book path calls `kernel.send_message`
with `pipeline_delay`; thin `ScheduledAgent.wakeup` / `NoiseTrader.act` /
`place_limit_order` (same RNG draws); compact 7-tuple order / 4-tuple quote
logs; Cython `set_wakeup`; BEST_BID/ASK read cached `_visible_qty`.

Phase 5 split of the new binary:

| Unit | kernel | extract_trace | extract_msg | kernel-only ev/s | full ev/s |
|---|---|---|---|---|---|
| `as06_throughput_fast` | 0.54s (82%) | 0.08s | 0.04s | 138k | 113k |
| `gb_mega_throughput` | 12.5s (85%) | 1.44s (10%) | 0.82s (6%) | 93.7k | 79.3k |

New hot spots: `ExchangeAgent.receive_message` (still the QuerySpread /
handle_limit hub), `ScheduledAgent.wakeup`, `Kernel.run` (Cython loop
billed to Python), `place_limit_order` / `noise_act` / numpy `randint`.
`Message.__post_init__` and dict-log extract left the top 20.

## CLI

```
simulate       --config /input/scenario.json --out /output/trace.parquet
simulate-batch --batch-dir /input/scenarios   --out-dir /output
```

## Local run (no Docker)

```bash
cd baselines && python setup_fast_sim.py build_ext --inplace

PYTHONPATH=baselines python -m fast_sim.simulate \
    --config regression_suite/scenarios/s001_price_time_priority.json \
    --out /tmp/fast_s001/trace.parquet
```

Requires the pinned ABIDES commit with the four `baselines/patches/` overlays.
The participant `Dockerfile` compiles the extension at image-build time
(`linux/amd64`, vendored, no runtime network).

## Local semantic + throughput results (this host, not the B200 fleet)

Official ranking is host-measured parquet-row-count / wall clock. Machines
differ. Traces were compared to the shipped unit references (exact fills,
coverage, timestamps, message ledger).

**Phase 6: Family 1 14/14 exact. as06, gb_mega, MP (`mp01`) and RA (`ra01`)
exact.** Phase 6 compiled Exchange receive / wakeup / place / `noise_act`
and stored the ledger as tuples. That was a real attempt at the Phase 5
hubs. **Both units moved <10% vs Phase 5** (as06 **1.08×**, gb_mega
inside the Phase 5 repeat band). Stop. No more micro-opts.

| Scenario | Shipped | P3 | P4 | P5 | Phase 6 | vs Phase 5 |
|---|---|---|---|---|---|---|
| `as06_throughput_fast` | 14,183 | 66,030 | 84,202 | 119,608 | **129,295** | **1.08×** |
| `gb_mega_throughput` | 10,571 | 48,259 | 66,921 | 78,140 | **74,862** | **0.96×** |

Repeats: as06 120k–129k, gb_mega 65.6k–74.9k. Official B200 worker will
differ. `events_per_sec` is `n_events / wall_clock_sec`.

## Championship roadmap (what 10× vs current would take)

A 10× from ~120k / ~78k is **~1.2M / ~780k ev/s**. That is not another
`__str__` or `isinstance` patch. After step 1 the Python tuple heap is
gone. Remaining wall is still **per-message Python**: `LimitOrder` /
`Message` objects, a numpy `RandomState` call per NoiseTrader draw, a
latency draw + ledger row per send, extract walking logs, and
MM/Value/Momentum `act()` still in Python. Discrete-event matching plus
Tier-A exactness forbids batched/JAX books.

**Keep in Python:** scenario JSON → config, rare paths (MarketHours,
post-close, modify/replace), parquet extract.

**Compile together:** book + kernel + all four Track-3 agents as one
C/Cython process with (1) compact event structs that still assign
`message_id` / `order_id` in ABIDES construction order, (2) a C heap
with the same `(deliver_at, (sid, rid, message))` / `Message.__lt__`
tie-break, (3) an RNG that matches `numpy.random.RandomState` bit-exact
(or a pre-drawn stream consumed in the same order), (4) a columnar
ledger written at deliver time.

**GPU:** only for *independent* `simulate-batch` scenarios. Never to
reorder one book's events.

**Tier-A risks:** heap-tie reordering; `message_id`/`order_id` construction
order; partial-fill snapshot vs in-place qty; `pipeline_delay` / latency
draws; STP `cancel_newest` / `cancel_oldest`; Kendall-τ if time
arithmetic changes.

**Suggested sequence:** (1) C event queue + compiled Exchange send/recv
while leftover agents still see Python objects; (2) compile Noise / MM /
Value / Momentum with bit-exact RNG; (3) zero-copy columnar ledger;
(4) GPU only for batch-of-scenarios.

## Championship step 1 (this change)

C `EventQueue` min-heap in `fast_sim._hotpath` (Python `heapq` twin in
`hotpath.py`). Each event is `(deliver_at, sid, rid, message_id)` plus a
borrowed `Message` pointer — the same key as
`(deliver_at, (sid, rid, message))` with `Message.__lt__` by
`message_id`. `Kernel.send_message` / `set_wakeup` / `runner` push and
pop that heap; leftover agents still receive Python `Message` /
`LimitOrder` objects. `message_id` / `order_id` assignment, numpy
`RandomState` draws, `pipeline_delay`, STP, and the partial-fill
snapshot are unchanged. GPU is unused.

**Family 1 14/14 exact.** as06, gb_mega, MP (`mp01`) and RA (`ra01`)
exact. Heap-order unit test matches `heapq` on
`(deliver_at, (sid, rid, message_id))`.

| Scenario | P5 | P6 | Step 1 best | vs Phase 5 |
|---|---|---|---|---|
| `as06_throughput_fast` | 119,608 | 129,295 | **138,985** | **1.16×** |
| `gb_mega_throughput` | 78,140 | 74,862 | **98,377** | **1.26×** |

Repeats: as06 130.8k–139.0k, gb_mega 95.9k–98.4k. Official B200 worker
will differ.

Post-step-1 cProfile (as06): `Kernel.run` still billed to the Python
caller (Cython loop). Next Python slices are extract, `order_executed`,
and leftover MM/Value `act()` (~3%). Agents are **not** the new wall —
do not rewrite them on this step.

## Championship step 2 (this change)

The remaining book wall after step 1 was Python `PriceLevel.peek` /
`pop` / `add_order` / `order_is_match` and `Side.is_bid()` called from
the already-compiled `execute_order` / `enter_order`. Step 2 inlines
those operations (same `_visible_qty` cache, same cheap-clone fill
snapshot) and compiles `cancel_order`. Agent-facing objects stay
Python `Message` / `LimitOrder`. No GPU. No agent rewrite.

**Family 1 14/14 exact.** as06, gb_mega, MP (`mp01`) and RA (`ra01`)
exact.

| Scenario | Step 1 | Step 2 best | vs Step 1 |
|---|---|---|---|
| `as06_throughput_fast` | 138,985 | **144,588** | **1.04×** |
| `gb_mega_throughput` | 98,377 | **103,585** | **1.05×** |

Repeats: as06 135.5k–144.6k (most runs sit in the step-1 band),
gb_mega 98.9k–103.6k. Official B200 worker will differ.

The PriceLevel Python bounce is gone. Leftover wall is still
`Kernel.run` billed to Python, extract, and agent callbacks. Agents
are **not** rewritten on this step.

## Championship step 3 (reverted)

Post-step-2 wall (this host): kernel ~80% of wall, `extract_trace`
~13%, message-ledger extract ~6–7%. One structural cut was attempted
and **reverted** — Family 1 did not stay exact.

**What was tried:** keep Python `Message` / `LimitOrder` only at the
agent `receive_message` / `place` boundary; put exchange hops on the
C heap as `(kind, payload, message_id)` (same `(deliver_at, sid, rid,
message_id)` key); write trace + message ledger as parallel columns
instead of per-row 10-tuples.

**Invariant that broke (s001):** in-hours fills / accepts / cancels
still matched, but the run emitted **+9 LimitOrderMsg**, **+4
CancelOrderMsg**, and **+13 MarketClosedMsg** after `mkt_close`.
`ORDER_SUBMITTED` grew 120→129. Wakeups and QuerySpread counts were
unchanged (84 / 74). The extra orders were a last MM/trader wave
whose acks were `MarketClosedMsg` — those rows are absent from the
reference ledger. Same extra-row pattern on `stp_cancel_newest` and
`partialfill_atomicity`. So the gate failed on **post-close
message-ledger composition / extra after-close submits**, not on
price-time fills.

No speed is claimed. Step 2 numbers stand.

**What a 10× from ~145k / ~104k would actually take:** kernel is
still ~80% after compiling the queue and the book. The leftover
kernel time is Python agents (`order_executed`, MM/Value `act`,
numpy `RandomState` draws) plus `Message`/`LimitOrder` construction
on the agent side of every hop. Extract is ~20% and is not 10× by
itself. A 10× (~1.4M / ~1.0M ev/s) needs **compiled Noise / MM /
Value / Momentum with a bit-exact `RandomState` stand-in**, and
only then a second pass at compact hops that does not let an extra
`act()` through after close. GPU still only for independent
`simulate-batch` scenarios.

## Championship step 4

Compile MarketMaker / ValueTrader / MomentumTrader `act()` (NoiseTrader
was already a Cython `noise_act`) and `TradingAgent.cancel_all_orders`.
Draws stay on `self.random_state` (`normal` / `randint`) and
`oracle.observe_price(..., random_state=self.random_state)` — no C RNG
stand-in. `sched_receive_message` still skips `act()` when
`mkt_closed`. Heap key, `message_id` / `order_id` construction order,
`pipeline_delay`, and STP are unchanged. **No compact-hop retry.**
No GPU.

**Family 1 14/14 exact**, including s001 event counts:
`ORDER_SUBMITTED` 120, wakeups 84, QuerySpread 74, **no
`MarketClosedMsg`**, no extra after-close LimitOrderMsg /
CancelOrderMsg. as06, gb_mega, MP (`mp01`) and RA (`ra01`) exact.

| Scenario | Step 2 best | Step 4 best | vs Step 2 |
|---|---|---|---|
| `as06_throughput_fast` | 144,588 | **140,921** | **0.97×** |
| `gb_mega_throughput` | 103,585 | **93,514** | **0.90×** |

Repeats: as06 116.6k–140.9k, gb_mega 74.9k–93.5k (this host was
noisier than the step-2 window). Official B200 worker will differ.
**No speed is claimed.** The gate was bit-exact Family 1, not a
throughput win. Compact hops stay off until after-close `act()` is
proven identical on a hop cut — this step did not retry that.

## Championship step 5

Retry the Step 3 compact internal-hop + columnar ledger cut now that
after-close `act()` is proven. Exchange hops carry
`(kind, payload, message_id)` on the same
`(deliver_at, sid, rid, message_id)` heap key. Python `Message` /
`LimitOrder` exist only at the agent boundary. Trace and message
ledger write parallel columns.

**The Step 3 bug:** `_materialize` hardcoded
`QuerySpreadResponse.mkt_closed = False`. After-close QueryMsg
(ABIDES allows the final spread) then looked open, so
`sched_receive_message` called `act()` and the next limit/cancel
wave hit `MarketClosedMsg`.

**The fix:** after-close QuerySpread stays on the compact path and
stores `mkt_closed = current_time > mkt_close` in the payload.
After-close limit / cancel / market still fall back to the original
`ExchangeAgent` (`MarketClosedMsg`). `sched_receive_message` still
refuses `act()` when `mkt_closed`. No further agent wrappers. No GPU.

**Family 1 14/14 exact.** s001 stays 120 `ORDER_SUBMITTED`, 84
wakeups, 74 QuerySpread, **0** `MarketClosedMsg`, 604 / 664 rows.
as06, gb_mega, MP (`mp01`) and RA (`ra01`) exact.

| Scenario | Step 2 best | Step 5 best | vs Step 2 |
|---|---|---|---|
| `as06_throughput_fast` | 144,588 | **163,541** | **1.13×** |
| `gb_mega_throughput` | 103,585 | **131,286** | **1.27×** |

Repeats: as06 150.8k–163.5k, gb_mega 109.8k–131.3k. Official B200
worker will differ. Both units cleared 10% vs Step 2 on this host.
That is not 10×. Stop coding on this step.

## Championship step 6

Incremental slice of the single-C-process cut: a Cython `MT19937`
that lock-steps `numpy.random.RandomState` (numpy 1.26 polar
`legacy_gauss` + masked `randint` via `next32`) and
`NoiseTrader.act` draws from that state after one `get_state()`
bind. Book, heap, and MM/Value/Momentum `act()` were already
compiled. Latency `lognormal` / `oracle.observe_price` still call
numpy. After-close rules stay Step 5. No GPU. No new agent wrappers.

`mt19937_matches_numpy()` is True (20 and 50 seeds). **Family 1
14/14 exact.** s001 stays 120 / 84 / 74 / 0 MarketClosedMsg /
604/664. as06, gb_mega, MP (`mp01`) and RA (`ra01`) exact.

| Scenario | Step 5 best | Step 6 best | vs Step 5 |
|---|---|---|---|
| `as06_throughput_fast` | 163,541 | **176,368** | **1.08×** |
| `gb_mega_throughput` | 131,286 | **137,687** | **1.05×** |

Repeats: as06 161.9k–176.4k, gb_mega 117.5k–137.7k. Official B200
worker will differ. Noise `RandomState` method calls are no longer
the wall. Remaining 10× is still one C process for latency /
`observe_price` / `LimitOrder` construction / extract, not more
Python agent wrappers.

## Championship step 7 (this change)

Remaining hot-path numpy RNG onto the same C `MT19937`, after
bit-identity vs numpy 1.26 `RandomState` on those sequences:

- latency `lognormal` = `exp(normal(mu, sigma))` (gauss cache shared)
- ValueTrader `observe_price` = `int(round(normal(r_t, sqrt(sigma_n))))`;
  `r_t` still comes from `oracle.observe_price(..., sigma_n=0)` so
  after-close lookup is unchanged

No new agent wrappers. After-close rules stay Step 5. No GPU.

`mt19937_matches_numpy()` now includes lognormal + observe_price
lock-step (True at 20×50 and 40×80). **Family 1 14/14 exact.**
s001 stays 120 / 84 / 74 / 0 MarketClosedMsg / 604/664. as06,
gb_mega, MP (`mp01`) and RA (`ra01`) exact.

| Scenario | Step 6 best | Step 7 best | vs Step 6 |
|---|---|---|---|
| `as06_throughput_fast` | 176,368 | **204,482** | **1.16×** |
| `gb_mega_throughput` | 137,687 | **159,530** | **1.16×** |

Repeats: as06 184.1k–204.5k, gb_mega 130.3k–159.5k. Official B200
worker will differ. Both units cleared 10% vs Step 6 on this host.
Hot-path numpy RNG is gone (uniform/pareto latency is unused on
these units). That is not 10×.

## Championship step 8 (this change)

C `Kernel.run` dispatch. No Python `Message` on the hop: wakeup /
act / exchange are called as Cython functions. Limit-order hops
carry field tuples; the agent keeps the original `LimitOrder` and
the exchange reconstructs a working copy, so `cheap_clone` is not
allocated per hop (fill snapshots stay — partial-fill semantics).
After-close rules stay Step 5. Step 7 C RNG stays. No GPU.

`mt19937_matches_numpy()` still True. **Family 1 14/14 exact.**
s001 stays 120 / 84 / 74 / 0 MarketClosedMsg / 604/664. as06,
gb_mega, MP (`mp01`) and RA (`ra01`) exact.

| Scenario | Step 7 best | Step 8 best | vs Step 7 |
|---|---|---|---|
| `as06_throughput_fast` | 204,482 | **222,357** | **1.09×** |
| `gb_mega_throughput` | 159,530 | **168,120** | **1.05×** |

Repeats: as06 199.4k–222.4k, gb_mega 136.0k–168.1k. Official B200
worker will differ. Incremental C dispatch is exact and a few
percent faster; it is not 10×.

## Championship step 9

C book + C ledger. `enter_order` / `execute_order` / `handle_limit`
store a compact `COrder` struct — no Python `LimitOrder` on the
book. Fill / accept / cancel snapshots stay (partial-fill
semantics) as primitive tuples, not `LimitOrder` objects. Trace and
message ledger write C arrays; pandas only at the final parquet
conversion. After-close rules stay Step 5. Step 7 C RNG stays.
No GPU.

`mt19937_matches_numpy()` still True. **Family 1 14/14 exact.**
s001 stays 120 / 84 / 74 / 0 MarketClosedMsg / 604/664. as06,
gb_mega, MP (`mp01`) and RA (`ra01`) exact.

| Scenario | Step 8 best | Step 9 best | vs Step 8 |
|---|---|---|---|
| `as06_throughput_fast` | 222,357 | **241,776** | **1.09×** |
| `gb_mega_throughput` | 168,120 | **206,425** | **1.23×** |

Repeats: as06 227.0k–241.8k, gb_mega 200.8k–206.4k. Official B200
worker will differ. gb_mega cleared 10%; as06 did not. Not 10×.

## Championship step 10

C Event hops + C agent order map. The hop payload is a C `Event`
with an embedded `COrder` and L2-1 fields — not a Python Event
plus tuple. Trading-agent working orders live in a `COrderMap`
keyed by `order_id`, not Python `LimitOrder`. Fill / accept /
cancel snapshots stay C tuples (partial-fill semantics). Pandas
only at the final parquet dump. `COrderMap.remove` keeps insertion
order so MarketMaker `cancel_all` matches Python `dict` sequence.
After-close rules stay Step 5. Step 7 C RNG stays. No GPU.

`mt19937_matches_numpy()` still True. **Family 1 14/14 exact.**
s001 stays 120 / 84 / 74 / 0 MarketClosedMsg / 604/664. as06,
gb_mega, MP (`mp01`) and RA (`ra01`) exact.

| Scenario | Step 9 best | Step 10 best | vs Step 9 |
|---|---|---|---|
| `as06_throughput_fast` | 241,776 | **263,427** | **1.09×** |
| `gb_mega_throughput` | 206,425 | **229,848** | **1.11×** |

Repeats: as06 239.9k–263.4k, gb_mega 208.4k–229.8k. Official B200
worker will differ. gb_mega cleared 10%; as06 did not. Not 10×.

## Championship step 11

From-scratch C kernel (`fast_sim._native.NativeSim`). It snapshots
params and RNG streams from `build_config`, then runs a C event
loop that never calls `Kernel.run`. Same heap key
`(deliver_at, sid, rid, message_id)`, same `message_id` /
`order_id` allocation, C `MT19937` bound to each agent's
`RandomState`, STP, `pipeline_delay`, and Step 5 after-close
(QuerySpread answered with `mkt_closed`; limit/cancel become
`MarketClosedMsg`). Close-price broadcast reuses one `message_id`
like stock `ExchangeAgent.wakeup`. Hybrid `_hotpath` is unchanged
and is the fallback (`FAST_SIM_NATIVE=0` or an unsupported
roster). No GPU.

`mt19937_matches_numpy()` still True. **Family 1 14/14 exact.**
s001 stays 120 / 84 / 74 / 0 MarketClosedMsg / 604/664. as06,
gb_mega, MP (`mp01`) and RA (`ra01`) exact. Hybrid fallback
still matches s001.

| Scenario | Step 10 best | Step 11 best | vs Step 10 |
|---|---|---|---|
| `as06_throughput_fast` | 263,427 | **526,419** | **2.00×** |
| `gb_mega_throughput` | 229,848 | **465,876** | **2.03×** |

Repeats: as06 459.3k–526.4k, gb_mega 447.5k–465.9k. Official B200
worker will differ. Both units cleared 10%. Not 10×.

## Championship step 12

Eat leftover Python inside native. Latency `uniform` / `pareto`
use C `MT19937` (`low + (high-low)*U`, `-log(1-U)*scale`,
`(1-U)^(-1/α)-1`); `native_rng_matches_numpy()` is True. Native
`result()` is a pyarrow Table; `simulate` writes snappy parquet
without a DataFrame copy. A C port of SparseMeanRevertingOracle
`observe_price` (OU + global `exponential` megashocks + scheduled
jumps) broke Family 1 row counts and was reverted — ValueTrader
still calls the Python oracle. Native stays default. Hybrid
fallback unchanged. No GPU.

`mt19937_matches_numpy()` still True. **Family 1 14/14 exact.**
s001 stays 120 / 84 / 74 / 0 MarketClosedMsg / 604/664. as06,
gb_mega, MP (`mp01`) and RA (`ra01`) exact.

| Scenario | Step 11 best | Step 12 best | vs Step 11 |
|---|---|---|---|
| `as06_throughput_fast` | 526,419 | **527,322** | **1.00×** |
| `gb_mega_throughput` | 465,876 | **462,285** | **0.99×** |

Repeats: as06 462.9k–527.3k, gb_mega 441.3k–462.3k. Official B200
worker will differ. Both units &lt;10% vs Step 11 (as06/gb_mega
are log-normal, already on C). Not 10×.

## Championship step 13 (this change)

Bit-exact C `observe_price` only. No parquet/Arrow change. The
Step 12 C port drifted (`stp_cancel_newest` 72018 vs 72048,
`ra01` 35610 vs 36431) because (1) 2021-ns timestamps (~1.6e18)
stored as `double` make `d = ts - pt` ≠ the integer subtraction
Python does when both times are ints (ulp ~256 ns), (2) megashock
sign was inverted vs `msv if randint(2)==0 else -msv`, (3)
`int(exponential)` overflowed 32-bit C `int` — now `floor` of a
positive draw, (4) C and Python must not share one RandomState.
`observe_many` matched the Python observation sequence on those
units (42/42 and 246/246) before `NATIVE_OBSERVE=1`. Native stays
default. Hybrid fallback unchanged. No GPU.

`mt19937_matches_numpy()` still True. `native_rng_matches_numpy()`
True (now includes `normal` at OU scales). **Family 1 14/14 exact.**
s001 stays 120 / 84 / 74 / 0 MarketClosedMsg / 604/664. as06,
gb_mega, MP (`mp01`) and RA (`ra01`) exact. Hybrid s001 exact.

| Scenario | Step 12 best | Step 13 best | vs Step 12 |
|---|---|---|---|
| `as06_throughput_fast` | 527,322 | **531,669** | **1.01×** |
| `gb_mega_throughput` | 462,285 | **480,889** | **1.04×** |

Repeats: as06 467.6k–531.7k, gb_mega 457.5k–480.9k. Official B200
worker will differ. Both units &lt;10% vs Step 12 (as06/gb_mega
have `jump_intensity=0`; C observe still drops the Python call).
Not 10×.

## Batch message ledger (this change)

`simulate-batch` already wrote a self-consistent in-memory ledger
(`t_recv - t_send == latency`, causal parent delivered before
child send). Native `result()` is a pyarrow Table; Step 12's
`pq.write_table` has no pandas metadata, so the official
`pd.read_parquet` (no `dtype_backend`) promotes nullable
`t_send_ns` to float64 and rounds ~1.6e18 ns times. Batch
`score_isolation` always runs `check_message_semantics` even
when the card says `requires_message_ledger = false`. Event
`trace.parquet` is unchanged (still raw Arrow). Message
ledgers go through pandas `Int64` so the parquet pandas
metadata restores integers. Hybrid already wrote DataFrames.
No GPU. Family 1 traces unchanged.

## Current championship path — 2026-09-15

The current execution plan is [Submission Readiness](../../docs/SUBMISSION-READINESS.md).
The tables above are historical local experiments; their best rates are not official
scores or directly comparable with the later amd64-container verification on macOS ARM.

Keep the exact CPU engine. First update packaging to toolkit v2.4.1 and team-claim 2.0,
then validate the complete public workload and measure end-to-end container time.
The official score averages per-unit rates over the entire evaluation roster, so
choose optimizations by their measured contribution to that mean. Profile startup,
Arrow/parquet output and batch scheduling before choosing the next implementation.
The earlier 1.4M / 1.0M targets do not establish a winning threshold. The large
exemplar and the organizer's production repeat-digest repair remain open readiness items.
