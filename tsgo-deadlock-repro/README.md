# tsgo LSP Deadlock Reproduction

A minimal, self-contained reproduction kit for the deadlock in `tsgo --lsp --stdio` under burst load during multi-file operations (e.g., VSCode Replace All across many files).

## The deadlock

When the LSP server receives many `textDocument/didOpen` + `textDocument/codeAction` requests in rapid succession:

1. **readLoop** blocks pushing incoming requests into the bounded `requestQueue` (capacity 100).
2. **dispatchLoop** processes requests and triggers project-loading, which issues a server→client `window/workDoneProgress/create` request.
3. **projectLoadingProgress.run** waits for the client's response to the progress request.
4. The response can only arrive via **readLoop**, which is blocked (step 1).
5. Result: **deadlock**. The server hangs, all three goroutines block, and no further requests are processed.

The harness here also exposes a related path: `dispatchLoop` itself can block synchronously inside `lspWriter.Write` while flushing a `window/logMessage` notification when stdout's pipe buffer is full — same root cause (the client is back-pressuring), different goroutine.

## Running the reproduction

### 1. Generate a test workspace (TypeScript files with import graph)

```bash
./generate_workspace.sh /tmp/tsgo-repro-ws
```

Override size with `COUNT`:

```bash
COUNT=1000 ./generate_workspace.sh /tmp/tsgo-repro-ws
```

### 2. Run the burst harness

```bash
TSGO=/path/to/tsgo python3 reproduce.py /tmp/tsgo-repro-ws
```

The script will:
- Spawn `tsgo --lsp --stdio`
- Send `initialize`, wait for response
- Send `initialized`
- Send N `didOpen` + N `codeAction` requests as fast as possible
- Wait 30 seconds for responses
- Print a summary; on deadlock, send SIGQUIT to capture a goroutine dump

### 3. Expected output on deadlock

```
Burst writer is BLOCKED after 135 didOpen + 135 codeAction (pipe buffer full → server requestQueue full)
...
REPRO SUMMARY
============================================================
  requests sent:        136
  responses received:   1
  notifications:        21
  server->client reqs:  3
  outstanding requests: 135
  first 5 outstanding:
    id=2  method=textDocument/codeAction
    id=3  method=textDocument/codeAction
    id=4  method=textDocument/codeAction
    id=5  method=textDocument/codeAction
    id=6  method=textDocument/codeAction

>>> DEADLOCK OBSERVED: process is alive but not responding <<<
>>> sending SIGQUIT to pid ... for goroutine dump <<<
>>> goroutine dump written to ./goroutine.txt <<<
```

## Workspace size & timing (Linux, observed)

- **150 files**: server keeps up, all responses in ~0.5s
- **500 files**: server keeps up, all responses in ~1.3s
- **1000 files**: deadlock reproduced, ~135–140 requests sent before blocking

The deadlock is reproducible in roughly 3–5 runs out of 10 at 1000 files (timing-dependent on project-load speed). When it does occur it's within 30s.

## For maintainers — code references

Commit: `6b34cd4bc` ([Fix panic when passing `@` response file argument to CLI (#3859)](https://github.com/microsoft/typescript-go/commits/6b34cd4bcf255e5b959bb94a878772b384dc0fef))

### a. Bounded `requestQueue` declaration

**File:** [`internal/lsp/server.go:60`](https://github.com/microsoft/typescript-go/blob/6b34cd4bcf255e5b959bb94a878772b384dc0fef/internal/lsp/server.go#L60)

```go
requestQueue:          make(chan *lsproto.RequestMessage, 100),
```

Channel capacity is 100; once full, incoming requests from readLoop block on send.

### b. `outgoingQueue` declaration

**File:** [`internal/lsp/server.go:61`](https://github.com/microsoft/typescript-go/blob/6b34cd4bcf255e5b959bb94a878772b384dc0fef/internal/lsp/server.go#L61)

```go
outgoingQueue:         make(chan *lsproto.Message, 100),
```

Output messages queue, also capacity 100; dispatchLoop tries to send log messages here and blocks if full.

### c. `readLoop` sends to `requestQueue`

**File:** [`internal/lsp/server.go:420–475`](https://github.com/microsoft/typescript-go/blob/6b34cd4bcf255e5b959bb94a878772b384dc0fef/internal/lsp/server.go#L420-L475)

```go
func (s *Server) readLoop(ctx context.Context) error {
	// ... read loop body ...
	} else {
		req := msg.AsRequest()
		if req.Method == lsproto.MethodCancelRequest {
			s.cancelRequest(req.Params.(*lsproto.CancelParams).Id)
		} else {
			s.requestQueue <- req  // ← blocks when queue is full
		}
	}
}
```

Line 474: readLoop blocks sending incoming requests into the bounded queue.

### d. `dispatchLoop` calls `handleRequestOrNotification`

**File:** [`internal/lsp/server.go:494–538`](https://github.com/microsoft/typescript-go/blob/6b34cd4bcf255e5b959bb94a878772b384dc0fef/internal/lsp/server.go#L494-L538)

```go
func (s *Server) dispatchLoop(ctx context.Context) error {
	// ... in select loop ...
	case req := <-s.requestQueue:
		s.lastRequestTimeMs.Store(time.Now().UnixMilli())
		// ...
		if doAsyncWork, err := s.handleRequestOrNotification(requestCtx, req); err != nil {
```

dispatchLoop blocks on `req := <-s.requestQueue` (line 501) waiting for requests; once processing a request, it calls handleRequestOrNotification synchronously.

### e. `handleRequestOrNotification` calls logger methods

**File:** [`internal/lsp/server.go:664–692`](https://github.com/microsoft/typescript-go/blob/6b34cd4bcf255e5b959bb94a878772b384dc0fef/internal/lsp/server.go#L664-L692)

```go
func (s *Server) handleRequestOrNotification(ctx context.Context, req *lsproto.RequestMessage) (func() error, error) {
	// ... handle handler ...
	if err != nil {
		if _, ok := errors.AsType[userFacingRequestFailedError](err); !ok {
			s.logger.Error("error handling method '", req.Method, "'", idStr, ": ", err)
		} else {
			s.logger.Info("handled method '", req.Method, "'", idStr, " in ", time.Since(start))
		}
		return nil, err
	}
	// ... more logging ...
	s.logger.Info("handled method '", req.Method, "'", idStr, " in ", time.Since(start))
	return nil, nil
}
```

Lines 676, 678, 692: calls to `logger.Error()` and `logger.Info()` synchronously from within request handling.

### f. `lspWriter.Write` calls `bufio.Writer.Flush`

**File:** [`internal/lsp/server.go:134–140`](https://github.com/microsoft/typescript-go/blob/6b34cd4bcf255e5b959bb94a878772b384dc0fef/internal/lsp/server.go#L134-L140)

```go
func (w *lspWriter) Write(msg *lsproto.Message) error {
	data, err := json.Marshal(msg)
	if err != nil {
		return fmt.Errorf("failed to marshal message: %w", err)
	}
	return w.w.Write(data)
}
```

`w.w.Write(data)` calls into `*lsproto.BaseWriter`, which internally performs a synchronous `Flush` to stdout (as seen in the goroutine dump).

### g. `Server.send` and `Server.sendNotification`

**File:** [`internal/lsp/server.go:644–660`](https://github.com/microsoft/typescript-go/blob/6b34cd4bcf255e5b959bb94a878772b384dc0fef/internal/lsp/server.go#L644-L660)

```go
func sendNotification[Params any](s *Server, info lsproto.NotificationInfo[Params], params Params) error {
	return s.send(info.NewNotificationMessage(params).Message())
}

// send writes a message to the outgoing queue, respecting context cancellation.
func (s *Server) send(msg *lsproto.Message) error {
	select {
	case s.outgoingQueue <- msg:
		return nil
	case <-s.backgroundCtx.Done():
		return s.backgroundCtx.Err()
	}
}
```

`sendNotification` calls `send`, which tries to send to `outgoingQueue`; blocks if queue is full.

### h. `logger.sendLogMessage` sends to `outgoingQueue`

**File:** [`internal/lsp/logger.go:25–46`](https://github.com/microsoft/typescript-go/blob/6b34cd4bcf255e5b959bb94a878772b384dc0fef/internal/lsp/logger.go#L25-L46)

```go
func (l *logger) sendLogMessage(msgType lsproto.MessageType, message string) {
	if l == nil {
		return
	}

	if !l.server.initStarted.Load() {
		fmt.Fprintln(l.server.stderr, message)
		return
	}

	notification := lsproto.WindowLogMessageInfo.NewNotificationMessage(&lsproto.LogMessageParams{
		Type:    msgType,
		Message: message,
	})

	select {
	case l.server.outgoingQueue <- notification.Message():
		// sent
	case <-l.server.backgroundCtx.Done():
		fmt.Fprintln(l.server.stderr, message)
	}
}
```

Line 41: select blocks sending log message into `outgoingQueue`; when queue is full, this goroutine parks.

### i. `sendClientRequest[...]` parks waiting for response

**File:** [`internal/lsp/server.go:568–599`](https://github.com/microsoft/typescript-go/blob/6b34cd4bcf255e5b959bb94a878772b384dc0fef/internal/lsp/server.go#L568-L599)

```go
func sendClientRequest[Req, Resp any](ctx context.Context, s *Server, info lsproto.RequestInfo[Req, Resp], params Req) (Resp, error) {
	id := jsonrpc.NewIDString(fmt.Sprintf("ts%d", s.clientSeq.Add(1)))
	req := info.NewRequestMessage(id, params)

	responseChan := make(chan *lsproto.ResponseMessage, 1)
	s.pendingServerRequestsMu.Lock()
	s.pendingServerRequests[*id] = responseChan
	s.pendingServerRequestsMu.Unlock()

	// ...
	if err := s.send(req.Message()); err != nil {
		return *new(Resp), err
	}

	select {
	case <-ctx.Done():
		return *new(Resp), ctx.Err()
	case resp := <-responseChan:
		if resp.Error != nil {
			return *new(Resp), fmt.Errorf("request failed: %s", resp.Error.String())
		}
		return info.UnmarshalResult(resp.Result)
	}
}
```

Lines 590–598: select parks the calling goroutine waiting for response on `responseChan`; only readLoop can unblock it by pushing responses into `pendingServerRequests`.

### j. `projectLoadingProgress.run` calls `createWorkDoneProgress`

**File:** [`internal/lsp/progress.go:110–146`](https://github.com/microsoft/typescript-go/blob/6b34cd4bcf255e5b959bb94a878772b384dc0fef/internal/lsp/progress.go#L110-L146)

```go
func (p *projectLoadingProgress) run() {
	var (
		loading collections.OrderedMap[string, int]
		token   string // current token; empty if no progress active
		tokenID int
		begun   bool // whether "begin" has been sent for the current token
	)
	// ...
	for {
		select {
		case ev := <-p.ch:
			text := p.reporter.localize(ev.message, ev.args...)
			if !ev.finish {
				count := loading.GetOrZero(text)
				loading.Set(text, count+1)
				if token == "" {
					tokenID++
					token = fmt.Sprintf("tsgo-loading-%d", tokenID)
					begun = false
					if p.delay <= 0 {
						delayFired = true
						p.reporter.createWorkDoneProgress(token)  // ← calls into sendClientRequest
```

Line 146: calls `createWorkDoneProgress`, which issues a server→client request and blocks waiting for the response (via `sendClientRequest`).

### k. `projectLoadingProgress.finish` parks on select

**File:** [`internal/lsp/progress.go:99–106`](https://github.com/microsoft/typescript-go/blob/6b34cd4bcf255e5b959bb94a878772b384dc0fef/internal/lsp/progress.go#L99-L106)

```go
func (p *projectLoadingProgress) finish(message *diagnostics.Message, args ...any) {
	select {
	case p.ch <- progressEvent{message: message, args: args, finish: true}:
		// Sent successfully.
	case <-p.reporter.done():
		// Server shutting down; drop the event.
	}
}
```

Line 101: select blocks if `p.ch` is full (buffered at capacity 64 in `newProjectLoadingProgress`); when dispatchLoop triggers project-loading, workers call finish repeatedly, filling the channel.

### l. `Server.ProgressFinish` calls `projectLoadingProgress.finish`

**File:** [`internal/lsp/server.go:333–337`](https://github.com/microsoft/typescript-go/blob/6b34cd4bcf255e5b959bb94a878772b384dc0fef/internal/lsp/server.go#L333-L337)

```go
func (s *Server) ProgressFinish(message *diagnostics.Message, args ...any) {
	if s.projectProgress != nil {
		s.projectProgress.finish(message, args...)
	}
}
```

Called by BFS workers to report project-loading completion; synchronously invokes `finish()`.

### m. `Session.DidOpenFile` calls `UpdateSnapshot` which calls `Snapshot.Clone`

**File:** [`internal/project/session.go:290–313`](https://github.com/microsoft/typescript-go/blob/6b34cd4bcf255e5b959bb94a878772b384dc0fef/internal/project/session.go#L290-L313)

```go
func (s *Session) DidOpenFile(ctx context.Context, uri lsproto.DocumentUri, version int32, content string, languageKind lsproto.LanguageKind) {
	s.cancelWarmAutoImportCache()
	s.scheduleIdleCacheClean()
	s.cancelScheduledSnapshotUpdate()
	s.snapshotUpdateMu.Lock()
	defer s.snapshotUpdateMu.Unlock()
	s.pendingFileChangesMu.Lock()
	s.pendingFileChanges = append(s.pendingFileChanges, FileChange{
		Kind:         FileChangeKindOpen,
		URI:          uri,
		Version:      version,
		Content:      content,
		LanguageKind: languageKind,
	})
	changes, overlays := s.flushChangesLocked(ctx)
	s.pendingFileChangesMu.Unlock()
	s.UpdateSnapshot(ctx, overlays, SnapshotChange{
		reason:      UpdateReasonDidOpenFile,
		fileChanges: changes,
		ResourceRequest: ResourceRequest{
			Documents: []lsproto.DocumentUri{uri},
		},
	})
}
```

Line 306: calls `UpdateSnapshot`; `UpdateSnapshot` (line 1195 in session.go) calls `oldSnapshot.Clone()`, which triggers project BFS.

### n. `BreadthFirstSearchParallelEx` waits on `sync.WaitGroup`

**File:** [`internal/core/bfs.go:64–145`](https://github.com/microsoft/typescript-go/blob/6b34cd4bcf255e5b959bb94a878772b384dc0fef/internal/core/bfs.go#L64-L145)

```go
func BreadthFirstSearchParallelEx[K comparable, N any](
	start N,
	neighbors func(N) []N,
	visit func(node N) (isResult bool, stop bool),
	options BreadthFirstSearchOptions[K, N],
	getKey func(N) K,
) BreadthFirstSearchResult[N] {
	// ...
	processLevel := func(index int, jobs *collections.OrderedMap[K, *breadthFirstSearchJob[N]]) result {
		// ...
		var wg sync.WaitGroup
		i := 0
		for j := range jobs.Values() {
			wg.Add(1)
			go func(i int, j *breadthFirstSearchJob[N]) {
				defer wg.Done()
				// ... visit each node ...
			}(i, j)
			i++
		}
		wg.Wait()  // ← blocks until all workers complete
```

Line 145: `wg.Wait()` blocks the caller (dispatchLoop via the call stack: dispatchLoop → handler → DidOpenFile → UpdateSnapshot → Clone → BFS) waiting for all worker goroutines to finish; workers call `ProgressFinish()` which blocks trying to send to `p.ch`.

## Files

- `generate_workspace.sh` — creates a TypeScript project with N files and cross-file imports
- `reproduce.py` — Python 3 LSP client that fires the burst (stdlib only)
- `README.md` — this file
- `run.log` — timestamped trace of all LSP messages sent/received (created on run)
- `stderr.log` — tsgo's stderr (created on run)
- `goroutine.txt` — goroutine dump from SIGQUIT (created on deadlock)

## Notes

- The harness sends requests as fast as Python can write to a pipe; no artificial delays.
- The burst writer thread may block on `proc.stdin.write()` because the OS pipe buffer fills when the server's bounded `requestQueue` is full.
- The reader thread continues to log every message received, so you can observe the moment responses stop.
- Exit code 0 = all responses received (no deadlock); exit code 2 = deadlock detected.
