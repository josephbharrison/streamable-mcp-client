# streamable-mcp-client

# Diagram

### **1 · Big‑picture flow**

```mermaid
graph LR
    %% ────────────────────── 1. Python application ─────────────────────
    subgraph Python_Application
        main[main.py]
        streamAgent[StreamableAgent]
        mux["StreamableAgentStream<br/>(multiplexer)"]

        main --> streamAgent
        streamAgent --> mux
    end

    %% ───────────────────── 2. MCP client wrapper ──────────────────────
    subgraph MCP_Client_Wrapper
        mcpWrap[MCPServerSseWithNotifications]
        mux --> mcpWrap
    end

    %% ────────────────────── 3. Remote MCP server ──────────────────────
    subgraph MCP_Server
        sse["SSE endpoint<br/>notifications/*"]
        mcpWrap --> sse
    end
```

1. **main.py** creates a normal OpenAI Agents Agent but wires in our custom MCPServerSseWithNotifications. It then calls StreamableAgent.run_streamed() for the *long‑running* tool request "stream up to 10 numbers.".

2. StreamableAgent.run_streamed() just forwards to the SDK’s Runner.run_streamed() and wraps the returned RunResultStreaming instance inside a **StreamableAgentStream** multiplexer.

3. **StreamableAgentStream** keeps **two** asyncio tasks running:
  - The agent’s *own* event generator (RunResultStreaming.stream_events()),
  - The **notification stream** produced by MCPServerSseWithNotifications.stream_notifications().
  - It emits a **single, unified** async stream that merges items from both.

4. Every time an SSE **notification chunk** arrives, the multiplexer
  - Exposes it immediately as a ResponseTextDeltaEvent (so the UI can print 1 2 3… in realtime),
  - Copies the text into RunResultStreaming.new_items **right now**,
  - Uses our patched helper Runner.continue_run() to step the outer agent forward **once**.
  - That lets the LLM “see” the latest tool output and decide what to say next while the tool is still running.


---

### **2 · Module‑by‑module cheat‑sheet**

| **file** | **what it adds** |
| --- | --- |
| **mcp_extensions/server_with_notifications.py** | Sub‑class of the SDK’s MCPServerSse that1. opens a **second** in‑memory stream for “logging” notifications,2. exposes a single async generator stream_notifications() that yields both the *tool* notifications (from the SSE endpoint) and any *logging* notifications injected by the server. |
| **mcp_extensions/streamable_agent_stream.py** | The heart of realtime relay.  It:• multiplexes the agent‑events task and the notification‑task.• turns each text chunk into the minimal set of UI events  (ItemAdded → ContentPartAdded → TextDelta → ContentPartDone).• appends a finished MessageOutputItem to run.new_items so the LLM can reference it.• calls **Runner.continue_run()** (the helper we patched into the SDK) to pull exactly *one* semantic event from the still‑running agent and then yields it downstream. |
| **mcp_extensions/streamable_agent.py** | Tiny convenience wrapper: given an Agent and an MCP server it returns a StreamableAgentStream each time you want to make a *streamed* call. |
| **main.py** | Demo / diagnostic script.  Shows:• how to spin up the SSE server,• how to watch both raw model deltas and relay‑injected deltas in the console.Notice the two if branches in the loop: one matches *relay* deltas (plain ResponseTextDeltaEvent), the other matches the model deltas that the SDK wraps inside a RawResponsesStreamEvent. |

---

### **3 · Key methods in**

### **StreamableAgentStream**

| **method** | **purpose** |
| --- | --- |
| stream_events() | Main coroutine. Runs two tasks (agent_task, notif_task), waits on whichever completes first, and yields events. Implements a *grace period* (_GRACE_TICKS) to keep listening for late notifications after the agent itself has finished. |
| _handle_notification() | Given **one** notifications/* payload:1. Converts its text into delta events for the UI.2. Creates a **completed** assistant MessageOutputItem and appends it to the in‑flight run’s new_items.3. Calls Runner.continue_run() once and yields the single returned event (usually another model delta or the final answer). |
| _extract_text_chunks() | 2‑line helper that supports both the assistant‑style {"content":[...{type:"text"}...]} payload and the flat {"data":{"type":"text","text":"…" }} payload shapes. |

---

### **4 · Why we patched the SDK**

### **Runner.continue_run**

- The public SDK lets you **start** a streamed run and then iterate:

```
async for evt in run.stream_events(): ...
```

- But you can’t say “give me just the **next** event and then pause”.
- The realtime relay needs exactly that granularity: after *each* notification chunk it must
  1. Wake the agent,
  2. Wait for **one** event (usually a model delta),
  3. go back to waiting for the next notification.
- continue_run() is therefore a minimal, ~20‑line helper that peeks one item from the internal queue, taking care to propagate errors and to notice when the background task has already finished.


When the SDK one day exposes an official step() / poll() API the patch can be dropped.

---

### **5 · Extending / modifying**

- **Want richer notification payloads** (e.g. images, JSON)?

  Extend _extract_text_chunks() and the UI‑event construction logic.

- **Need longer grace period?**

  Change _GRACE_TICKS (each tick = 100 ms).

- **Skip immediate model reaction** (pass‑through only)?

  Remove the call to Runner.continue_run() in _handle_notification().

- **Multiple concurrent tools?**

  Instantiate one StreamableAgentStream per tool invocation; each manages

  its own multiplexing.


```mermaid
sequenceDiagram
    %% concrete classes / files
    participant Main as main.py run()
    participant SA as StreamableAgent
    participant SAS as StreamableAgentStream
    participant Runner as openai‑agents Runner
    participant MCP as MCPServerSseWithNotifications
    participant SSE as SSE server

    %% 1. construction
    Main ->> SA: run_streamed("stream up to 10 numbers")
    SA   ->> Runner: run_streamed(agent, input)
    Runner -->> SA: RunResultStreaming base_stream
    SA  -->> Main: StreamableAgentStream (SAS)

    Note over Main,SAS: Main now iterates SAS.stream_events()

    %% 2. SAS starts two async tasks
    Note over SAS,MCP: Task A base_stream.stream_events()  Task B MCP.stream_notifications()

    %% 2b. MCP opens the SSE channel
    SAS ->> MCP: subscribe_notifications()
    MCP ->> SSE: HTTP GET /sse
    SSE -->> MCP: 200 OK (event stream)

    %% 3A. normal agent streaming
    SAS ->> Runner: Task A next base_stream event
    Runner -->> SAS: RawResponsesStreamEvent (LLM delta or tool‑call)
    SAS -->> Main: same RawResponsesStreamEvent

    %% 3B. notification streaming
    SSE --) MCP: notifications/number 1
    MCP --) SAS: JSON‑RPC payload 1
    SAS --) Main: ResponseTextDelta "1"

    %% 4. commit chunk and advance agent
    SAS ->> Runner: continue_run(base_stream)
    Runner -->> SAS: next LLM delta
    SAS --) Main: ResponseTextDelta (LLM reply)

    %% …numbers 2‑10 handled the same way…

    %% 5. stream ends
    SSE --) MCP: notifications/stream_end
    MCP --) SAS: stream_end
    Runner -->> SAS: final assistant message
    SAS -->> Main: final assistant message
```
