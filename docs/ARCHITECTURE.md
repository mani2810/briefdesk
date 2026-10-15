# BriefDesk architecture

## 1. Request paths

| Path | Transport | Notes |
|---|---|---|
| Browser UI | static files from FastAPI | Same origin as the API, so no CORS. Strict CSP. |
| Browser actions | REST `/api/v1/...` | The access token travels in a header. A 401 triggers one silent refresh, using the httpOnly cookie. |
| Live chat | WebSocket `/api/v1/ws/briefs/{id}` | The client gets a 60-second single-use ticket from `POST /briefs/{id}/ws-ticket`, then sends `hello {after}`. The server replays events from the database, then streams live. |
| MCP clients | stdio → `mcp_server/briefdesk_mcp.py` → REST | Authenticated with a personal API token (`bd_...`, stored only as a SHA-256 hash). |

## 2. Agents (LangGraph)

```
START → supervisor ─┬─► plan_gate (interrupt: wait for the human)
                    ├─► researcher ──┐
                    ├─► writer ──────┤   every worker → supervisor
                    ├─► reviewer ────┤
                    ├─► verifier ────┘
                    └─► FINISH → finalize → END
```

**The Supervisor** works from a deterministic state machine (`allowed_moves`) that lists the legal next steps.
- **When there's only one legal step**, the Supervisor takes it without an LLM call.
- **At real choice points**, the LLM chooses from the allowed steps and explains why. Examples: research more or write now; fix unsupported citations or publish.
- **Invalid answers are overridden.** If the LLM answers outside the allowed steps, or is unavailable, the policy decides.

**The Researcher** runs a ReAct loop with tool calling.
- **Tools:** `web_search`, `read_page`, plus every MCP tool that is ready.
- **Bounded:** it stops after a set number of steps, set by depth: 3, 5 or 8.
- **Compact history:** older tool results are shrunk to one line before each call.
- **Fallback:** if it read fewer than 2 pages, the system opens the best results itself, without the LLM.
- **Notes:** a structured `Findings` call turns page excerpts into cited notes. Citations to sources that don't exist are dropped.

**The Writer** streams Markdown and may only cite numbers from AVAILABLE SOURCES.
- **Length scales with the notes:** thin notes produce a shorter report, never padding.
- **Gap signal:** it may answer `INSUFFICIENT:` while research rounds remain.

**The Reviewer** returns one of three verdicts:
- **approve**
- **research:** facts are missing; only allowed while research rounds remain.
- **rewrite:** writing, structure or citations need work. It is never used to ask for facts the notes don't contain.

**The Verifier** (`agents/verifier.py`) checks each `(sentence, [n])` pair.
- **Word overlap:** it finds the passage in source n that best matches the sentence and scores the overlap of content words.
- **Strict numbers:** a number in the claim that is missing from the source halves the score.
- **Second opinion:** weak and unsupported claims get one batched LLM check.
- **Fix loop:** if problems remain, the Writer gets the list of failing sentences.

**`finalize`** re-verifies the final draft if it changed since the last check, so every published version carries a verification result. It then appends the Sources list (cited sources only) and saves `report_versions` and `citations`.

**Human in the loop:**
- **Plan approval:** `interrupt()` pauses the graph; `Command(resume=...)` continues it.
- **Steering:** messages typed during a run go to the `steer_notes` table, and the Supervisor pulls them in at its next step.
- **Revisions:** a revision re-enters the same thread with `{revision, last_worker: "revision"}` and keeps all notes, sources and the draft.

## 3. Conversation agent (`services/chat.py`)

```
message → keyword fast path (approve / cancel / retry / export-with-format / greeting)
        → otherwise ChatIntent from the router model, given the brief's state
        → otherwise rules (if the model is unavailable)
        → normalised by state (e.g. "revise" while running becomes "steer")
        → action: start · steer · approve_plan · edit_plan · question · revise · export · cancel · retry
```

Questions are answered from the report plus the best-matching passages of the sources, streamed as deltas.

## 4. Publisher agent (`agents/publisher.py`)

The Publisher turns a request into a file in five steps:
1. **understand:** the request becomes an `ExportSpec` (keywords first, then the LLM).
2. **normalise:** aliases are resolved, and impossible formats get honest substitutions.
3. **select:** the scope is the full report, a summary (LLM) or one section (fuzzy heading match).
4. **shape:** the content is reshaped for the format (details below).
5. **render:** a renderer in `renderers/` produces the file.

Step 4 in detail:
- **Slides** get a `SlideDeck`.
- **Images** get an `Infographic` (stats, points, and a chart only when the report has comparable numbers).
- **Spreadsheets** get `DataTables`.
- **If the LLM is unavailable,** deterministic shapers build the same structures from the Markdown, so an export never fails because of the model.

## 5. LLM gateway (`llm/gateway.py`)

Every call passes the same guards, in this order:
1. **Run budget:** a `RunContext` counts calls per run, enforcing `RUN_MAX_LLM_CALLS`.
2. **Cache:** a TTL cache keyed by model, prompt version and input.
3. **Circuit breaker per model:** closed → open → half-open (one probe).
4. **Token pacer per model:** a sliding 60-second window. It waits, telling the user in the chat, instead of triggering a 429.
5. **Bounded retry:** only transient errors are retried, honouring Retry-After. Malformed structured output is retried once. Bad keys and unknown models are never retried; they open the breaker for 10 minutes.

**Model tiers:**
- **router:** qwen, for short structured decisions.
- **fast:** gpt-oss-20b, for ReAct tool calling.
- **large:** gpt-oss-120b, for planning, writing, reviewing and publishing.

Each model has its own Groq free-tier allowance.

**Offline mode:** `LLM_MODE=fake` swaps in `llm/fake.py`, which follows the same schemas, tool calls and streaming. The tests, CI and `run.py --demo` use it.

## 6. Data

| Table | Purpose |
|---|---|
| `users`, `refresh_tokens`, `api_tokens` | auth |
| `briefs` | one conversation and its evolving report (status, plan, current markdown, version, verified ratio, usage) |
| `runs` | one per research, revision, answer, export or plan-edit job: status, error, LLM calls, tokens |
| `events` | the chat timeline (`message`, `trace`, `plan`, `report`, `file`, `error`, `draft_start`); replay source |
| `sources` | numbered `[n]` per brief, with the extracted text kept for verification |
| `report_versions`, `citations` | every version and every checked citation |
| `steer_notes` | user guidance waiting for the Supervisor |
| `files` | exports; random ids; downloads checked against owner/admin and kept inside `DATA_DIR` |

LangGraph checkpoints live in a separate SQLite file (`CHECKPOINT_DB`), with thread id `brief-<id>`.

## 7. Scaling notes

This is a single process: in-memory rate limits, an in-process event hub, thread pools and SQLite. To run several instances:
- **Hub and rate limits:** move the event hub to Redis pub/sub, and the rate limits to Redis.
- **Database:** use PostgreSQL, with `langgraph-checkpoint-postgres` for the checkpoints.
- **Jobs:** run the agent pool as separate workers fed by a queue.

The service boundaries (`runner`, `hub`, `briefs`) were drawn with that move in mind.

## 8. Known limitations

- **SSRF check timing:** the fetcher resolves DNS, checks the addresses, then lets httpx connect. A DNS-rebinding attacker could change the answer in between. A pinned-IP transport would close that gap.
- **Fuzzy verification:** the verifier is word-overlap based, with a light LLM check. It catches unsupported figures and made-up claims well. It isn't a full entailment model.
