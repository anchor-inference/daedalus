# Conversation search

The conversation list orders projects by their newest member's last activity, including nested
agents and the Voice project. Empty projects use creation time. Ordinary rows, children and forks
are newest first; search results use relevance within each project. Project identity and expansion
state survive the single-agent row becoming a folder. A single-agent row opens its conversation;
the separate disclosure and project menu remain available. A filtered result never changes the
project's real member count.

## Local retrieval

Settings → Components offers an explicit **off/local** switch and a separate indexing pause.
Nothing sends conversation text to a provider. An existing chat provider does not imply permission
to send it embedding requests or incur extra charges, so provider embeddings are not selected
implicitly. Exact search needs neither the optional runtime nor a downloaded model.

The local model is [multilingual E5 Small](https://huggingface.co/intfloat/multilingual-e5-small),
MIT-licensed, with Russian and English support. Its pinned int8 ONNX weights occupy 118,346,824
bytes and its tokenizer 17,082,730 bytes. Downloads use the same manager, manifests, resumable
part files, SHA-256 validation, cancellation and progress as speech models. The catalog pins the
upstream revision and both file hashes. Components shows byte progress and offers removal.

The speech extra supplies ONNX Runtime and the small tokenizer binding. Sherpa's bundled runtime
exposes speech operations rather than the general tensor API needed by a text encoder; the Python
ONNX Runtime binding supplies that API. There is no transformer framework, separate model server,
vector database or hosted service. Inference uses one CPU thread, attention-mask mean pooling,
L2 normalization, and E5's `query: ` and `passage: ` prefixes. The runtime's tokenizer caps inputs
at 512 tokens.

## Index and ranking

The semantic index covers session titles and text blocks in messages. Tool calls and tool-result
logs remain searchable through FTS; reasoning, images and audio are not embedded. Text is read from
SQLite one block passage at a time: up to 1,000 characters with 128-character overlap. A transcript
or full serialized message is never loaded into Python for indexing or retrieval. A very dense
passage can reach the tokenizer's 512-token cap; lexical coverage is unaffected.

SQLite stores normalized little-endian float32 vectors (384 dimensions, 1,536 bytes each), their
model-space identity, dimension, transcript sequence, block and character offset. A model revision,
pooling or dimension change clears the derived vectors and resets the durable pending queue.
Insert/update triggers enqueue messages; replacement invalidates old vectors and a revision check
rejects a stale inference result. Transcript deletion cascades into vectors and pending cursors;
agent deletion also cascades into title vectors. A title change is picked up by the next pass.

Each pass attempts at most eight units, checking a half-second scheduling budget between them.
Each native inference has an 800 ms termination timer. Oldest and newest pending messages alternate,
so backfill and new arrivals both progress. Each passage commits its cursor independently. Pausing,
restarting, or stopping for agent work therefore resumes at a passage boundary. A shared lock
serializes each background unit against agent start, resume and compaction; the worker checks for
active work under that lock. Cancellation drains native work before releasing the lock. Queries
have priority over the next background unit. Pausing indexing preserves query access to existing
vectors.

Exact retrieval uses the existing contentless `transcript_fts` and the same escaped prefix/phrase
query compiler as `HistorySearch`. Titles matching the literal query precede FTS hits ordered by
BM25; ties are deterministic. At most 200 title and 200 transcript candidates are read. Semantic
retrieval keeps the best 200 passages/titles by cosine similarity, rejecting scores below 0.78.
This is a conservative heuristic, not a probability or a claim that every paraphrase is recognized.

Both lists have one-based ranks. A missing rank contributes zero:

```
passage_score = (exact_rank ? 1 / (60 + exact_rank) : 0)
              + (semantic_rank ? 1 / (60 + semantic_rank) : 0)
session_score = max(passage_score for passages in that session)
```

An FTS rank belongs to a transcript message and contributes to its semantic passages. Titles are
separate candidates. Taking the maximum prevents repeated matches in long conversations from
accumulating an advantage. The winning passage provides one redacted, whitespace-collapsed snippet,
trimmed to about 180 characters. Contentless FTS supplies only a row id: snippet text comes from a
bounded substring of the transcript row, including for exact tool-output matches.

## Limits and disclosure

The endpoint uses the same owner authentication as the rest of the API. Every retrieval query and
the final snippet read join visible sessions with the installation's tenant; an optional project
scope applies to both halves. Returned rows are loaded by their matched ids, so an old conversation
can be found even outside the ordinary listing's newest 200 rows. Deleted sessions cannot disclose
orphaned transcript hits.

Queries are at most 500 characters and return 30 sessions by default, at most 50. Two concurrent
readers are admitted; excess requests receive HTTP 429. At most one query runs embedding inference
at a time. SQLite uses a dedicated read connection with a 500 ms progress-handler deadline and
50 ms lock wait. Vectors are scanned in batches of 256, with a 50,000-vector cap; a larger index or
an exhausted deadline produces an explicit partial-result notice. Snippet reads get a separate
150 ms deadline. No unbounded native-work queue is left behind by cancelled requests.

Without a model, while warming, or after inference failure, exact search remains available and the
interface links to Components. Indexing progress and partial coverage are stated separately. A
running backfill does not claim to have searched passages it has not yet embedded. Project scoping
can narrow a search that reaches the scan limit.

## The agent's chat search

The agent reaches the same service through two tools. `ChatSearch(query, project, limit)` ranks the
operator's other chats by topic, one hit per chat with its id, title, last activity, the seq of the
best passage and a snippet; `RecentChats(n, before, after, project)` lists them by last activity with
what was first asked and the headline the last reply ended with, from SQL alone. `HistoryExpand`
reads either kind of hit back by seq; `HistorySearch` searches only the current chat. Both new tools
are offered while `[memory] search_chats` is on.

Inside a project both tools reach that project's chats; elsewhere they reach the chats outside
projects (every plain chat's scratch project and the installation's own projects, taken together).
Naming another project lists its matching chats with their headlines and without snippets, for when
the operator asks about it. Never included: the asking chat, a chat marked private
(`{"private": true}` in its metadata), and every session that is not the operator talking — a
subagent, a staff member, a schedule, a heartbeat or other unattended run, a prompt coach, a side
question. Imported conversations are included. The rule is one predicate in
`daedalus/search/chats.py` with an SQL twin applied inside both retrieval halves and the snippet
read, and `HistoryExpand` refuses a private chat named by id.

Every answer says which search answered: hybrid when the semantic half was ready, words only (with
the reason) otherwise. The tools see only the service's `query` and its `semantic`, `reason` and
`partial` fields; the embedding model behind it can change without touching them. When no chat holds
every word of a multi-word query, the words half is asked again with any word allowed, and the answer
says so.
