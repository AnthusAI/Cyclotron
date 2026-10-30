# Remote login for the Cyclotron CLI, without long-lived keys

Status: **proposed** (2026-10-30). Ryan decided the open question on 2026-10-30: both brokers throttle. The two
live changes still need his approval.

This plans three stories under the standalone CLI epic (63ef259b):

- 128870de: install the CLI and select a lab and account.
- 244ae4bd: approve a remote login.
- f95dc70c: store sessions securely and renew them.

Papyrus plans the same feature for its own CLI (PPY-5a3624, PPY-109943, PPY-829904). Ryan wants the two to
work the same way, so this plan follows Papyrus's protocol, endpoint names, broker shape, storage order and
renewal contract. Since Ryan's decision of 2026-10-30, both CLIs build on one shared library and broker in a
new public AnthusAI repository ([shared-cli-auth.md](shared-cli-auth.md)), which Auritus and Chatticus can use
too. The products stay separate: each has its own key namespace, client, discovery data and sessions, and
neither reads the other's stored credentials.

## Same as Papyrus, and where it differs

The same in both:
- **The broker.** One broker per deployment: one per Papyrus site, one per Cyclotron lab. It is its own Lambda
  function with a function URL, plus a DynamoDB table with TTL, in that deployment's backend. Its approval and
  callback pages are on the deployment's own domain.
- **The protocol.** The broker serves `POST /start` and `POST /poll`, and the site serves the pages
  `/cli/approve` and `/cli/callback`. Each answer carries the same fields, the transaction states are the same,
  and the flow uses the split secrets V and P.
- **Discovery.** The deployment's discovery data names the broker and the callback with the same keys,
  `remote_login_endpoint` and `remote_callback_uri`.
- **The client.** Each deployment's existing public Cognito client gets one more callback URL, with no new
  client. The CLI holds that client's own tokens and renews them with the refresh token.
- **Commands.** `login --remote <target>`, `auth agent [--status | stop]`, `status`, `whoami` and `logout`.
- **Storage.** The same backend order, fail-closed with no plaintext fallback, including the memory-only
  agent on cloud machines (Ryan's decision for Papyrus). Each target has its own cross-platform lock.
- **Renewal.** The same rules: three separate kinds of evidence, never replaying an uncertain mutation, and
  `invalid_grant` failing closed while network and 5xx errors keep the session.
- **Throttling.** Both brokers rate-limit each caller on `/start` and `/poll`, and answer a CLI that polls too
  fast with `slow_down`, which makes it wait longer (Ryan, 2026-10-30: keep the throttling on both).

Ryan wants the best choices from both plans in one solution. These came from this plan and go into the Papyrus
plan too:
- The approval page asks the person to compare the matching code before approving. It does not only show it.
- `/poll` hands the code over with a conditional delete, so two polls at once cannot both receive it.
- A request that carries both a cookie and a bearer token is refused.
- The CLI names its secure-store backend explicitly, so a library cannot choose a plaintext backend on its own.
- The client's token settings are read and recorded with the plan, and again at release.
- Logout says plainly that an access token the API has already accepted stays valid until it expires.

Where they differ, and why:
- **Where each starts.** Papyrus already has a CLI: `papyrus login` with the macOS Keychain and refresh
  renewal. It adds `--remote` and the new stores. Cyclotron has no CLI yet. What it has is the lab's browser
  sign-in, which uses the same Cognito pieces (described below). So Cyclotron builds the `cyclotron` CLI
  first (128870de).
- **What a session is bound to.** A Papyrus session is bound to a publication. A Cyclotron session is bound
  to a lab, and the account is chosen on top of it.
- **How the API accepts the CLI.** Papyrus's CLI already calls its site APIs with Cognito tokens. The lab's
  API accepts only its own cookie session today, so it needs a bearer path.
- **Enrollment.** A person must have signed in to the lab in a browser once, because the lab's identity
  lookup at Prospectus requires prior enrollment.

## Why

Ryan, 2026-10-30: "We need a feature like the AWS CLI login's `--remote` option, so that an agent with a
computer running in the cloud can start a login and then give me a URL that I can use to authenticate that
CLI session for it. So that we can authenticate cloud AI sessions without using long-lived access keys."

So an agent on a cloud machine with no browser runs `cyclotron login --remote`, prints a URL and a short
matching code, and waits. Ryan opens the URL on his phone or laptop, signs in as he does for the lab, checks
the code, and approves. The CLI then holds a renewable session for that lab, and nothing secret has passed
through chat: no access key, no pasted code, no token.

## What the lab does today

These facts are taken from the code and from the live client settings (read on 2026-10-30).

- **Who signs people in.** The lab signs people in through the `prospectus-members` Cognito user pool
  (`us-east-1_hlSyajw9S`, in the Papyrus production account 712236451410) with Google, using the app client
  `cyclotron-console-public`.
- **Who owns the client.** The Prospectus repository defines the client (its Amplify backend,
  `ProspectusMembersCyclotronConsoleBrowser`). A change to the client is a Prospectus change and a Prospectus
  deploy.
- **The client's settings.** It is a public client: no secret, the authorization-code flow with PKCE, and
  `ALLOW_REFRESH_TOKEN_AUTH`. Access and ID tokens last 60 minutes and refresh tokens 30 days. Token
  revocation is enabled, and refresh-token rotation is off. Its only callback is
  `https://lab.cyclotron.anth.us/auth/callback`.
- **How a browser signs in.** The lab's BFF does the whole sign-in on the server (`account_routes.py`):
  1. It sends the browser to `/oauth2/authorize` with S256 PKCE, a state and nonce bound to a browser
     cookie, and a central Google identity challenge.
  2. It exchanges the code itself.
  3. It resolves the person at Prospectus with the signed challenge.
  4. It keeps a server-side session behind an opaque cookie.
  The session ends when the access token expires, after one hour at most. The lab never asks Cognito for a
  refresh.
- **How a request is checked.** After sign-in, every request is checked with the access token alone. The
  person comes from Prospectus's access-only lookup (`GoogleIdentityClient.me`), and the account grants come
  from Prospectus on every request. The signed Google challenge is needed only to enroll a person the first
  time.

Two things follow from these facts:

- **The client already allows a renewable CLI.** A CLI that holds this client's own Cognito tokens can renew
  for 30 days. The only change the client needs is a second callback URL.
- **The lab can accept the CLI's tokens.** It can check a CLI's access token exactly as it checks a browser
  session's: verify the JWT, look the person up at Prospectus, then check the account grant. What it lacks is
  a way to receive a bearer token instead of a cookie.

## Why a broker

Cognito has no RFC 8628 device flow. AWS's `--remote` has the person paste a code back into the terminal, and
pasting through chat is ruled out. So each lab gets a small broker that hands one authorization code to the one
CLI that asked for it.

The broker is the shared one ([AnthusAI/cli-login](https://github.com/AnthusAI/cli-login)), deployed once for
the lab:
- **The CLI's routes.** The CLI calls its function URL directly for `/start` and `/poll`, which are throttled
  per caller.
- **The pages.** The lab's BFF serves `/cli/approve` and `/cli/callback` on `https://lab.cyclotron.anth.us`
  with the library's page handlers, so the person approves on the origin they already sign in on.
- **The private steps.** The BFF invokes the broker's `describe`, `bind`, `complete` and `deny` with IAM.

The broker never holds a token.

## The flow

**1. Start (CLI).** The CLI makes a PKCE verifier V and a poll secret P, and keeps both in memory. It sends
`POST /start` to the broker with:
- the client id;
- the lab origin;
- `code_challenge = S256(V)`;
- `sha256(P)`;
- a requester label: host name, CLI version, and the agent's own note.

The lab answers with:
- a transaction id T (128 random bits);
- a matching code of eight characters from an unambiguous alphabet;
- `https://lab.cyclotron.anth.us/cli/approve?t=T`;
- a poll interval;
- an expiry about 10 minutes away.

The CLI prints the URL and the matching code, and nothing else secret.

**2. Approve (person, any device).** The approval page shows the requester label, the matching code, the lab,
and what a CLI session can do: everything the person can do in the lab, within each account's grants. The
person confirms that the code matches the one the agent reported.

Approve sends the browser to `/oauth2/authorize` with:
- the transaction's `code_challenge`;
- `redirect_uri=https://lab.cyclotron.anth.us/cli/callback`;
- `state` = T plus a CSRF nonce bound to a short-lived cookie.

Deny and cancel are recorded on the transaction.

**3. Callback (lab).** `/cli/callback` checks the state against the cookie and stores the authorization code on
T. The code is useless without V, which never leaves the CLI.

**4. Redeem (CLI).** The CLI polls `POST /poll` with T and P. The lab compares `sha256(P)` and, once the
code is there, returns it exactly once: the same conditional write that reads it also deletes the record.
Then the CLI exchanges the code with V at the Cognito token endpoint and verifies the tokens itself:
- issuer and client;
- `token_use`;
- expiry;
- the nonce it chose.

**5. Use.** The CLI calls the lab's API with `Authorization: Bearer <access token>`. The lab verifies the
token, then looks up the person and the account grant at Prospectus, exactly as for a browser session.

A bearer request needs no CSRF check, because it carries no ambient credential. The lab refuses a request that
carries both a cookie and a bearer token.

### Properties

- The broker never holds a token. The code it holds for a few seconds needs V.
- The URL cannot redeem anything without P.
- `/start` and `/poll` are rate-limited per caller. A CLI that polls faster than the interval gets `slow_down`
  and waits longer, as in RFC 8628.
- Each transaction is single use, expires in about 10 minutes, and is bound to issuer, client and lab. A
  replayed, expired, denied or substituted transaction yields nothing; the Gherkin in 244ae4bd covers these
  cases.
- Phishing is the remaining risk, as in every device flow: someone could send the URL to another person and
  hope they approve it. The requester label and the matching code are the defence, so the page asks the person
  to compare the code before approving.
- **First enrollment.** A person who has never signed in to the lab in a browser is not enrolled at
  Prospectus. The access-only lookup would then refuse, and the CLI says to sign in to the lab once in a
  browser. The broker cannot do the enrollment, because that needs tokens.

## Choosing a lab and an account (128870de)

`cyclotron` is its own package and executable. It runs on clean Linux without Papyrus, a Mac or the AWS CLI.

- **Lab.** The CLI discovers and checks the lab's metadata from the lab's discovery data: the issuer, client id,
  authorization origin, API origin, `remote_login_endpoint` and `remote_callback_uri`. It pins them all to the
  session it stores.
- **Sessions.** A session is one person signed in to one lab. Accounts are a selection on top of it, kept as
  non-secret context (`cyclotron use --lab <origin> --account <id>`). Every command sends that context, and the
  lab checks the grant.
- **No guessing.** A context that is unclear is refused until it is chosen explicitly, and a missing grant is
  denied with no fallback to another lab or to AWS.

## Storage (f95dc70c)

The CLI stores secrets in this order. Every backend fails closed, and there is never a plaintext fallback.

1. **macOS Keychain.**
2. **Linux Secret Service** (D-Bus, an unlocked collection).
3. **Linux kernel keyring.** Probe it first: Docker's default seccomp profile blocks `keyctl` and `add_key`. A
   user keyring lasts until reboot.
4. **Memory agent**, ssh-agent style. The refresh token lives only in a background process's memory, behind a
   0600 Unix socket, and never on disk. After a restart, the person approves again. This is the backend for
   cloud machines with no keyring: `cyclotron auth agent`, with `--status` and `stop`, as in Papyrus.
5. **Windows Credential Manager.** A blob is at most 2,560 bytes, so the CLI stores only the refresh token
   there.

How credentials are kept apart and protected:

- **Keys.** Credentials are keyed by product (`cyclotron`), lab origin, issuer, client and Cognito subject.
  Non-secret context (the selected account, cached display names) lives in a separate file.
- **Locks.** Each key has its own cross-platform lock (`fcntl` on Linux and macOS, `LockFileEx` on Windows),
  not one global lock.
- **Library choice.** If the CLI uses the Python `keyring` package, it names the backend explicitly. That stops
  `keyring` from choosing a plaintext backend such as `keyrings.alt` on its own.

## Renewal, status and logout (f95dc70c)

- **Renewal.** The CLI refreshes with the refresh token shortly before the access token expires, holding the
  key's lock. Rotation is off on this client today. The lock still serializes concurrent refreshes, so turning
  rotation on later cannot lose the newest refresh token.
- **The 30-day limit.** Cognito does not extend a refresh token's 30 days when it is used. After 30 days the
  person approves again.
- **No replays.** A mutation whose outcome is uncertain is never replayed. Lab mutations from the CLI carry a
  request id, and a retry reuses that id.
- **Status and whoami.** These redact every token. They tell apart what is cached locally (lab, subject,
  expiry) from what the lab confirms remotely (person, grants).
- **Logout.** Logout deletes the local secret and revokes the refresh token at `/oauth2/revoke`. It reports a
  failed or offline revocation rather than hiding it, and it touches only that lab's session.
- **What logout cannot do.** An access token the lab has already seen stays valid until it expires, up to 60
  minutes, because the lab verifies JWTs offline. Whether Prospectus's access-only lookup refuses a revoked
  token is not yet checked; it is part of the evidence below.

### Evidence of renewal

Three reports, kept separate:

1. **Simulated-clock specs:** more than 24 simulated hours, fresh processes, concurrent refresh, and revocation.
2. **The settings:** the client's provider settings, read and recorded (as above, and again at release).
3. **A real run:** a cloud session logged in with `--remote` that keeps working for 24 hours or more of
   elapsed time.

## What changes where

| Where | Change | Needs |
|---|---|---|
| Prospectus | Add `https://lab.cyclotron.anth.us/cli/callback` to `cyclotron-console-public` | Ryan's approval; Prospectus deploy |
| Shared repository | The library (stores, memory agent, locks, sign-in, remote login, renewal, logout), the broker with its pages, and the protocol specs ([shared-cli-auth.md](shared-cli-auth.md)) | A new public AnthusAI repository |
| Lab BFF (`account_routes.py`) | The discovery keys, and bearer sessions on `/api/*` | Specs beside the modules; Gherkin from the stories |
| Lab stack | A broker deployment (its Lambda, function URL and DynamoDB table with TTL), the two pages in the BFF, and the BFF's grant to invoke the broker | Ryan's approval; reviewed change set |
| New package `cyclotron` | `login [--remote]`, `use`, `status`, `whoami`, `logout` and `auth agent`, from the library, plus lab and account selection | Fixture-driven read/write example first (128870de) |

The order:
1. The library and broker are built in the shared repository, starting from Papyrus's existing code.
2. The lab's bearer path and discovery keys.
3. The CLI, tested against a fake lab.
4. The live changes, once Ryan approves them.
5. The 24-hour run.

## Decided

**Throttling, for both CLIs** (Ryan, 2026-10-30): "Keep the throttling on both, that's a good idea." Both brokers
rate-limit each caller on `/start` and `/poll`, and send `slow_down` to a CLI that polls too fast. This protects a
sign-in endpoint that needs no prior sign-in, so it is an exception to Cyclotron's rule against arbitrary limits.
The rule still holds for the decision pipeline.
