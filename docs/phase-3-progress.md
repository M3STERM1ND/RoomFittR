# Phase 3 progress

Live status for `implementation-plan.md` §8 Phase 3 (Furniture Catalog
Pipeline). Updated as work lands. **If work stopped partway, the "Stopped
at" line below is the resume point.**

**Stopped at:** _**The two pure stages are done, 2026-09-23.** §4.5's
dimension parser and §4.7's acceptance gate need no network, no retailer
decision and no credentials, so they were built first — and the dimension
parser is the R7 mitigation, which makes it the highest-value thing in this
phase regardless of ordering. Everything else waits on **D7–D9**._

---

## Task status

| Task (from the plan) | Status | Notes |
|---|---|---|
| Retailer selection and crawl policy (D7–D9) | **blocked — human** | A legal and commercial decision, not a coding one. Nothing downstream can start without it. |
| §4.3 Discover / crawl | **not started** | Needs D7–D9: what may be crawled, how fast, and under what terms. |
| §4.4 Extract (JSON-LD, selectors, Haiku fallback) | **not started** | The selector configs are per-retailer, so they need the retailers. |
| **§4.5 Dimension parser** | **done** | `workers/catalog/roomfittr_catalog/dimensions.py`. Every format §4.5 enumerates. |
| **§4.5 Parser corpus** | **partial** | 146 cases in `fixtures/dimensions/corpus.json`, authored from §4.5's own examples. §4.5 asks for **300+ real strings**; see below. |
| §4.6 Normalize | **partial** | Units → integer mm and the axis semantics live in the parser. Price normalisation, category mapping, style/colour tagging and the SigLIP embedding all need products to normalise. |
| **§4.7 Validate** | **done** | `validate.py` + `product_rules.yaml`. Every row of §4.7's table, with the status it specifies. |
| §4.8 Deduplication | **not started** | Needs a catalog to find duplicates in. The schema half exists (`duplicate_group_id`, and `layout_candidates` already offers only a group's representative). |
| §4.9–4.11 refresh, admin, `layout_candidates` | **schema only** | The materialized view exists and is indexed; the crawl and refresh crons that feed it do not. |

---

## Definition of done (from the plan)

| Clause | State |
|---|---|
| Audit thresholds met | ❌ needs a catalog to audit |
| ≥ 40 active products in each of the 12 core categories | ❌ needs the crawler |
| Nightly refresh running unattended for a week | ❌ needs the crawler and Modal |

**Phase 3 has barely started**, and that is a scheduling fact rather than a
technical one: it is gated on a decision only a person can make.

---

## The corpus, stated plainly

§4.5 asks for the parser to be "unit-tested against a corpus of **300+ real
strings collected in Phase 3**". What exists is **146 cases written from
§4.5's own examples**, crossed with the units and spellings retailers use.

That is not the same thing and the files say so — `_build.py`'s docstring,
the JSON's `note` field, and a test that fails if the note is removed. The
value of 300 real strings is precisely the formats nobody thought of: the
retailer who writes `Dims: 84x36x33`, the one who puts height first without
saying so, the one whose spec table has two "Width" rows. A generated corpus
covers what we already knew to handle and by construction covers nothing
else.

When the crawler exists, append real strings to `corpus.json` and delete
nothing. A generated case that a real string contradicts is a case that was
wrong.

---

## What was found while building

1. **The corpus found four bugs in the parser it was written for**, which is
   the entire argument for having one:
   - The unit alternation was shortest-first, so `84 inchW` matched "in" and
     left "ch" in front of the axis letter. The W was never seen, the set
     silently went unlabelled, and its confidence dropped — a wrong answer
     that looked like a cautious one.
   - `\b` cannot match `Ø`, so the diameter sign was in the word table and
     unreachable from the pattern.
   - Splitting candidate sets on "two or more spaces before a capital" turned
     one line reading `W: 84"  D: 36"  H: 33"` into three sets of a single
     measurement and resolved it to nothing.
   - Mapping length onto width unconditionally made `W 160 cm x L 230 cm` set
     the width twice, contradict itself, and reject a perfectly clear rug.

2. **I invented the `rejected_*` status names.** The catalog migration had
   `rejected_dimensions`, `rejected_category`, `rejected_duplicate`,
   `rejected_policy`; §4.7 names five different ones. Corrected in the
   original migration, which has never been applied outside a test container.
   The lesson is narrow and worth keeping: §6.3 writes the status column as
   `rejected_*` and it is tempting to read that as "any reason you like", but
   §4.7 three sections earlier enumerates them.

3. **A component-only dimension set must be refused, not accepted.** A seat,
   a tabletop or a drawer interior is never the product's size, and recording
   it is R7 with the numbers read perfectly. Accepting it at low confidence
   would have been the softer-looking and worse choice.

## Deliberate decisions, recorded at the code

- **`convention` is a required argument of `parse()` with no default.**
  Nothing in `213 x 91 x 84 cm` says whether 91 is the depth or the height;
  §4.4 puts that in the retailer config. A default would silently transpose
  two axes for every retailer who disagreed with it.
- **When in doubt, return nothing.** A `None` costs a Haiku call (§4.5 step
  3) or a rejected product; a wrong answer costs someone a sofa that does not
  fit. This is the opposite of the rule everywhere else in the repo, and it
  is why the corpus has as many refusals as it does.
- **The validation gate takes network answers as arguments.** Whether a URL
  resolves without an off-domain redirect and whether an image returns
  `image/*` are facts the crawler already has. Passing them in keeps the gate
  testable offline and stops a briefly-unreachable retailer reclassifying its
  whole catalog.
- **Non-USD is `pending_review`, not rejected.** §4.6 excludes it from V1's
  single market; it is a good product in the wrong market, and rejecting it
  would lose it when V1 stops being single-market.

---

## What only you can do

1. **D7–D9: which retailers, under what terms.** §9.4 treats the crawl policy
   as a legal record, not a configuration value — the `retailers.crawl_policy`
   column exists to hold the robots notes and the ToS decision next to the
   retailer they apply to. Everything else in this phase is downstream of it.
2. **An affiliate decision**, if any, since it changes the outbound link
   shape and therefore §4.7's off-domain-redirect check.
3. **Collecting the 300+ real dimension strings**, which happens naturally as
   a by-product of the first crawl — but only once there is one.
