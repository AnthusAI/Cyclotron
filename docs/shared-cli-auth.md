# One CLI login for every Anthus product

Status: **proposed** (2026-10-30). Ryan approved factoring the CLI login out into a new public repository,
[AnthusAI/cli-login](https://github.com/AnthusAI/cli-login). Its `docs/design.md` is now the design, and its
board tracks the work. This page records why, and what it means for Cyclotron.

## Why

Three Python CLIs sign people in with Cognito, and each does it its own way:

| Product | CLI login today | Gaps against the agreed design |
|---|---|---|
| Papyrus | `papyrus login`: PKCE, macOS Keychain, refresh renewal, a registry of sites (about 720 lines) | Mac only; no remote login |
| Auritus | `auritus login`: password or SSO, refresh (432 lines in `auritus/auth.py`) | Tokens in a mode-0600 file in plain text; SSO on a fixed `localhost:8080` callback with no PKCE and no state; no remote login |
| Cyclotron | None yet: only the lab's browser sign-in | Everything (epic 63ef259b) |
| Chatticus | None (a TypeScript product) | Would use the broker and the protocol, not the Python library |

The agreed design (Papyrus PPY-5a3624, PPY-109943 and PPY-829904; Cyclotron
[cli-remote-login.md](cli-remote-login.md)) adds about 2,000 to 3,000 lines of client code to each CLI:
- the Linux, Windows and memory-agent stores;
- cross-platform locks;
- the remote-login client;
- renewal and logout rules.

It also needs a broker service for each deployment. Writing that three times means reviewing and fixing the
same security code three times. One library means it is right once, and Chatticus and Auritus get it already
tested.

## What "do not combine the products" means now

The Cyclotron and Papyrus issues say shared auth libraries "must not combine the two CLI products". A shared
library keeps that rule as long as every product stays separate:
- its own key namespace in the secure store;
- its own Cognito client;
- its own discovery data;
- its own sessions.

One product never reads, refreshes or revokes another product's credentials, and there is never one login that
covers two products. The library enforces this: the product namespace is part of every stored key, and a store
refuses a key outside the namespace it was opened with.

## What the repository holds

The repository is public, so it holds only code and docs: no account ids, pool ids, client ids, domains or
secrets. Each product supplies those through its own discovery data. It has three parts.

### 1. The Python library (`anthus_cli_auth`)

The product describes one target: the issuer, the client id, the authorization and token endpoints, the API
origin, the broker endpoint and callback, its product namespace, and the binding it checks. A Papyrus binding is
a publication; a Cyclotron binding is a lab with an account on top. The library does the rest:

- **Signing in on this machine.** PKCE (S256), a loopback callback on a free port, state and nonce, and the
  token exchange. It checks each token's issuer, client, `token_use`, expiry and nonce.
- **Remote login.** It calls the broker's `/start`, prints the URL and the matching code, polls `/poll` at the
  given interval, waits longer on `slow_down`, and exchanges the code with V.
- **Secure stores.** In the agreed order:
  1. macOS Keychain;
  2. Linux Secret Service;
  3. the Linux kernel keyring, after probing that it works;
  4. the memory-only agent;
  5. Windows Credential Manager.

  Every store fails closed, and there is never a plaintext fallback. Each backend is named explicitly. A
  product can keep its existing Keychain service name, so Papyrus's Mac sessions survive the move.
- **The memory agent.** It runs as `<product> auth agent`, with `--status` and `stop`. It holds refresh tokens
  in memory only, behind a 0600 Unix socket, and runs one agent per product.
- **Locks.** One cross-platform lock per stored key (`fcntl`, or `LockFileEx` on Windows), never a global one.
- **Renewal.** It refreshes shortly before expiry while holding the key's lock. On `invalid_grant` it ends the
  session with recovery guidance. On a network error, 429 or 5xx, it keeps the session and retries, but only
  token operations: never a product's mutation.
- **Logout and status.** Logout deletes the local secret, revokes the refresh token at `/oauth2/revoke`,
  reports a failed or offline revocation, and says that an already-accepted access token stays valid until it
  expires. Status redacts every token, and separates what is cached locally from what the server confirms.
- **Commands.** Optional ready-made `login [--remote]`, `status`, `whoami`, `logout` and `auth agent`
  subcommands, which a product mounts under its own CLI name.

### 2. The broker (one deployment per product deployment)

The broker is a Python Lambda function, a DynamoDB table with TTL, and a CloudFormation template.
- **The CLI's routes.** The CLI calls the function URL directly for `POST /start` and `POST /poll`. Those two
  routes are throttled per caller and answer `slow_down` (Ryan, 2026-10-30).
- **The pages.** The product serves `/cli/approve` and `/cli/callback` on its own domain, with the library's
  page handlers.
- **The private steps.** The product's backend calls the broker's `describe`, `bind`, `complete` and `deny`
  by invoking the Lambda with IAM, so only the product's own pages can attach a sign-in to a waiting CLI.

The Papyrus session proposed this shape on 2026-10-30. It needs no reverse proxy, and the CSRF cookie belongs to
the product's own domain.

### 3. The protocol and its conformance specs

A language-neutral description of the broker's requests, answers, states and errors, with Gherkin features
that check a broker and a client. A TypeScript CLI, such as a future Chatticus one, implements the client from
the protocol and runs the same features.

## How products move to it

1. **Create the repository and its board.** Then move Papyrus's CLI login code into the library, keeping the
   behaviour and the Keychain names unchanged, and keep Papyrus's own specs passing.
2. **Build the new parts once, in the library:** the Linux and Windows stores, the memory agent, the locks,
   remote login, and the broker. Prove the broker on one Papyrus test site.
3. **Papyrus** uses the released library (PPY-5a3624, PPY-109943, PPY-829904).
4. **Cyclotron** builds the `cyclotron` CLI on it (63ef259b and its stories), deploys a broker for the lab, and
   adds a bearer path to the lab API.
5. **Auritus** follows. Its token file moves into a secure store once, and the file is deleted. Whether
   password login stays is the Auritus owners' decision.
6. **Chatticus**, when it has a CLI, deploys a broker and implements the client from the protocol.

Each product still needs its own live changes, and each needs Ryan's approval:
- a callback URL on its Cognito client;
- its broker deployed;
- the two pages on its domain, and its backend's grant to invoke the broker.

## Versions and releases

The library is a Python package released from the new repository, and each product pins an exact version. A
release that changes the protocol bumps the major version. The broker and every client must speak the same
major version, and the broker's answers carry it.
