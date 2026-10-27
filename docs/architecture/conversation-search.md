# Conversation search

The conversation list orders projects by their newest member's last activity, including nested
agents and the Voice project. Empty projects use creation time. Ordinary rows, children and forks
are newest first; search results use relevance within each project. Project identity and expansion
state survive the single-agent row becoming a folder. A single-agent row opens its conversation;
the separate disclosure and project menu remain available. A filtered result never changes the
project's real member count.

## Local retrieval

Settings → Components offers an **off/local** switch and a separate indexing pause. Semantic search
is on by default: with the switch on and no model yet, the indexer starts the model's download by
itself, and after a failed one asks again ten minutes later. Nothing sends conversation text to a
provider. An existing chat provider does not imply permission to send it embedding requests or incur
extra charges, so provider embeddings are not selected implicitly. Exact search needs neither the
optional runtime nor a downloaded model.

The local model is [ruBERT mini FRIDA](https://huggingface.co/sergeyzh/rubert-mini-frida),
MIT-licensed, a seven-layer, 312-wide BERT distilled for Russian and English. It replaced
multilingual E5 small after a comparison on real conversations: better on Russian questions, level
on English ones, and two to three times faster on one CPU core. The upstream repository has no ONNX
file, so the weights (128,791,720 bytes, float32) are our own export, fetched from the single URL
`MODEL_URL` in `daedalus/speech/embedding_catalog.py`; the tokenizer (2,413,691 bytes) is upstream's
file at a pinned revision. Downloads use the same manager, manifests, resumable part files, SHA-256
validation, cancellation and progress as speech models. A file with the pinned size and SHA-256
placed in the model's directory (`<state>/models/embeddings/rubert-mini-frida/`) before a download
starts is taken instead of downloading it, which is how a machine without access to the download host
is installed. Components shows byte progress and offers removal.

The speech extra supplies ONNX Runtime and the small tokenizer binding. Sherpa's bundled runtime
exposes speech operations rather than the general tensor API needed by a text encoder; the Python
ONNX Runtime binding supplies that API. There is no transformer framework, separate model server,
vector database or hosted service. Inference uses one CPU thread, attention-mask mean pooling, L2
normalization, and the model's own prefixes (`search_query: ` and `search_document: `), which the
catalog entry carries. Inputs are cut at 256 tokens: a 1,000-character passage is about 270 tokens,
so this trims only the tail and makes a passage half again as fast to encode.

## Index and ranking

The semantic index covers session titles and text blocks in messages. Tool calls and tool-result
logs remain searchable through FTS; reasoning, images and audio are not embedded. Text is read from
SQLite one block passage at a time: up to 1,000 characters with 128-character overlap. A transcript
or full serialized message is never loaded into Python for indexing or retrieval.

SQLite stores each vector as 312 signed bytes and one scale (a quarter of float32, with no measurable
loss), beside its transcript sequence, block, character offset and the digest of its text. The
model's identity, revision, dimension, token cap and storage form make one space string; a change to
it clears the derived vectors and resets the durable pending queue. Insert/update triggers enqueue
messages; replacement invalidates old vectors and a revision check rejects a stale inference result.
Transcript deletion cascades into vectors and pending cursors; agent deletion also cascades into title
vectors. A title change is picked up by the next pass.

Each pass reads up to 32 queued messages, half the newest and half the oldest so backfill and new
arrivals both progress, encodes what fits in one second and commits the batch in one transaction; a
passage that did not fit stays queued where it was. A passage whose text is already indexed (a
re-imported chat, a message quoted back: about a quarter of a real corpus) reuses the stored vector
instead of being encoded again. Encoding runs on a thread of its own at the lowest scheduling priority
(on Linux), so the backfill takes only CPU time nothing else wants. A shared lock serializes each
pass against agent start, resume and compaction; no pass starts while an agent works or a query is
encoding. Cancellation drains native work before releasing the lock. Pausing indexing preserves query
access to existing vectors. Each passage inference has a ten-second termination timer and each query
inference 800 ms. A failing pass is retried after thirty seconds; three in a row stop the index until
the operator switches it off and on.

Exact retrieval uses the existing contentless `transcript_fts`. Across conversations a message
matches on any word of the query, as a prefix, and BM25 ranks the matches, so a message holding more
of the words, and rarer ones, comes first; function words ("which", "как") and words of one or two
characters are left out unless they are all the query has. Requiring every word, as this once did,
answered almost nothing to a question in plain language. Inside one conversation, whose hits are
shown in transcript order rather than by score, every word is still required. Titles matching the
literal query precede FTS hits; ties are deterministic. At most 200 title and 200 transcript
candidates are read.

Semantic retrieval compares the query with every stored vector, from an in-memory copy of the index
kept per database (about 30 MB for 90,000 passages) and brought up to date by reading only rows added
since the last query; a deletion is noticed by count and the copy is read again. It keeps the best
200 passages and titles by cosine, rejecting scores below the model's floor of 0.15. That floor is
calibrated on real conversations as a line under the noise, not a probability: an unrelated pair
scores 0.18 at the median, the best relevant passage of a question 0.40, and any higher line drops
real answers.

Both lists have one-based ranks. A missing rank contributes zero:

```
passage_score = (exact_rank ? 1 / (60 + exact_rank) : 0)
              + (semantic_rank ? 1 / (60 + semantic_rank) : 0)
session_score = max(passage_score for passages in that session)
```

Weighting the words half lower measured no better, so the weights are equal. An FTS rank
belongs to a transcript message and contributes to its semantic passages. Titles are separate
candidates. Taking the maximum prevents repeated matches in long conversations from accumulating an
advantage. The winning passage provides one redacted, whitespace-collapsed snippet, trimmed to about
180 characters. Contentless FTS supplies only a row id: snippet text comes from a bounded substring of
the transcript row, around the query's longest word, including for exact tool-output matches.

## Limits and disclosure

The endpoint uses the same owner authentication as the rest of the API. Every retrieval query and
the final snippet read join visible sessions with the installation's tenant; an optional project
scope applies to both halves. Returned rows are loaded by their matched ids, so an old conversation
can be found even outside the ordinary listing's newest 200 rows. Deleted sessions cannot disclose
orphaned transcript hits.

Queries are at most 500 characters and return 30 sessions by default, at most 50. Two concurrent
readers are admitted; excess requests receive HTTP 429. At most one query runs embedding inference
at a time, and it never waits for a backfill pass. SQLite uses a dedicated read connection with a
500 ms progress-handler deadline and 50 ms lock wait; bringing the in-memory vectors up to date has
its own five-second budget, which only the first query after a start needs. An exhausted deadline
produces an explicit partial-result notice. Snippet reads get a separate 150 ms deadline. No
unbounded native-work queue is left behind by cancelled requests.

Without a model, while warming, or after inference failure, exact search remains available and the
interface links to Components. Indexing progress and partial coverage are stated separately. A
running backfill does not claim to have searched passages it has not yet embedded.

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
`partial` fields; the embedding model behind it can change without touching them.
