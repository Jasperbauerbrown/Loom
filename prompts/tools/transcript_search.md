Search what was actually said, across every conversation — full-text over the
transcript. This is episodic recall and it is the last resort: entity notes are what you
know, transcripts are only what happened. Reach for it when the user refers to a
specific past exchange ("what did I tell you about the printer's bed size?") and the
vault has no answer.

Arguments:
- `query` (required): words to look for. Terms are combined; exact wording helps.
- `conversation` (optional): a conversation id to search only that conversation. Omit to
  search all of them, including archived threads.
- `k` (optional, default 8): how many matches to return, 1–50.
- `neighbours` (optional, default false): also return the two events either side of each
  match, so you see the exchange rather than one line.

Messages, tool calls (the tool and its arguments — a command line you ran) and tool
results are all searched, so it finds the exact command that worked last time.

Returns each match with its conversation title, timestamp, event kind and a snippet;
a tool-call match shows the call itself (tool and arguments).
If you learn something durable this way, write it to the vault so the next answer does
not need a search.
