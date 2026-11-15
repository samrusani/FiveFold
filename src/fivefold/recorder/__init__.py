"""Model cassettes and run playback (0009).

A run can record every model request and response into a cassette: scripted runs on
request, and live runs always (`fivefold.agents.live`). Replay serves them in order, so
anyone can re-execute a recorded run without an API key or a network, and check that it
does the same as before.

- `cassette`: the cassette format, its digest, and the request hash.
- `recording`: the recording wrapper, a chat model that records the model it wraps.
- `playback`: record a run, and re-execute a recorded run from its cassette and compare
  its records with the original's; `python -m fivefold.recorder` on the command line.

The replay adapter is a model adapter, in `fivefold.agents.replay`. The harness picks a
run's adapter in `fivefold.harness.models`.
"""
