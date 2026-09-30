# Backend architecture

only where it touches the API or the relay.

## 1. The one rule

The backend helps, but it is never where the sensitive things live. If the server were seized or
served with a court order, it should hold nothing that identifies a survivor or reveals what she said.

The backend must never:

- **hold a private key.** Survivor keys are made in her browser tab and die with it. The platform
  key signs the client config on a laptop, not on the server.
- **see message content, or who is talking to whom.** Messages go browser → relay → browser.
  They never pass through FastAPI.
- **hold money.** Holding other people's money is a licensed activity under Kenya's VASP Act 2025.
- **log IP addresses next to public keys.** An IP plus a key is exactly the link an investigator,
  or an abuser with connections, would want.

Every choice below comes from those four lines.

## 2. The pieces

```
  survivor's browser tab            counsellor dashboard (browser)
          │  │                                 │  │
   wss:// │  │ https:// (read only)     wss:// │  │ https:// (NIP-98 signed writes)
          ▼  ▼                                 ▼  ▼
 ┌─────────────────────┐   ┌──────────────────────────────────────┐
 │ RELAY               │   │ API (FastAPI)                        │
 │ nostr-rs-relay      │   │  /v1/config       signed relay list  │
 │ 0.10.0              │◄──│  /v1/orgs         directory          │
 │ NIP-42 auth         │   │  /v1/disbursements payment records   │
 │ gift wraps only to  │   │  worker: NIP-05 checks, roster sync, │
 │ their recipient     │   │  expiry, purge                       │
 └─────────────────────┘   └──────────────────┬───────────────────┘
                                              ▼
                                    ┌──────────────────┐
                                    │ PostgreSQL 16    │
                                    └──────────────────┘
          all behind Caddy (TLS) on one small server
```

Three things we run: the relay, the API (plus its worker), and the database. The web client is
static files on a **separate host**, for the reason in section 5.1.

## 3. What the backend owns, and what it does not

| Concern | Backend? | Notes |
|---|---|---|
| Keys, message encryption, outbox, retry, relay failover | No | Client. The backend never sees them. |
| Storing private messages | Relay only | Ciphertext in NIP-17 gift wraps, deleted on expiry. |
| Trusted relay list | Serves it | Signed by the platform key; the client verifies. |
| Organisations and counsellor rosters | Yes | Index and cache. Clients still verify signatures. |
| NIP-05 checks on organisations | Yes | Worker job. |
| Emergency payments | Records only | State machine and preimage-hash claim. Never holds funds. |
| Health records | Not built | Cut from the demo. See section 10. |

## 4. Stack 

| Piece | Choice |
|---|---|
| API | FastAPI 0.141.1, Pydantic 2.13.5, pydantic-settings 2.15.0, uvicorn 0.54.0 |
| Database access | SQLAlchemy 2.1.1, Alembic 1.20.0, psycopg 3.3.6 |
| Nostr signatures | coincurve 21.0.0 (BIP-340 Schnorr), with our own ~80-line NIP-01 helper in `app/nostr/events.py` |
| Tests | pytest 9.1.1, hypothesis 6.168.3 |
| Lint and format | ruff 0.16.9 |
| Images | `python:3.12.14-slim-bookworm`, `postgres:16.15`, `scsibug/nostr-rs-relay:0.10.0`, `caddy:2.11.4-alpine` |

No Redis, no Celery. Background jobs are a few loops in a second process from the same code.
BOLT11 invoices are decoded and signature-checked with `bolt11` 2.2.0.

## 5. The API

### 5.1 Config: the trusted relay list (built)

`GET /v1/config` returns a Nostr event (kind 30078, `d` tag `resilience/client-config`) signed by
the **platform key**. Its content lists the trusted relays and the address of the approved-orgs
list. The platform's public key is built into the web client, which checks the signature.

- The platform secret key never touches the server. `scripts/sign_config.py` signs the file on a
  laptop; the server only serves it, and refuses to if it does not verify against
  `PLATFORM_PUBKEY`.
- **Web caveat:** this protects the relay list only while the web client is served from a
  different host than the API. If one box serves both, whoever takes that box can change the key
  and the list together. A native app later closes this fully.

### 5.2 Authentication: NIP-98 (built)

No accounts, no passwords, no phone numbers. Every write carries
`Authorization: Nostr <base64 of a signed kind 27235 event>`. The server checks, in order:

1. The token is base64 JSON, the event id matches its content and the signature is valid.
2. Kind is 27235.
3. `created_at` is within 60 seconds of now.
4. The `u` tag equals `PUBLIC_API_BASE` + path + query. **Never `request.url`**: behind Caddy the
   server sees `http://api:8000/...` while the browser signed `https://api.<domain>/...`.
5. The `method` tag equals the request method.
6. On POST, PUT and PATCH, the `payload` tag equals the SHA-256 of the body.
7. On writes, the event id has not been used before (`seen_auth_events`). Reads may repeat inside
   the window: two identical GETs in the same second produce the same event id, and replaying a
   read changes nothing.

The result is a pubkey. Each route then decides whether that key is a platform admin, an
approved organisation, or a counsellor on an organisation's roster.

Survivors never authenticate to the API. Config and directory are public GETs, so the server
cannot tell which survivor key is reading the directory.

### 5.3 Directory: organisations and counsellors (built)

A chain of three signatures, each checkable by the client without trusting the server:

```
platform key ──signs──► approved-orgs list (kind 30000, d=approved-orgs)
                              │  each org also proves its domain with NIP-05
                              ▼
organisation key ──signs──► verified-counsellors list (kind 30000, d=verified-counsellors)
                              ▼
                        counsellor keys
```

- Rosters are NIP-51 follow sets with an `expiration` tag (NIP-40), for example 30 days, so a
  forgotten roster lapses on its own.
- **Revoking a counsellor is publishing a new list.** Follow sets are addressable, so the newer
  version replaces the old one. NIP-58 badges are not used because the spec makes awards
  immutable.
- The backend adds: admin approval of organisations, a NIP-05 check
  (`https://<domain>/.well-known/nostr.json?name=_`), roster sync from the relay with signature
  checks, and a fast directory API.
- The backend is a fast index, not the source of truth. Clients verify what they receive.

How it works in the code:

1. **Apply.** `POST /v1/orgs {name, domain}`, NIP-98 signed by the organisation's key. That key
   becomes its identity; one application per domain and per key. The domain must be a public
   hostname (no IPs, ports, `localhost` or `.local`). Status `pending`, hidden from the public.
2. **Approve.** An admin (a key in `ADMIN_PUBKEYS`) calls `POST /v1/admin/orgs/{id}/approve`.
   The server fetches `https://<domain>/.well-known/nostr.json?name=_` right then, without
   following redirects (NIP-05 forbids them), with a 5-second timeout and a 64 KB cap, and
   refuses domains that resolve to private addresses. Approval only happens if `names._` is the
   organisation's key.
3. **Roster.** The organisation signs its counsellor list (kind 30000, `d=verified-counsellors`,
   `p` tags, `expiration` required) and sends it to `PUT /v1/orgs/{id}/roster`. No NIP-98 is
   needed because the event carries the organisation's own signature. The server refuses events
   signed by any other key, anything older than the stored roster, and expired or future-dated
   events. Anyone left off the newest roster is marked inactive.
4. **Profile.** Each counsellor signs her own public profile, a standard Nostr kind 0 event,
   and sends it to `PUT /v1/orgs/{id}/counsellors/{pubkey}/profile`. Like the roster, it needs no
   NIP-98 because the event carries her own signature. The server accepts it only from a
   counsellor on the organisation's current, unexpired roster, refuses any other signer and any
   profile older than the stored one, and keeps the signed event verbatim. One profile per key,
   as on Nostr. Besides `name`/`display_name` and `about`, Resilience reads three fields of its
   own from the content: `specialties` and `languages` (lists of short text) and
   `response_time` (for example "Usually replies within a few hours"). Other Nostr clients ignore
   them.
5. **Read.** `GET /v1/orgs` and `GET /v1/orgs/{id}/counsellors` are public and cacheable for 60
   seconds. The counsellors response carries the organisation (name, domain, NIP-05), the signed
   roster, and every counsellor the organisation has listed, each with a `status`:
   `verified` (on the newest roster, not expired), `expired` (on the newest roster, but the
   organisation let it lapse) or `removed` (left off the newest roster). Removed and expired
   counsellors stay in the list so a survivor already talking to one is warned instead of
   watching them vanish. Each entry has the parsed profile and the signed profile event, so the
   client can check both signatures itself. `picture` and `banner` are never returned: loading an
   image URL would give that host the survivor's IP address, so clients must not load them from
   the signed event either.
6. **Re-check.** Every 6 hours the worker repeats the NIP-05 check. A definite failure (wrong
   key, no file, a redirect) suspends the organisation. A timeout or a 5xx is only logged, so a
   website's bad hour does not take an organisation offline.

Not built yet: the worker pulling rosters and counsellor profiles from the relay by itself (the
dashboard sends them to the API), and the platform-signed approved-orgs list for clients to
verify.

### 5.4 Disbursements: emergency money (built)

The counsellor sends emergency assistance to a survivor. The backend records the disbursement and
checks that the submitted preimage hashes to the invoice payment hash. It never holds or transfers
the money. This check records an authenticated organisation's payment claim; the API does not
independently observe or verify Lightning settlement.

1. The counsellor creates a disbursement. State `CREATED`.
2. The survivor's wallet makes a Lightning invoice and sends it back **in the chat**.
3. The dashboard attaches it. The API decodes it and checks amount, expiry and first use.
   State `INVOICE_ATTACHED`.
4. The counsellor presses Pay: state `PAYING`, and the organisation's own browser wallet pays.
5. The payer's wallet returns the **preimage**. The counsellor submits it, and the API checks
   `sha256(preimage) == payment_hash`. State `PAID` means this authenticated claim had a
   hash-matching preimage. It is not independent proof of network settlement; that needs a wallet
   or provider verification integration, which is not included.

```
CREATED ──invoice──► INVOICE_ATTACHED ──start──► PAYING ──matching preimage──► PAID
   │                        │                         │
   ├──cancel──► CANCELLED  ├──cancel──► CANCELLED   └──expired──► stays PAYING
   └──24 hours──► EXPIRED  └──invoice expired──► EXPIRED
```

Rules that stop paying twice:

- `POST /v1/disbursements` needs an `Idempotency-Key`, unique per organisation. Same key and body
  returns the original; same key with a different body returns 409.
- Every transition is one statement: `UPDATE ... SET state = :new WHERE id = :id AND state =
  :expected`. Zero rows changed means someone else got there first: 409.
- `PAYING` never fails on its own. The worker does not retry or mark it failed on expiry because
  it cannot tell whether payment settled just before expiry and the proof arrived late. Keep it in
  `PAYING` until the organisation reconciles it with its wallet/provider; retrying without that check
  could pay twice. An attached invoice expires automatically only before the counsellor marks it
  `PAYING`. `FAILED` is reserved for a future wallet/provider result that confirms payment failed;
  no current route can make that transition.
- The same valid preimage twice returns 200 with the same result.
- Per-payment and daily caps per organisation, checked at `CREATED`. A platform admin configures
  the caps through `PUT /v1/admin/orgs/{id}/disbursement-limits`. Zero/unset caps block new requests.
  Pending amounts and paid amounts count toward the daily cap; cancelled, expired, and failed ones do
  not. The day boundary is midnight in Kenya (Africa/Nairobi). The same idempotency key and body
  returns the original even when it already reserves the cap.
- Once final, the invoice string is deleted; only hash, amount and timestamps stay. The
  survivor's pubkey is never stored on a disbursement.

In tests, `POST /v1/_mock/settle/{id}` simulates a wallet settlement and is mounted only when
`APP_ENV=test`. There is no production payment provider or wallet integration yet. The production
API records the authenticated counsellor's claim after checking the preimage hash. No M-Pesa
(Daraja) payouts to survivors: they show on her M-Pesa statement.

### 5.5 Endpoints

| Method and path | Auth | Status |
|---|---|---|
| `GET /healthz` | none | built |
| `GET /v1/config` | none | built |
| `GET /v1/whoami` | NIP-98 | built |
| `GET /v1/orgs`, `GET /v1/orgs/{id}/counsellors` | none | built |
| `POST /v1/orgs` | NIP-98 | built |
| `PUT /v1/orgs/{id}/roster` | none (the body is a signed event) | built |
| `PUT /v1/orgs/{id}/counsellors/{pubkey}/profile` | none (the body is the counsellor's signed kind 0) | built |
| `GET /v1/admin/orgs?status=` | NIP-98, platform admin key | built |
| `POST /v1/admin/orgs/{id}/approve` \| `/suspend` | NIP-98, platform admin key | built |
| `PUT /v1/admin/orgs/{id}/disbursement-limits` | NIP-98, platform admin key | built |
| `POST /v1/disbursements` + `Idempotency-Key` | NIP-98, approved org or current counsellor | built |
| `POST /v1/disbursements/{id}/invoice` | NIP-98, org or current counsellor | built |
| `POST /v1/disbursements/{id}/paying` \| `/proof` \| `/cancel` | NIP-98, org or current counsellor | built |
| `GET /v1/disbursements?state=` | NIP-98, org or current counsellor; own orgs only | built |
| `POST /v1/_mock/settle/{id}` | NIP-98, test environment only | built |

## 6. The relay

`relay/config.toml` for nostr-rs-relay 0.10.0:

- `nip42_auth = true` and `nip42_dms = true`: gift wraps (kind 1059) are only served to the
  authenticated recipient.
- It deletes events whose NIP-40 `expiration` has passed. Clients set 7 days on every gift wrap.
  A public relay "MAY persist them indefinitely", which is why we run our own.
- `remote_ip_header` unset and `RUST_LOG=warn`, so client IPs do not end up in logs.
- Rate limits: 5 events/second, 10 subscriptions/minute.
- In production `relay_url` must be the public `wss://` address, because NIP-42 checks it.

No group messaging: NIP-17 groups have no admins and no bans, so an abuser who gets into a
support group cannot be removed.

## 7. Data model (migrations 0001 and 0002, built)

```
organizations            id, name, domain (unique), nostr_pubkey (unique),
                         status pending|approved|suspended, nip05_verified_at,
                         per_payment_cap_sat, daily_cap_sat, created_at
roster_events            event_id pk, org_id, created_at, raw jsonb   -- signed source of truth
counsellor_attestations  (org_id, counsellor_pubkey) pk, roster_event_id, issued_at,
                         expires_at, active
counsellor_profiles      counsellor_pubkey pk, event_id (unique), created_at, raw jsonb,
                         updated_at                                  -- signed kind 0, 0002
disbursements            id, org_id, idempotency_key, request_hash, amount_sat, amount_kes,
                         rate_source, reason_code, state, payment_hash (unique), invoice,
                         invoice_expires_at, created_by_pubkey, created/updated/paid_at
                         unique (org_id, idempotency_key)
disbursement_transitions id, disbursement_id, from_state, to_state, actor_pubkey, at
seen_auth_events         event_id pk, seen_at                          -- NIP-98 replay guard
```

What is absent is the point: no survivor table, no message table, no IP column, no phone numbers.

## 8. Background worker

`python -m app.worker`, same code, second process.

| Job | Every | Status |
|---|---|---|
| Purge replay-guard ids older than 2 × the NIP-98 window | 1 min | built |
| NIP-05 re-check; suspend orgs whose domain no longer vouches for their key | 6 h | built |
| Roster sync from the relay, verify, index | 5 min | planned (the API accepts signed rosters) |
| Disbursement expiry, delete final invoice strings | 1 min | built |

## 9. Deployment

- One small server: Caddy (TLS for `api.` and `relay.` subdomains), API, worker, Postgres, relay.
  Browsers refuse `ws://` from an `https://` page, so the relay needs a real certificate before
  anyone tests on a phone.
- Web client: a Next.js static export on a separate host (Cloudflare Pages or Vercel).
- Settings that must be right in production: `PUBLIC_API_BASE` (the public https URL),
  `CORS_ORIGINS` (exact web client origin; `*` is refused at startup), `PLATFORM_PUBKEY`.
- uvicorn runs with `--no-access-log`. The static host will log page loads with IPs, which shows
  an IP visited the site but never links it to a key.

## 10. Threat model: the backend's share

| Threat | What stops it |
|---|---|
| Server seized or subpoenaed | It holds public org data and payment hashes. No messages, no survivor identities, no IPs. |
| Abuser poses as a counsellor | Counsellors exist only on org-signed rosters, orgs only on the platform-signed list plus NIP-05, and clients verify every signature. |
| Fake relay swapped in | Relay list is signed by the platform key; the client checks it. |
| Captured request replayed | 60-second window, exact URL and method, body hash, single-use ids on writes. |
| Organisation pays twice | Idempotency key, conditional updates, no automatic retry from `PAYING`. |
| Someone else reads her messages | NIP-44 encryption inside NIP-17 gift wraps, and `nip42_dms`. |
| Logs link a person to a key | Access logs off, relay at warn level, no IP header forwarding. |

Health records are out of scope for the current implementation. They would be the riskiest data
in the system, and a record encrypted with a key that dies when the tab closes can never be
reopened.

## 11. Tests

Built so far (149 tests, all passing):

- **Events:** property tests with hypothesis. No event whose content or tags change after signing
  may ever verify.
- **NIP-98:** every rejection path (missing header, garbage token, wrong key, wrong kind, too old,
  too new, wrong URL, internal URL, wrong method, missing or wrong payload hash, replayed write)
  plus the rule that a rejected request does not burn its event id.
- **Config:** 503 when unconfigured, 500 for a tampered file or another key's signature.
- **NIP-05:** domain rules, every failure mode (wrong key, no `_`, redirect, 404, bad JSON),
  which failures count as definite, private-address refusal, and the dev override being ignored
  outside dev and test.
- **Organisations:** apply, duplicates, bad input, admin-only listing and approval, approval
  refused when the website does not vouch, suspension, and the worker suspending only on a
  definite failure.
- **Rosters:** signature and author checks, tampering, stale and replayed rosters, expiry, and a
  hypothesis property test: after any sequence of rosters, only counsellors on the newest one
  are listed.
- **Disbursements:** NIP-98 and org/counsellor authorization, caps, idempotency under retries,
  BOLT11 signature/network/amount/expiry checks, legal transitions, privacy, retry after expiry,
  hash-matching preimage handling, and concurrent create/proof races.
- **Platform:** health check, CORS allows only the configured origin, `*` refused, purge and expiry
  jobs, migrations down and up.

Disbursement tests also cover BOLT11 amount round-trips, concurrent create/proof requests, and
randomized request sequences that must stay within the organisation daily cap.
