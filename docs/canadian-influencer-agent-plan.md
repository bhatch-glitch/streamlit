# Implementation Plan: Canadian Influencer Recruitment Agent

**Program:** Wayfair Canada Creator Program
**Codename:** `maple-scout` (the agent's operating persona is defined below)
**Owner:** Creator Partnerships
**Status:** Draft plan — pending approval on the open decisions in the final section
**Related systems:** CreatorIQ (existing `app.py` dashboard + `creatoriq/` pipeline), Playwright automation, MCP tooling

---

## 1. Overview

Build an autonomous AI agent that runs the top of the creator-recruitment funnel for the
Wayfair Canada Creator Program end-to-end, every business day:

1. **Discover** 50–100 candidate Canadian influencers per day across Instagram, TikTok, and YouTube.
2. **Vet** that each candidate is genuinely Canada-based (not just Canada-adjacent content).
3. **Enrich** each qualified candidate with a verified, business-appropriate email address.
4. **Qualify** candidates for brand fit, audience quality, and authenticity (fraud/bot screening).
5. **Reach out** with a personalized, on-brand, CASL-compliant email introducing the program, then run a bounded follow-up sequence.
6. **Log** every candidate and interaction into a system of record and hand accepted creators off to CreatorIQ.

The intent is not a brittle scraper-plus-mail-merge script. It is an **agent that reasons** — it makes
the kind of judgment calls a seasoned influencer-marketing manager makes, applies a consistent quality
bar, explains its decisions, and escalates edge cases to a human instead of guessing.

### What "extreme intelligence" means here (and what it does not)

- **It does:** apply expert heuristics for brand fit and audience quality, write genuinely personalized
  outreach that references the creator's actual content, detect engagement fraud, resolve ambiguous
  location signals, tier creators, and know when a candidate should be skipped or escalated.
- **It does not:** operate without guardrails. Volume caps, compliance rules, and a human approval gate
  are hard constraints the model cannot override. Intelligence is applied *inside* the guardrails, not
  as a replacement for them.

---

## 2. The Operator Persona — "Maple"

The agent operates under a stable persona so its judgment is consistent and reviewable. This persona is
encoded as the agent's system prompt and reflected in scoring rubrics and email tone.

> **Maple** — 28 years old, 5–7 years in influencer marketing. Ran creator programs for retail/home &
> lifestyle brands. Fluent in Canadian creator culture (knows the difference between a Toronto, Montréal,
> Vancouver, and Prairies audience; knows FR-CA matters in Québec). Has a sharp eye for fake engagement,
> a strong sense of brand fit, and writes outreach that sounds like a human who actually watched the
> creator's content — never a mail merge. Bilingual-aware (EN/FR-CA). Obsessive about not burning the
> brand's reputation or the domain's sender score. Would rather send 40 excellent, personalized emails
> than 100 generic ones.

**Judgment principles Maple applies (these become explicit rubric rules, not vibes):**

- Audience quality > raw follower count. A 15k-follower creator with 6% engagement and a real Canadian
  audience beats a 400k account with bought followers.
- Home/lifestyle/decor/DIY/organization/parenting/real-estate niches are the core; adjacent niches
  (food, small-space living, renter content) are in-scope with a fit note.
- Never contact a creator twice for the same program. Never contact anyone who unsubscribed. Never
  contact obvious minors or accounts that are clearly businesses/agencies unless they're a management
  contact for an in-scope creator.
- When location or fit is genuinely ambiguous, escalate to a human rather than send.

---

## 3. Architecture

### 3.1 Agentic loop

The agent runs a **plan → act → observe → reflect** loop per candidate, orchestrated by an LLM with
tool-calling. A daily "shift" orchestrator manages the batch, budget, and rate limits.

```
Daily Shift Orchestrator
  ├─ builds today's sourcing plan (niches, hashtags, geos, platforms) within volume budget
  ├─ for each candidate:
  │     Discovery tool → Geo-vetting tool → Enrichment tool → Qualification (LLM+rubric)
  │        → Personalization (LLM) → Compliance gate → [Human approval] → Send tool
  ├─ enforces caps (per-day, per-domain, per-platform) and dedupe against system of record
  └─ writes a shift report (candidates seen, qualified, contacted, escalated, skipped + reasons)
```

### 3.2 Tools (function-calling interface the LLM is given)

| Tool | Responsibility | Likely implementation |
|---|---|---|
| `discover_candidates` | Return candidate creators for a niche/hashtag/geo query | Platform APIs where available; Playwright-driven collection where not; optional 3rd-party discovery API |
| `fetch_profile` | Pull full profile + recent posts for a handle | Platform APIs / Playwright |
| `verify_location` | Score how likely the creator is Canada-based | Multi-signal resolver (see §4.2) |
| `find_email` | Find + verify a contact email | Bio/link parsing first; enrichment API (e.g. Hunter/Apollo) as fallback; MX/SMTP verification |
| `score_candidate` | Return brand-fit + audience-quality + authenticity scores | LLM against the Maple rubric + computed metrics |
| `draft_outreach` | Produce a personalized EN or FR-CA email | LLM using the profile + template + program facts |
| `send_email` | Send via the transactional/ESP provider | ESP API (must support CASL headers + unsubscribe) |
| `log_event` | Persist candidate + every decision/interaction | System of record (see §3.4) |
| `escalate` | Queue an item for human review | Review queue / Slack |

Every tool call and every LLM decision is logged with its rationale so a human can audit *why* a
candidate was contacted, skipped, or escalated.

### 3.3 Memory

- **Working memory:** the current candidate's profile, signals, and scores within one loop.
- **Long-term memory / system of record:** the candidate database — every creator ever seen, their
  status, contact history, unsubscribe flags, and outcomes. This is the dedupe and compliance backbone.
- **Semantic memory (optional, phase 3+):** embeddings of past accepted vs. rejected creators to sharpen
  the fit model over time.

### 3.4 System of record (data model)

A `creators` store (start with SQLite/CSV to match the current repo's lightweight footprint, with a clear
path to Postgres) with at least:

- Identity: platform handles, display name, profile URLs, follower counts, primary niche.
- Location: resolved country/province/city, confidence score, and the raw signals used.
- Contact: email, email verification status/source, `consent_status`, `unsubscribed_at`.
- Scoring: brand_fit, audience_quality, authenticity/fraud score, tier (A/B/C), language (EN/FR-CA).
- Lifecycle: `status` (discovered → vetted → enriched → qualified → queued → contacted → replied →
  accepted/declined/unsubscribed/suppressed), timestamps, and the agent's rationale at each step.
- Outreach: template/variant used, send timestamps, follow-up count, reply text, bounce/complaint flags.

---

## 4. The Daily Pipeline (5 stages)

### 4.1 Stage 1 — Discovery (sourcing 50–100/day)

- Maintain a **sourcing playbook**: prioritized niches × Canadian geo-signals × platforms, with weekly
  rotation so we don't re-scrape the same pool.
- Canadian discovery seeds: geo hashtags and place tags (`#torontocreator`, `#vancouvermom`,
  `#montrealhome`, `#canadianhomedecor`, `#yyc`, `#yeg`, city/place tags), audience-lookalikes of already
  accepted creators, and "creators who tagged Canadian retailers/home brands."
- **Prefer official/permitted access paths.** Use platform APIs and permitted discovery vendors first.
  Where automated browsing is used, it is rate-limited, respects ToS boundaries, and is treated as a
  fallback (see §6 on legal/ToS).
- Output: a raw candidate list, deduped against the system of record before any further work is spent.

### 4.2 Stage 2 — Canadian geo-vetting

No single signal is trusted. `verify_location` combines and weights:

- Explicit signals: profile "location" field, "📍 Toronto" in bio, `.ca` links, province/city hashtags,
  language (FR-CA is a strong Québec signal), currency/spelling ("colour", "$CAD").
- Content signals: recognizable Canadian locations, retailers, seasons/events, tagged Canadian places.
- Audience signals (when available via API/insights): follower geo-distribution.
- The LLM resolves conflicts and returns a **confidence score + explanation**. Rules:
  - High confidence Canada → proceed.
  - Ambiguous / conflicting → **escalate to human**, do not send.
  - Confidently non-Canadian → skip with reason.

### 4.3 Stage 3 — Email enrichment

- Order of preference: (1) email in bio / linktree / "business inquiries" field, (2) email on a linked
  website/press page, (3) enrichment API fallback.
- **Verify before use:** syntax → MX record → SMTP/verification-API check. Never send to unverified or
  role-noise addresses that don't fit (e.g. generic `info@` gets a lower priority than a management/PR
  contact).
- If no acceptable email is found → mark `enrichment_failed` and (optionally) queue an on-platform DM
  path for a later phase, or escalate. Do not fabricate addresses.

### 4.4 Stage 4 — Qualification & scoring

`score_candidate` produces three scores plus a tier, using computed metrics + the Maple rubric:

- **Brand fit:** niche alignment with Wayfair Canada (home, decor, DIY, organization, lifestyle, parenting).
- **Audience quality:** engagement rate vs. follower band, comment authenticity, posting cadence, content quality.
- **Authenticity / fraud:** follower-growth anomalies, engagement-pod signatures, comment quality, bot ratio.
- Output: tier A/B/C + a short human-readable rationale. C-tier and fraud-flagged candidates are
  suppressed or escalated rather than contacted.

### 4.5 Stage 5 — Personalized outreach + follow-ups

- `draft_outreach` writes a genuinely personalized email: references specific recent content, states why
  they fit Wayfair Canada, explains the program (perks, commission/gifting, expectations), and gives a
  single clear CTA. **Language auto-selected** (EN or FR-CA).
- Templates are **structured with personalization slots**, not free-form each time — this keeps tone
  on-brand and reviewable while the personalization stays authentic.
- **Follow-up cadence:** at most 1–2 spaced follow-ups, auto-stopped on any reply, bounce, or unsubscribe.
- Every send passes the **compliance gate** (§5) and, in early phases, a **human approval gate** before
  transmission.

---

## 5. CASL Compliance (non-negotiable)

Cold commercial email to Canadians is governed by **CASL (Canada's Anti-Spam Legislation)** — penalties
are severe (up to CAD $10M per violation). A 5–7-year influencer-marketing pro treats this as a hard
constraint, so it is enforced in code, not left to the model:

- **Sender identification:** every email clearly identifies Wayfair/the program and includes a valid
  physical mailing address and a real way to contact the sender.
- **Unsubscribe:** a working, no-cost, ≤10-business-day-honored unsubscribe mechanism in every message;
  unsubscribes are written to the system of record and **globally suppress** future contact.
- **Consent basis:** cold outreach relies on CASL's limited exceptions (e.g. conspicuously published
  business contact email relevant to the recipient's role) — the agent records the **consent basis and
  evidence** for each send, and only uses publicly/conspicuously published business/PR contacts.
- **Suppression list:** unsubscribes, complaints, hard bounces, and "do not contact" are permanent.
- **No dark patterns, no misleading subject/sender lines.**
- **Legal sign-off** on templates and consent logic is a launch gate (see open questions).

> This section should be reviewed by legal/privacy counsel before the first real send. The plan assumes
> that review happens during Phase 0.

---

## 6. Platform ToS, deliverability & anti-abuse

- **Respect platform Terms of Service.** Prefer official APIs and sanctioned discovery vendors; treat
  browser automation as a rate-limited fallback and keep it within ToS boundaries. Flag any approach that
  requires ToS-violating scraping for explicit human decision rather than doing it silently.
- **Deliverability / domain reputation:** consider a dedicated outreach subdomain, proper SPF/DKIM/DMARC,
  gradual send-volume warmup, and monitoring of bounce/complaint rates. Blowing up the domain's sender
  score is treated as a Sev-1.
- **Rate limits & caps:** hard daily cap (50–100 contacted), per-domain throttling, and human-in-the-loop
  in early phases so nothing goes out at scale before quality is proven.
- **PIPEDA / privacy:** store only necessary contact data, document retention, and honor deletion requests.

---

## 7. Tech stack & integration with the existing repo

Reuse what's already here to minimize new surface area:

- **Language/runtime:** Python (matches `app.py`, `requirements.txt`).
- **Browser automation:** Playwright (already a dependency) for fallback discovery/profile fetch.
- **Agent orchestration:** LLM with tool-calling; MCP (already a dependency) is a natural fit for exposing
  the tools in §3.2. Add a lightweight agent loop / orchestration layer.
- **Storage:** start with SQLite/CSV under a new `agent/` package alongside `creatoriq/`; graduate to
  Postgres if volume warrants.
- **Email/ESP:** a transactional provider that supports custom headers, list-unsubscribe, bounce/complaint
  webhooks (e.g. an ESP the org already uses).
- **UI:** extend the existing Streamlit app with a **"Recruitment" dashboard** — daily shift report,
  approval queue, escalations, funnel metrics — reusing the current Looker-styled theme.
- **CreatorIQ handoff:** accepted creators flow into CreatorIQ (the system the current dashboard already
  tracks), closing the loop from recruitment → activation → performance.

Proposed layout:

```
agent/
  persona.py          # Maple system prompt + rubric constants
  orchestrator.py     # daily shift loop, budget, caps, dedupe
  tools/
    discovery.py  location.py  enrichment.py  scoring.py  outreach.py  email_send.py
  compliance.py       # CASL gate + suppression list
  store.py            # system of record (SQLite/CSV -> Postgres)
  reports.py          # shift report + metrics
scripts/
  run_shift.py        # entrypoint (cron/scheduled)
docs/
  canadian-influencer-agent-plan.md   # this document
```

---

## 8. Phased implementation roadmap

Phases are ordered by dependency and risk, not calendar time. Each phase is independently shippable and gated.

### Phase 0 — Foundations & compliance guardrails
- Legal/privacy review of the CASL approach; approve consent basis + templates.
- Stand up the system of record (data model in §3.4) and the global suppression list.
- Set up the ESP: domain/subdomain, SPF/DKIM/DMARC, unsubscribe + bounce/complaint webhooks.
- Encode the Maple persona + scoring rubric.
- **Exit gate:** counsel sign-off on compliance; suppression + unsubscribe verifiably working end-to-end.

### Phase 1 — Discovery + geo-vetting (read-only, no sending)
- Implement `discover_candidates`, `fetch_profile`, `verify_location`, `log_event`.
- Run daily to produce a **vetted Canadian candidate list** with confidence + rationale — no emails yet.
- **Exit gate:** on a human-reviewed sample, ≥90% of "high-confidence Canada" picks are actually Canadian.

### Phase 2 — Enrichment + qualification
- Implement `find_email` (with verification) and `score_candidate` (fit/quality/fraud + tiering).
- Produce a daily **qualified + enriched** shortlist. Still no automated sending.
- **Exit gate:** email verification pass-rate and scoring quality validated against human spot-checks.

### Phase 3 — Outreach with human-in-the-loop
- Implement `draft_outreach`, `send_email`, the CASL compliance gate, and follow-up logic.
- **Every email requires human approval** in the Streamlit approval queue before sending.
- Start with a small daily volume; warm up the sending domain.
- **Exit gate:** approval-acceptance rate high, bounce/complaint rates within thresholds, replies logged.

### Phase 4 — Supervised autonomy & scale
- Auto-send A-tier, high-confidence, clearly-compliant candidates; keep humans on B/C-tier and all edge
  cases. Ramp to the full 50–100/day cap.
- Add EN/FR-CA auto-selection, follow-up sequences, and reply triage.
- Wire accepted creators into CreatorIQ; surface the full funnel in the Recruitment dashboard.
- **Exit gate:** stable KPIs (§9), no compliance incidents, positive reply/acceptance trend.

### Phase 5 — Continuous improvement (optional)
- Feed accepted-vs-rejected outcomes back into the fit model; A/B test templates; expand niches/geos.

---

## 9. KPIs & guardrail metrics

**Funnel/volume:** candidates discovered/day, % passing geo-vet, % enriched with verified email,
% qualified (by tier), emails sent/day (must respect cap), reply rate, positive-reply rate,
program-acceptance rate, cost per accepted creator.

**Guardrail (any breach pauses sending):** bounce rate, spam-complaint rate, unsubscribe rate,
domain reputation, geo-vet precision (human-audited), and personalization quality (human-audited).

---

## 10. Risks & mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| CASL violation | Severe fines, legal exposure | Compliance gate in code, counsel sign-off, permanent suppression list, human approval in early phases |
| Domain reputation damage | Emails land in spam; brand harm | Dedicated subdomain, SPF/DKIM/DMARC, warmup, bounce/complaint monitoring, hard caps |
| Platform ToS violations / blocks | Data source loss, legal risk | Prefer official APIs/vendors; rate-limit; escalate ToS-risky steps to humans |
| False-positive "Canadian" | Wasted outreach, off-target | Multi-signal resolver + confidence threshold + human escalation on ambiguity |
| Bad email data | Bounces hurt deliverability | Multi-step verification before send; suppress on bounce |
| Generic/off-brand outreach | Low reply rate, brand harm | Persona + structured personalization + human approval gate + quality audits |
| Over-automation early | Scaled mistakes | Phased rollout; sending is human-gated until quality is proven |
| Engagement fraud slips through | Poor program ROI | Authenticity/fraud scoring; tiering; human review of high-value picks |

---

## 11. Open questions / decisions needed

1. **Legal/consent:** Which CASL exemption(s) are we relying on for cold B2B outreach, and who signs off?
   What physical mailing address and sender identity go in the footer?
2. **Discovery data source:** Approved to use official platform APIs and/or a paid discovery vendor
   (which one?), or is Playwright fallback the primary path? This drives ToS risk and cost.
3. **Enrichment vendor:** Which email-finding/verification provider (Hunter, Apollo, other) is approved?
4. **ESP:** Which sending provider, and can we use a dedicated subdomain for warmup?
5. **Program terms to state in the email:** commission/gifting structure, expectations, and CTA/landing page.
6. **Autonomy threshold:** at what tier/confidence are we comfortable auto-sending vs. requiring approval?
7. **Bilingual scope:** is FR-CA outreach in scope for launch, and who reviews FR-CA copy?
8. **Storage/scale:** stay on SQLite/CSV to match the current repo, or provision Postgres from the start?

---

*This plan is intentionally guardrail-first: the agent is smart, but volume caps, CASL compliance, and a
human approval gate are hard constraints it operates within. Ship the phases in order; each one is
independently useful and de-risks the next.*
