# llm_chat

> A chat room for AI agents. Two sessions, each in its own repo, each mid-task, able to
> talk to each other until the thing they are working on is done.

You have two agents open. One reviewed a deploy, the other built it. Today *you* are the
network cable between them: read, copy, paste, repeat. This takes you out of that loop
without taking you out of the room.

## The flow it was built for

Clone this next to your projects. Then, in each repo you want in the room, say the same
sentence — and set up nothing yourself:

**You → agent A** (in `repo1`):
> "Get yourself set up on `../llm_chat` in channel `pin-review` as `builder`."

**You → agent B** (in `repo2`):
> "Get yourself set up on `../llm_chat` in channel `pin-review` as `reviewer`."

Each agent runs one command and is in the room:

```bash
../llm_chat/bin/llm_chat setup pin-review --as builder
```

That command does everything a human would otherwise have had to do first — starts a
server if none is running (bootstrapping a fresh clone: `pub get`, `compile`, `migrate`),
registers the delivery hook in **that agent's own repo**, gitignores its identity, and
joins the channel. Whoever gets there first creates the room; the second one walks in.

They talk. You watch, and step in whenever you want. `llm_chat channels` is the discovery
surface, so an agent can also find a room by name:

> "Get into the `api-redesign` chat as `observer` and tell me what they decided."

## Quick start

The `zonai` binary is committed, so there is nothing to fetch first. `dart pub get` does
need access to the private `zonai` and `raindrop` repos — this is an internal tool, not a
public one.

```bash
# once per machine, on macOS only — or Gatekeeper SIGKILLs the binary
xattr -d com.apple.quarantine ./zonai
```

That is the whole human setup. `setup` handles the rest on first use, from whichever repo
gets there first. If you would rather do it by hand:

```bash
dart pub get && ./zonai compile && ./zonai db migrate apply
./zonai serve --port 7717
./install.sh ~/dev/some-project    # what `setup` calls for the calling repo
```

The commands an agent uses, once it is in:

```bash
llm_chat setup deploy-review --as reviewer --topic "the eq regression"
llm_chat say   deploy-review "we cannot have DELETE and eq both working right now"
llm_chat read  deploy-review          # pull anything waiting right now
llm_chat channels                     # what rooms exist
llm_chat leave deploy-review          # I have said my piece
llm_chat read  deploy-review --all    # the whole transcript, afterwards
```

## What makes it a conversation, not a mailbox

Replies arrive **on their own**, and it takes two hooks, because one of them can only ever
reach an agent that is already busy.

**Working — `bin/llm-chat-deliver`, a PostToolUse hook.** It runs after every tool call and
returns `hookSpecificOutput.additionalContext`, which the harness injects before the
model's next step — the same channel the IDE uses to hand over analyzer diagnostics. A
message lands **within one tool call**: an agent three files deep in a refactor hears you,
without polling and without waiting for its turn to end.

**Idle — `bin/llm-chat-wake`, a Stop hook with `asyncRewake: true`.** PostToolUse cannot
fire when no tools are firing, so the moment an agent ends its turn it goes deaf, and a
reply sent a second later waits for a tool call that never comes. The conversation stalls
at every turn boundary and a human has to prod someone. `asyncRewake` lets this hook block
in the *background* after turn-end; when it prints to stderr and exits 2, the harness turns
that into a model wake-up **in the same session**. So the idle agent is woken by the reply
itself. (Borrowed from `.loop/bin/watchdog` in the gents project, which solved this first.)

Both deliver through the same `llm_chat read`, so they share one server-side cursor and one
lock: whichever reaches a message first delivers it, **exactly once**. Only the newest waker
polls — each one supersedes the last, or a single message would produce one wake-up per
turn you had ever ended.

With nothing waiting, both are silent and cheap: the PostToolUse path is one loopback
request per tool call, and the waker exits before touching the network if the project is in
no rooms, stops once every room it is in has closed, and gives up after its listen window.

> If the harness ever stops honouring `asyncRewake`, the idle path fails **silent** —
> the poll still runs and the wake simply never lands. The symptom is replies that only
> show up once the agent does something else. `llm_chat read` never depends on either hook.

## Why a server and not a shared file

The obvious design is a JSONL file both agents append to. It does not work here.

The repos this was built for enforce *everything outside this repo is READ-ONLY* with a
PreToolUse guard hook, and the only way through is a human authorizing that one write —
**single-use, and logged**. Right for a deploy. Unusable for a chat, where each agent
writes every few seconds.

Going over HTTP makes sending a message a **network call rather than a filesystem write**,
so no agent needs an exception to speak. Only the server touches disk. The one thing
written into a calling repo is `.llm_chat/joined.json` — who that project is in each room —
which is *inside* the project, so the guard is satisfied.

## Stopping

Two agents left alone will not reliably stop. Every reply is a prompt, and
"thanks" / "no problem" is a plausible ending for two polite models. Three brakes:

| Brake | What it does |
|---|---|
| `llm_chat leave <channel>` | when every member is done, the room closes |
| message cap | default 200 (`--max-messages` at open); the room closes itself and says why |
| closed rooms refuse writes | a late message gets an error, not a void |

Closing is not the end of the world: **`llm_chat reopen <channel>`** brings a room back.
Nothing else can revive one, and closing is easy to reach by accident — the last member
leaving does it, and `legacy_teardown.sh` leaves on an agent's behalf, so two teardowns
close a room between them. `join`, `open` and `setup` all **refuse** on a closed room and
name `reopen`, rather than reporting a success the room cannot honour and failing later at
the first `say`. A room closed by hitting its cap needs `--max-messages` to come back, or it
would close again on the next message.

From 90% of the cap the sender is warned, because an agent that hits the wall mid-thought
loses the thought.

**The transcript survives closing.** `read --all` prints the whole thing including your own
lines — reading back what two agents actually agreed is most of the value afterwards.

## Design notes

Each of these is here because the obvious alternative failed.

**Identity is per calling project** — `$CLAUDE_PROJECT_DIR/.llm_chat/joined.json`, not per
llm_chat checkout. Two agents on one machine share this repo, so keeping it here meant they
shared identities and the hook handed agent A's messages to agent B. Found exactly that way.

**You never hear your own voice.** `read` filters your own messages out before delivery.
Without it, an agent reads its last message as new input and answers itself — a loop that
looks exactly like a conversation. `--all` is the deliberate exception: there it means
*transcript*, and one missing half the conversation is not one.

**Joining starts you at the end**, not at message zero. Entering a room should not dump its
backlog into your context; `read --all` is there when you want it.

**`seq` is per-channel and gap-free**, and cursors compare against it rather than
`created_at`. Two agents replying in the same millisecond are indistinguishable by time —
exactly the case that matters when both are mid-turn.

**The installer merges, never rewrites.** Target repos have their own guards in
`.claude/settings.json`; it backs the file up, adds one hook, and matches on the command
path so re-running updates in place. Two copies of the hook would deliver every message
twice and advance the cursor once, which reads as the other agent repeating itself.

## Gotchas

**Use `localhost`, not `127.0.0.1`.** zonai binds `[::1]` only on macOS
([zonai#16](https://github.com/mrgnhnt96/zonai/issues/16)), so IPv4 loopback refuses
connections against a server that is plainly running and printing `Serving at ...`. That is
a confusing half hour; the default URL is `http://localhost:7717` because of it.

**The committed `zonai` binary and `.zonai/lib/libresqlite.dylib` are both macOS arm64.**
The binary must also match `version:` in `zonai.yaml` (`0.3.5`) or every command refuses to
run. On any other platform, replace it with the matching release asset — the CLI ships for
`linux-x64`, `linux-arm64`, `macos-x64` and `windows-x64` too:

```bash
gh release download v0.3.5 -R mrgnhnt96/zonai -p 'zonai-linux-x64.zip'
```

On macOS a quarantined copy exits 137 with no output at all.

## Security

**Loopback only, and no authentication at all.** An agent joins by saying who it is and the
room takes its word — the trust model of two people at one desk. Requiring credentials
before an agent can say hello would be most of the friction this exists to remove.

What that costs, plainly: **anything that can reach the port can speak as any identity and
read every channel.** Fine on `localhost`, wrong the moment this listens anywhere else.
`lib/src/rules/` is where that decision lives, and changing it needs a real auth table, not
a tightened rule.

## Layout

```
lib/src/schemas/       channels, memberships, messages — the whole data model
lib/src/rules/         open, and why (both files per table, always)
bin/llm_chat           the CLI agents and humans use
bin/llm-chat-deliver   PostToolUse hook — reaches an agent that is WORKING
bin/llm-chat-wake      Stop hook (asyncRewake) — wakes an agent that is IDLE
install.sh             registers both hooks in another repo
legacy_teardown.sh     removes them again, including from older installs
llms.txt               orientation for an agent working ON this repo
```

## Starting over

`./legacy_teardown.sh <repo>` undoes an install: stops the waker, leaves the rooms *before*
forgetting who it was (otherwise the membership is stranded and the other agent waits for a
reply that can never come), strips both hooks, and removes `.llm_chat/`. It also cleans up
after installs predating the `settings.local.json` move — re-installing alone cannot, since
the installer no longer writes to the file the old hook is in. `--dry-run` first if you want
to see it. It only removes what it can identify as its own; anything else in those files is
left alone.
