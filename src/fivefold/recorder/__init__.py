"""Model cassettes, run playback and experimental branches (0009).

A run can record every model request and response into a cassette: scripted runs on
request, and live runs always (`fivefold.agents.live`). Replay serves them in order, so
anyone can re-execute a recorded run without an API key or a network, and check that it
does the same as before. An experimental branch replays a recorded run's first K model
calls, then switches to the scripted model or a live one, as a new run with its own
cassette.

- `cassette`: the cassette format, its digest, and the request hash.
- `recording`: the recording wrapper, a chat model that records the model it wraps.
- `playback`: record a run, re-execute a recorded run from its cassette and compare its
  records with the original's, and build a branch of a recorded run;
  `python -m fivefold.recorder` on the command line.

The replay and branch adapters are model adapters, in `fivefold.agents.replay` and
`fivefold.agents.branch`. The harness picks a run's adapter in `fivefold.harness.models`.
"""
