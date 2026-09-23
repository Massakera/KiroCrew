# Factory Droid frame corpus

Three files: two live captures and one synthesized turn. Read `../README.md` first
for what a fixture is and what the corpus does and does not prove.

| File | Provenance | Frame classes it carries |
|---|---|---|
| `handshake-live.jsonl` | live | `initialize` answer with `agentInfo.version` and both auth methods, `session/new` with one stdio `mcpServers` element, the `autonomy_level=normal` write and its `current_mode_update` / `config_option_update`, `session/list` |
| `auth-failure-live.jsonl` | live | a `session/prompt` rejected by Factory with a 401: an `agent_message_chunk` naming it and a `-32603` error carrying the status |
| `turn-synthesized.jsonl` | **synthesized** | `tool_call`, `session/request_permission`, `tool_call_update`, `agent_message_chunk`, `stopReason` |
| `session-load-live.jsonl` | live (0.225.2) | a second adapter process: `initialize`, `session/list`, `session/load` replaying the first process's session as `user_message_chunk` and answering with `configOptions` and no `modes`, an unknown id refused with `-32602` |
| `steer-refused-live.jsonl` | live (0.225.2) | `_session/steering` and `_session/steer` both answered `-32601` |
| `compact-as-prompt-live.jsonl` | live (0.225.2) | a `/compact` prompt sent to Factory as an ordinary turn (401 with the placeholder key), not handled as a command |

## What the live captures establish

Captured off `droid exec --output-format acp` 0.225.1 with an isolated `HOME` and a
placeholder `FACTORY_API_KEY`; no real credential was involved and none appears here.

- **Routing is `SESSION_CONFIG`.** `session/new` advertises a `select` config option
  `autonomy_level` whose `normal` value "auto-approves only read operations", and
  `session/set_config_option` accepts that value and echoes it back. That is the
  option Crew arms before the first prompt.
- **An unstartable stdio element does not fail `session/new`.** The session is
  created although the element's command is not an MCP server.
- **A key is accepted at `session/new` and rejected at the first turn.** With
  `FACTORY_API_KEY` set the harness needs no `authenticate` call; a bad key surfaces
  only as the 401 in `auth-failure-live.jsonl`. With no key and no stored login,
  `session/new` answers `-32000 Authentication required`.

## What is still missing: a signed-in turn

`turn-synthesized.jsonl` is written from the ACP v1 schema. The fact it stands in
for -- that a write under `normal` really is raised as `session/request_permission`
-- is the one the harness stays out of the selectable baseline for. To replace it,
on a host where `droid` is signed in (or `FACTORY_API_KEY` is set):

```bash
export KIROCREW_ACP_RECORD_FRAMES=/tmp/droid-frames
export KIROCREW_EXPERIMENTAL_BACKENDS=droid
kirocrew gateway   # then, in a chat, spawn a subagent with backend "droid"
                   # asking it to create a file, and approve the prompt
```

Split `/tmp/droid-frames/droid.jsonl` into `turn-live.jsonl` (keep it under 50
frames, add the `_meta` header, review it for anything private), delete
`turn-synthesized.jsonl`, and regenerate the snapshots with
`python3 scripts/update_acp_frame_snapshots.py`.
