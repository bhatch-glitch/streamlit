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
  │     Discovery → Program-membership screen (Creator Connect) → Geo-vetting → Enrichment
  │        → Qualification (LLM+rubric) → Personalization (LLM)
  │        → Compliance gate (re-checks Creator Connect) → [Human approval] → Send tool
  ├─ enforces caps (per-day, per-domain, per-platform) and dedupe against system of record
  └─ writes a shift report (candidates seen, qualified, contacted, escalated, skipped + reasons)
```

### 3.2 Tools (function-calling interface the LLM is given)

| Tool | Responsibility | Likely implementation |
|---|---|---|
| `discover_candidates` | Return candidate creators for a niche/geo/brand-mention query | **CreatorIQ Discovery API (preferred)**; else platform APIs / permitted discovery vendors; Playwright as last resort (see §4.1) |
| `fetch_profile` | Pull full profile + recent posts for a handle | Platform APIs / Playwright |
| `check_program_membership` | Check a handle/email against the active-participant roster | **Creator Connect** Apps Script Web App endpoint, or Google Sheets API on the roster sheet (see §4.2) |
| `verify_location` | Score how likely the creator is Canada-based | Multi-signal resolver (see §4.3) |
| `find_email` | Find + verify a contact email | Bio/link parsing first; enrichment API (e.g. Hunter/Apollo) as fallback; MX/SMTP verification |
| `score_candidate` | Return brand-fit + audience-quality + authenticity scores | LLM against the Maple rubric + computed metrics |
| `draft_outreach` | Produce a personalized EN or FR-CA email | LLM using the profile + template + program facts |
| `send_email` | Auto-send from `influencers@wayfair.ca` | Google Workspace/Gmail API or transactional ESP; must support the `wayfair.ca` sender, CASL headers + List-Unsubscribe |
| `log_event` | Persist candidate + every decision/interaction | System of record (see §3.4) |
| `escalate` | Queue an item for human review | Review queue / Slack |

Every tool call and every LLM decision is logged with its rationale so a human can audit *why* a
candidate was contacted, skipped, or escalated. In practice most of these tools are implemented as **n8n
workflows** that hold the credentials and perform the external calls — see §3.5.

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
- Program membership: `program_status` (not_a_member / active_member / former_member / unknown),
  `creator_connect_matched_on` (handle/email/name), `creator_connect_row_ref`, `membership_checked_at`.
- Scoring: brand_fit, audience_quality, authenticity/fraud score, tier (A/B/C), language (EN/FR-CA).
- Lifecycle: `status` (discovered → vetted → enriched → qualified → queued → contacted → replied →
  accepted/declined/unsubscribed/suppressed), timestamps, and the agent's rationale at each step.
- Outreach: template/variant used, send timestamps, follow-up count, reply text, bounce/complaint flags.

### 3.5 Integration & credentials layer — n8n

Rather than the agent holding raw API keys and OAuth tokens for every external system, **n8n is the
integration and credentials layer**. Access is granted by connecting each system's credentials **once**
in n8n, and each external action becomes an n8n **workflow** the agent triggers. This is the cleanest way
to "grant access" for this project because:

- **Centralized, revocable credentials.** Google (Sheets for Creator Connect, Gmail for
  `influencers@wayfair.ca`), CreatorIQ HTTP API, the enrichment vendor, and the ESP are all authorized in
  n8n's credential store — not scattered through the agent's codebase. Access can be granted/revoked in
  one place without redeploying the agent.
- **Workflows as tools.** Each agent tool in §3.2 maps to an n8n workflow exposed via webhook (or invoked
  through the n8n MCP server): `discover_candidates` → CreatorIQ HTTP node, `check_program_membership` →
  Google Sheets/Apps Script node on the Creator Connect sheet, `find_email` → enrichment HTTP node,
  `send_email` → Gmail node sending as `influencers@wayfair.ca`, `log_event` → append to the store.
- **Scheduling built in.** The daily "shift" can be a cron/schedule trigger in n8n, or the agent can drive
  it and call n8n workflows per candidate.
- **Already available here.** Two n8n MCP servers are configured for this workspace
  (`Wf-plats-n8n-staging`, `Wf-plats-n8n-prod`), so the agent can invoke n8n workflows directly via MCP.

> **Current status:** at time of writing, both n8n MCP servers fail live tool discovery (connection needs
> fixing) — a Phase 0 setup item. The design does not depend on which transport is used: the agent can
> call n8n via MCP or via plain webhook URLs.

**Division of labor:** n8n handles *access and execution* (auth, API calls, sending, sheet reads). The
agent handles *reasoning* (geo-vetting judgment, scoring, personalization, escalation decisions). Keep
CASL enforcement and the active-member re-check in the agent's compliance gate (or a dedicated n8n
workflow the agent must call), so guardrails aren't bypassable by a raw send node.

---

## 4. The Daily Pipeline (6 stages)

### 4.1 Stage 1 — Discovery (sourcing 50–100/day)

**Primary source — CreatorIQ Discovery (preferred).** We're already on CreatorIQ, and its Discovery
product is purpose-built for exactly this: search a database of millions of creators by brand
name/keywords and refine with filters for **location (Canada / province), audience demographics,
engagement quality, content style, and brand affinity**, with a feed of recent **brand mentions** (e.g.
creators already talking about Wayfair / home & decor). Using CreatorIQ as the source is the cleanest
option because:

- Canadian geo-filtering and audience-demographic filters are built in (helps Stage 3 geo-vetting).
- "Creators mentioning Wayfair / competitors / home-decor keywords" is a high-intent seed list.
- It stays inside the platform the program already runs on, so accepted creators flow straight through.

**Access caveat:** CreatorIQ's API is a private, enterprise GraphQL/REST interface, and *programmatic*
access to Discovery search specifically is contract-dependent — it is **not guaranteed to be exposed via
the API on every plan**. Two implementation modes depending on what the CreatorIQ contract allows
(confirm with the CreatorIQ account rep — see open questions):

1. **CreatorIQ Discovery API (fully automated):** the agent queries Discovery directly (location=Canada +
   niche/keyword/brand-mention filters), pulls candidate lists, and ingests them. This is the target.
2. **CreatorIQ Discovery UI → export → ingest (semi-automated):** if Discovery isn't API-accessible, a
   human runs saved Discovery searches and exports lists (or Playwright drives the UI within ToS); the
   agent ingests the export and takes over from screening onward.

**Fallback / supplementary sources** (used if CreatorIQ Discovery is unavailable via API, or to widen the
top of funnel):

- **Platform APIs** (Meta/Instagram, TikTok, YouTube Data) for hashtag/keyword/geo discovery.
- **Permitted third-party discovery vendors** (e.g. Modash / HypeAuditor-class tools) with Canada +
  audience-geo filters.
- **Canadian discovery seeds:** geo hashtags and place tags (`#torontocreator`, `#vancouvermom`,
  `#montrealhome`, `#canadianhomedecor`, `#yyc`, `#yeg`), audience-lookalikes of already-accepted
  creators, and creators who tag Canadian retailers/home brands.
- **Playwright** browsing is a rate-limited last resort, kept within ToS boundaries (see §6).

Output: a raw candidate list, deduped against the system of record before any further work is spent.

### 4.2 Stage 2 — Existing-member & prior-contact screening (via Creator Connect)

**Recruitment is signup-oriented — we must never email a creator who is already an active participant
in the Wayfair Canada Creator Program, nor anyone we've already contacted or suppressed.** This screen
runs immediately after discovery and *before* any effort is spent on geo-vetting, enrichment, or scoring,
so we never waste budget (or risk annoying an existing partner) on someone already in the program.

**Source of truth — Creator Connect.** The active-participant roster lives in **Creator Connect**, the
existing Google Apps Script (Google Sheets–backed) system that manages the program's creators. The
backing spreadsheet is:

> `https://docs.google.com/spreadsheets/d/1_OOr2bqbVbCHytMw6qbrNqiFrEunXiv5wH38oLYvwH4/edit`
> (sheet ID `1_OOr2bqbVbCHytMw6qbrNqiFrEunXiv5wH38oLYvwH4`)

The agent treats Creator Connect as the authoritative membership list and reuses/extends that existing
code rather than duplicating the roster. Two supported integration paths (pick per the current script's
shape):

- **Apps Script Web App endpoint (preferred):** extend the existing Creator Connect script with a
  read-only `doGet`/`doPost` handler (e.g. `?action=lookup&handles=...`) deployed as a Web App, returning
  each handle's membership status. The agent calls this endpoint to check candidates in batches. Auth via
  a shared token/service credential. This reuses Creator Connect's own view of the sheet (including any
  normalization/columns it already understands) instead of re-implementing them.
- **Google Sheets API (fallback):** read the roster sheet directly via the Sheets API using a service
  account that has been granted view access.
- **Via n8n (recommended for access grant):** connect Google in n8n once and expose a "membership lookup"
  workflow (Google Sheets / Apps Script node) that the agent calls — this is the simplest way to grant the
  agent read access to Creator Connect without handling Google credentials directly (see §3.5).

> **Access note:** the spreadsheet above is currently link-restricted (not world-readable — an
> unauthenticated fetch returns HTTP 401). Whichever path is chosen, it needs credentials: share the sheet
> with a **service account** (Sheets API path) or deploy the **Creator Connect Web App** with an access
> token the agent can use. The exact column names (handles per platform, email, status/active flag) still
> need to be confirmed against the live sheet — see open questions.

**Matching logic** (a membership match must be robust, not just exact-string):

- Normalize and match on platform handles (case-insensitive, strip `@`/URLs), known email, and
  display-name + platform as a secondary signal.
- Account for handle changes and multi-platform creators — a creator active on one platform is still an
  existing member even if discovered on another. Where Creator Connect stores cross-platform identity,
  use it; otherwise fuzzy-match and **escalate ambiguous matches to a human** rather than risk emailing an
  active partner.

**Outcomes:**

- **Active participant** → mark `already_in_program`, suppress, and skip (log the Creator Connect match).
- **Previously contacted / unsubscribed / suppressed** (from our own system of record) → skip with reason.
- **Former/inactive participant** → route per program policy (default: **escalate**, since re-recruiting a
  churned creator is a judgment call, not a cold-outreach decision).
- **No match** → proceed to geo-vetting.
- **Lookup error / Creator Connect unreachable** → **fail closed**: do not send. Hold the candidate and
  retry/escalate rather than assuming "not a member."

This check is also mirrored as a **final pre-send guard** in the compliance gate (§5): re-verify against
Creator Connect at send time so roster changes between discovery and send can't slip an active member into
an outreach batch.

### 4.3 Stage 3 — Canadian geo-vetting

No single signal is trusted. `verify_location` combines and weights:

- Explicit signals: profile "location" field, "📍 Toronto" in bio, `.ca` links, province/city hashtags,
  language (FR-CA is a strong Québec signal), currency/spelling ("colour", "$CAD").
- Content signals: recognizable Canadian locations, retailers, seasons/events, tagged Canadian places.
- Audience signals (when available via API/insights): follower geo-distribution.
- The LLM resolves conflicts and returns a **confidence score + explanation**. Rules:
  - High confidence Canada → proceed.
  - Ambiguous / conflicting → **escalate to human**, do not send.
  - Confidently non-Canadian → skip with reason.

### 4.4 Stage 4 — Email enrichment

- Order of preference: (1) email in bio / linktree / "business inquiries" field, (2) email on a linked
  website/press page, (3) enrichment API fallback.
- **Verify before use:** syntax → MX record → SMTP/verification-API check. Never send to unverified or
  role-noise addresses that don't fit (e.g. generic `info@` gets a lower priority than a management/PR
  contact).
- If no acceptable email is found → mark `enrichment_failed` and (optionally) queue an on-platform DM
  path for a later phase, or escalate. Do not fabricate addresses.

### 4.5 Stage 5 — Qualification & scoring

`score_candidate` produces three scores plus a tier, using computed metrics + the Maple rubric:

- **Brand fit:** niche alignment with Wayfair Canada (home, decor, DIY, organization, lifestyle, parenting).
- **Audience quality:** engagement rate vs. follower band, comment authenticity, posting cadence, content quality.
- **Authenticity / fraud:** follower-growth anomalies, engagement-pod signatures, comment quality, bot ratio.
- Output: tier A/B/C + a short human-readable rationale. C-tier and fraud-flagged candidates are
  suppressed or escalated rather than contacted.

### 4.6 Stage 6 — Personalized outreach + follow-ups (automatic send)

This is fully automated: once a candidate clears screening, geo-vetting, enrichment, qualification, and
the compliance gate, the agent **sends the email automatically** — no manual mail-merge step.

- **Sent from `influencers@wayfair.ca`.** All outreach comes directly from this address, so replies land
  in that inbox and the sender identity matches the program. Requires the `wayfair.ca` domain to
  authorize the sending service and publish SPF/DKIM/DMARC, and the mailbox/alias to exist (see setup
  requirements below and open questions).
- **Proper-name personalization.** The email greets the creator by their real first name, resolved by
  `draft_outreach` from the best available source (profile display name → name in bio/"business" field →
  CreatorIQ profile). A **safe-fallback rule** handles missing/ambiguous names: if a confident first name
  can't be parsed (handle-only accounts, brand/business names), use a warm neutral greeting rather than a
  wrong or awkward one (never "Hi @handle" or "Hi undefined"), and flag low-confidence names for review.
- **Everything needed to sign up.** The body explains the Wayfair Canada Creator Program (perks,
  commission/gifting, expectations), references specific recent content so it reads as human-written, and
  contains a single clear CTA — a **direct link to the signup/onboarding page** — plus any info the
  creator needs to join (what to do next, what to expect). **Language auto-selected** (EN or FR-CA).
- **Templated, not robotic.** Templates are **structured with personalization slots** (name, content
  reference, niche-specific hook, signup link), so tone stays on-brand and reviewable while the
  personalization stays authentic. Supports A/B variants.
- **Follow-up cadence:** at most 1–2 spaced follow-ups, auto-stopped on any reply, bounce, or unsubscribe.
- **Every send passes the compliance gate** (§5): CASL footer (sender ID + `wayfair.ca` mailing address +
  working unsubscribe), suppression check, and the active-member re-check against Creator Connect.

**Sending-infrastructure options for `influencers@wayfair.ca`** (choose in Phase 0):

- **Google Workspace / Gmail API or SMTP** (if `wayfair.ca` is on Workspace and `influencers@` is a real
  mailbox): most "personal-looking," replies sit naturally in the inbox; good fit for 50–100/day.
- **Transactional ESP** (SendGrid / Postmark / Amazon SES) with `influencers@wayfair.ca` as a verified
  sender: better at scale, with built-in bounce/complaint webhooks and List-Unsubscribe headers; route
  replies back to the mailbox.
- **Via n8n (recommended for access grant):** authorize Gmail/Workspace (or the ESP) once in n8n and
  expose a "send outreach" workflow the agent calls — n8n sends as `influencers@wayfair.ca` using its
  stored credential, so the agent never handles the mailbox credentials directly (see §3.5). The
  compliance gate still runs before the send workflow is invoked.

**On "automatic":** the end state is hands-off automatic sending. Because the emails go out under the
`wayfair.ca` brand domain, the plan still recommends a **short supervised warm-up** (human approval on the
first batches while sender reputation ramps), then flips to automatic send for A-tier, high-confidence,
compliant candidates. The approval gate is a configurable dial, not a permanent requirement — see §8
Phases 3–4 and the autonomy-threshold open question.

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
- **Active-member re-check:** the gate re-queries Creator Connect (§4.2) at send time and **blocks any
  send to a creator who is already an active program participant** — recruitment is signup-oriented, so
  existing members are never emailed.
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
- **Integration & credentials — n8n:** external access (CreatorIQ, Google Sheets/Gmail, enrichment, ESP)
  is granted and executed through n8n workflows the agent triggers via the configured n8n MCP servers
  (`Wf-plats-n8n-staging` / `Wf-plats-n8n-prod`) or webhooks. Centralizes credentials; see §3.5.
- **Storage:** start with SQLite/CSV under a new `agent/` package alongside `creatoriq/`; graduate to
  Postgres if volume warrants.
- **Email sending:** all outreach sent from **`influencers@wayfair.ca`** via either Google Workspace
  (Gmail API/SMTP) or a transactional ESP (SendGrid/Postmark/SES). Requires `wayfair.ca` DNS
  (SPF/DKIM/DMARC) authorizing the sender, the mailbox/alias to exist, custom headers + List-Unsubscribe,
  and bounce/complaint webhooks. Replies route back to the `influencers@wayfair.ca` inbox.
- **Discovery:** CreatorIQ Discovery API preferred (same platform the program runs on), with platform
  APIs / permitted vendors / Playwright as fallbacks (see §4.1).
- **UI:** extend the existing Streamlit app with a **"Recruitment" dashboard** — daily shift report,
  approval queue, escalations, funnel metrics — reusing the current Looker-styled theme.
- **Creator Connect (program roster):** the existing Google Apps Script / Sheets system is the authoritative
  active-participant list. Integrate read-only via its Apps Script Web App endpoint (preferred) or the
  Google Sheets API with a service account (see §4.2). Reuse/extend the existing script rather than copying
  the roster.
- **CreatorIQ handoff:** accepted creators flow into CreatorIQ (the system the current dashboard already
  tracks), closing the loop from recruitment → activation → performance.

Proposed layout:

```
agent/
  persona.py          # Maple system prompt + rubric constants
  orchestrator.py     # daily shift loop, budget, caps, dedupe
  tools/
    discovery.py  membership.py  location.py  enrichment.py  scoring.py  outreach.py  email_send.py
  creator_connect.py  # Creator Connect roster client (Web App endpoint / Sheets API)
  compliance.py       # CASL gate + suppression list + active-member re-check
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
- **Set up n8n as the access layer:** fix the n8n MCP connection (staging or prod), connect credentials
  (Google for Creator Connect + `influencers@wayfair.ca`, CreatorIQ, enrichment, ESP), and stub the
  workflows that back the agent's tools (§3.5).
- Stand up the system of record (data model in §3.4) and the global suppression list.
- **Set up sending from `influencers@wayfair.ca`:** configure the mailbox/alias, publish SPF/DKIM/DMARC on
  `wayfair.ca`, wire unsubscribe + bounce/complaint handling, and verify a test send/reply round-trip.
- **Confirm CreatorIQ Discovery access:** determine with the CreatorIQ account rep whether Discovery is
  API-accessible (automated) or UI-only (export/ingest), and build the `discover_candidates` client accordingly.
- Encode the Maple persona + scoring rubric.
- **Stand up the Creator Connect integration:** confirm access (service account view access or a deployed
  Web App endpoint), confirm the roster's handle/email/status columns, and build `check_program_membership`.
- **Exit gate:** counsel sign-off on compliance; suppression + unsubscribe verifiably working end-to-end;
  membership lookup returns correct results on a known set of active members.

### Phase 1 — Discovery + membership screen + geo-vetting (read-only, no sending)
- Implement `discover_candidates`, `fetch_profile`, `check_program_membership`, `verify_location`, `log_event`.
- Run daily to produce a **vetted, non-member Canadian candidate list** with confidence + rationale — no emails yet.
- **Exit gate:** on a human-reviewed sample, ≥90% of "high-confidence Canada" picks are actually Canadian,
  and **zero active Creator Connect members** appear in the candidate list.

### Phase 2 — Enrichment + qualification
- Implement `find_email` (with verification) and `score_candidate` (fit/quality/fraud + tiering).
- Produce a daily **qualified + enriched** shortlist. Still no automated sending.
- **Exit gate:** email verification pass-rate and scoring quality validated against human spot-checks.

### Phase 3 — Automatic outreach (supervised warm-up)
- Implement `draft_outreach` (proper-name merge + signup link + program info), `send_email` from
  `influencers@wayfair.ca`, the CASL compliance gate, and follow-up logic.
- Emails **send automatically**, but during warm-up a human approves the first batches in the Streamlit
  approval queue while `wayfair.ca` sender reputation ramps and volume is small.
- **Exit gate:** approval-acceptance rate high, bounce/complaint rates within thresholds, replies logged.

### Phase 4 — Full automatic send & scale
- **Flip to hands-off automatic send** for A-tier, high-confidence, clearly-compliant candidates; keep
  humans on B/C-tier and all edge cases. Ramp to the full 50–100/day cap.
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
| Emailing an existing program member | Wastes budget, annoys partners, off-message | Creator Connect membership screen early in pipeline + re-check at send time; escalate ambiguous matches |
| Creator Connect access/format drift | Missed members, failed lookups | Confirm access + columns in Phase 0; fail-closed (block send) if the lookup errors rather than assuming "not a member" |
| False-positive "Canadian" | Wasted outreach, off-target | Multi-signal resolver + confidence threshold + human escalation on ambiguity |
| Bad email data | Bounces hurt deliverability | Multi-step verification before send; suppress on bounce |
| Generic/off-brand outreach | Low reply rate, brand harm | Persona + structured personalization + human approval gate + quality audits |
| Over-automation early | Scaled mistakes | Phased rollout; sending is human-gated until quality is proven |
| Engagement fraud slips through | Poor program ROI | Authenticity/fraud scoring; tiering; human review of high-value picks |

---

## 11. Open questions / decisions needed

1. **Legal/consent:** Which CASL exemption(s) are we relying on for cold B2B outreach, and who signs off?
   What physical mailing address and sender identity go in the footer?
2. **CreatorIQ Discovery API access:** does the current CreatorIQ contract expose Discovery search via
   the API (fully automated), or only via the UI (export/ingest)? Confirm with the CreatorIQ account rep.
   This determines whether discovery is hands-off or semi-automated, and whether fallback sources are needed.
3. **Enrichment vendor:** Which email-finding/verification provider (Hunter, Apollo, other) is approved?
4. **Sending from `influencers@wayfair.ca`:** who controls `wayfair.ca` DNS to publish SPF/DKIM/DMARC and
   authorize the sender? Is `influencers@wayfair.ca` a Google Workspace mailbox (use Gmail API) or should
   we send via an ESP with it as a verified sender? Any brand/deliverability rules on the apex domain?
5. **Signup flow + program terms:** the exact **signup/onboarding page URL** to link, plus commission/
   gifting structure and expectations to state in the email.
6. **Autonomy threshold:** at what tier/confidence are we comfortable auto-sending vs. requiring approval?
7. **Bilingual scope:** is FR-CA outreach in scope for launch, and who reviews FR-CA copy?
8. **Storage/scale:** stay on SQLite/CSV to match the current repo, or provision Postgres from the start?
9. **Creator Connect access & schema:** grant access via **n8n** (connect Google once, expose a lookup
   workflow — the offered path), a service account with view access, or a Web App endpoint? What are the
   exact roster columns (per-platform handles, email, active/status flag), and how are cross-platform
   identities and handle changes represented so membership matching is reliable?
10. **n8n setup:** use `Wf-plats-n8n-staging` or `Wf-plats-n8n-prod` (both MCP connections currently fail
    discovery and need fixing)? Which credentials (Google, CreatorIQ, enrichment, ESP) will be connected
    in n8n, and will external actions be exposed as MCP tools, webhooks, or both?

---

*This plan is intentionally guardrail-first: the agent is smart, but volume caps, CASL compliance, and a
human approval gate are hard constraints it operates within. Ship the phases in order; each one is
independently useful and de-risks the next.*
