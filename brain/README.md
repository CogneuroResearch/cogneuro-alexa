# brain — the conversation loop

Build-order step 4: get the LLM reliably reading, discussing and updating
Linear **before** any microphone is involved. Text in, text out. No audio,
no Whisper, no Piper, no Wyoming — those wrap around this later without
changing it.

```
chat.py            terminal REPL — the assistant without the voice
verify.py          pre-flight checks, one layer at a time
smoke_test.py      offline tests, no keys, no network
conversation.py    history + tool-call round trip + sentence streaming
tools.py           tool schemas and implementations
linear_client.py   thin Linear GraphQL client
llm/
  base.py          the provider interface (one streaming method)
  __init__.py      registry — LLM_PROVIDER picks the adapter
  anthropic_provider.py
  openai_provider.py
```

## Setup

```bash
cd brain
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install anthropic          # or: pip install openai
cp .env.example .env && $EDITOR .env
python verify.py               # checks keys, Linear schema, weather
python chat.py
```

The Linear key comes from Settings › Security & access › Personal API keys.

## Design notes

**The provider is swappable.** Everything above `llm/base.py` speaks a
neutral history format; each adapter translates to its own wire shape.
Adding Gemini or a local model is one file and one line in
`llm/__init__.py` — no changes to the loop, the tools, or the prompt.

**There is no intent parser.** Text goes to the LLM with the tool schemas
attached and the model decides that "bump that up" means `set_priority`.
This is the whole reason for not using Home Assistant's Assist pipeline,
where every new phrasing is a config problem.

**Adding a capability is one function.** Write it, add a `ToolSpec`,
register it in `Toolbox.HANDLERS`. `get_weather` is in here as much to
demonstrate that as to be useful — it needs no API key (Open-Meteo).

**Sentence streaming is built in from the start.** `SentenceStreamer`
turns token deltas into whole sentences, so when Piper arrives it can
start speaking the first sentence while the model is still writing the
third. Retrofitting that later would mean restructuring the loop.

**Issues are addressed by identifier, never UUID.** `ENG-142` is
something a person can say out loud; a UUID is not. `linear_client`
resolves identifiers to UUIDs internally.

**Tool errors are returned to the model, not raised.** A failed Linear
call comes back as `"Linear error: ..."` so the assistant can say what
went wrong instead of the process dying mid-sentence.

## Verified against Linear, September 2026

- auth is the raw personal API key in `Authorization`, no `Bearer` prefix
- `priority` is an integer: 0 none, 1 urgent, 2 high, 3 medium, 4 low
- `dueDate` is a TimelessDate, `YYYY-MM-DD`
- `commentCreate` needs the issue UUID, not the identifier
- workflow state names vary per team, so `set_state` matches by name
  against that issue's own team and reports the options when it can't

`verify.py` re-checks all of this against your account, so if Linear
changes something the failure is one line rather than a mystery.

## What comes next

Once this behaves, step 5 is Whisper and Piper as Wyoming services on the
Pi 5, and `conversation.py` gains audio on either side of it. Nothing in
this directory should need to change for that.
