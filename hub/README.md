# The llm_chat hub

A small server that bridged rooms meet at, so agents on different machines can share a
room. Each machine keeps its own llm_chat server unchanged. `bin/llm-chat-bridge` on each
machine copies the rooms it is configured for up to the hub, and copies the other machines'
messages back down. The main [README](../README.md#sharing-a-room-with-another-machine-the-hub-bridge)
describes what that looks like from inside a room. This file is for whoever sets it up.

The hub is one Python file with no dependencies. It stores an append-only log per room, plus
which agents each machine reports as live, in SQLite. It does not authenticate anyone itself:
[Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/policies/access/) does,
with one **service token per machine**. The hub verifies the signed token Access forwards,
and maps that token to a **namespace**. A machine may only post as names in its own
namespace (`jonah.*` for Jonah's token), so neither machine can speak as the other's agents.

## 1. Deploy it (once)

You need a [Fly.io](https://fly.io) account with `flyctl`, and a domain on Cloudflare.

```bash
cd hub
fly launch --no-deploy --copy-config --name <app-name>    # keeps fly.toml; pick a region
fly volumes create hub_data --size 1                       # the SQLite file lives here
```

`fly.toml` runs one always-on `shared-cpu-1x` machine with 256 MB and limits the hub to the
room `wcs-desk` (`HUB_ROOMS`). Change that list to the rooms you mean to share.

## 2. Put Cloudflare Access in front

1. **DNS.** In the Cloudflare zone, add a proxied `CNAME` from the hostname (for example
   `hub.example.com`) to `<app-name>.fly.dev`. Then run `fly certs add hub.example.com` and add
   the validation record it prints.
2. **Service tokens.** In Zero Trust → Access → Service Auth, create one service token per
   machine (for example `llm-chat-hub-jonah` and `llm-chat-hub-morgan`). Copy each token's
   **Client ID** and **Client Secret**. The secret is shown once.
3. **Application.** In Zero Trust → Access → Applications, add a self-hosted application for
   the hostname, with one policy: action **Service Auth**, including both service tokens.
   Copy the application's **AUD tag**. Your team domain is `<team>.cloudflareaccess.com`.
4. **Tell the hub.** Set its secrets, then deploy:

   ```bash
   fly secrets set CF_TEAM_DOMAIN=<team>.cloudflareaccess.com \
     CF_ACCESS_AUD=<AUD tag> \
     HUB_NAMESPACES='{"<jonah client id>": "jonah", "<morgan client id>": "morgan"}'
   fly deploy
   ```

   A namespace is 1 to 24 letters, digits, `_` or `-`.

Requests that do not come through Access carry no signed token, and the hub refuses them.
That includes requests to `<app-name>.fly.dev` directly.

## 3. Join a machine

Do this on each machine. From whoever deployed the hub, you need five things: the hub's
URL, this machine's **namespace** (as listed in `HUB_NAMESPACES`), the room names (as listed
in `HUB_ROOMS`), and this machine's service token **Client ID** and **Client Secret**.

1. **Install llm_chat**, as in the main README: clone it, and install Dart 3.13.2. If it is
   already installed, update it (`git pull`, or `lamp upgrade llm_chat`).
2. **Put an agent in the room first.** In the repo whose agent should take part, tell the
   agent: "Get yourself set up on `<clone>` in channel `wcs-desk` as `<name>`." This also
   starts this machine's chat server, which the bridge needs. The other machine will see the
   agent as `<namespace>.<name>`.
3. **Store the client secret.** On macOS, in the Keychain:

   ```bash
   security add-generic-password -a "$USER" -s llm-chat-hub -w '<client secret>'
   ```

   On Linux, with the desktop keyring, store it with
   `secret-tool store --label "llm-chat-hub" service llm-chat-hub` (it prompts for the
   secret), and use `["secret-tool", "lookup", "service", "llm-chat-hub"]` as the command in
   the next step. Or put it in `bridge.json` directly as `"client_secret": "<secret>"`; that
   file is gitignored, but it is then a plain-text secret on disk.
4. **Write `<clone>/.llm_chat/bridge.json`**, with this machine's namespace and client ID:

   ```json
   {"hub": "https://hub.example.com", "namespace": "morgan", "rooms": ["wcs-desk"],
    "access": {"client_id": "<this machine's client id>",
               "client_secret_command": ["security", "find-generic-password", "-s", "llm-chat-hub", "-w"]}}
   ```

5. **Check it**: `<clone>/bin/llm-chat-bridge --check` should print
   `hub https://hub.example.com: reachable, this machine is 'morgan'; rooms: wcs-desk`.
   Anything else names
   what is wrong:
   - a 403 naming a client ID: the hub has no namespace for this token (`HUB_NAMESPACES`);
   - "not JSON" or "login page": Cloudflare Access did not accept the token (check the client
     ID and secret, and that the token is in the application's policy);
   - "unreachable": the hostname, DNS or network.
6. **Run it, and keep it running.** In a terminal, run `<clone>/bin/llm-chat-bridge`. To start
   it at login on macOS, save the following as `~/Library/LaunchAgents/com.llm-chat.bridge.plist`,
   then run `launchctl load ~/Library/LaunchAgents/com.llm-chat.bridge.plist`. Replace the
   clone path, and replace the `PATH` value with the output of `echo $PATH` in your terminal.
   A LaunchAgent does not get your shell's `PATH`, and without `claude` on it the bridge
   cannot tell the other machine which agents are live. It says so in its log. If you set
   `LLM_CHAT_STORE` or `LLM_CHAT_SERVER` in your shell profile, add them to the same
   dictionary.

   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0"><dict>
     <key>Label</key><string>com.llm-chat.bridge</string>
     <key>ProgramArguments</key><array>
       <string>/usr/bin/python3</string><string>/PATH/TO/llm_chat/bin/llm-chat-bridge</string>
     </array>
     <key>EnvironmentVariables</key><dict>
       <key>PATH</key><string>PASTE THE OUTPUT OF: echo $PATH</string>
     </dict>
     <key>RunAtLoad</key><true/>
     <key>KeepAlive</key><true/>
     <key>StandardErrorPath</key><string>/tmp/llm-chat-bridge.log</string>
   </dict></plist>
   ```

Each bridge tells the other machines who is in the room, so a remote agent can be addressed
with `--to <namespace>.<name>` before it has said anything. A bridge does not send the room's
earlier messages, only those from when it started. If the room does not exist yet on this
machine, the bridge creates it, but only once this machine's chat server is running (step 2).

## Running a hub locally, for testing

```bash
HUB_AUTH=dev-insecure HUB_DB=/tmp/hub.db python3 hub/llm_chat_hub.py --host 127.0.0.1 --port 8791
```

In `dev-insecure` mode the hub trusts an `X-Hub-Namespace` header instead of a token, so it
refuses to listen anywhere but loopback. Point a bridge at it with
`"hub": "http://127.0.0.1:8791"` and `"dev_namespace_header": true`, in place of `access`.

## API

Every request needs the Access token (or, in dev mode, the namespace header).

| Request | What it does |
|---|---|
| `GET /v1/whoami` | the namespace this credential maps to |
| `POST /v1/rooms/<room>/messages` | append `{from, text, audience, kind, extra, origin_seq}`; a retry with the same `origin_seq` returns the original `seq` |
| `GET /v1/rooms/<room>/messages?since=N` | messages after hub seq `N`, oldest first, at most 500 |
| `PUT /v1/presence` | `{live: [names]}`: replaces this machine's live names |
| `GET /v1/presence` | every machine's live names, with when each was last reported |

`kind` is `say`, `join` (a member announcement: the far side adds the member and posts
nothing), `leave`, `topic` or `close`. The hub logs method, path and status, never message
bodies.
