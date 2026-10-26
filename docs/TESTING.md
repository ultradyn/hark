# Running tests without live provider calls

Install the test environment with `uv sync --python 3.13.11 --extra dev`, then
run `uv run pytest`. The normal suite blocks outbound DNS and socket connections
before IO. Loopback and Unix sockets remain available for local fixtures, and
`httpx.MockTransport` stays usable without an opt-in.

For explicitly authorized live provider verification, both conditions are required:

1. Mark the individual test with `@pytest.mark.allow_provider_network`.
2. Run it with `HARK_TEST_PROVIDER_NETWORK=1 uv run pytest <specific-test>`.

The environment variable alone or the marker alone does not enable external
connections. The policy also applies to ordinary Python children and their
children through an inherited `PYTHONPATH` startup bootstrap. It does not alter
`subprocess.Popen` or the production synthesis isolation identity checks.

When a subprocess needs a custom environment or replaces `PYTHONPATH`, use the
`guarded_subprocess_env` fixture rather than discarding the bootstrap:

```python
def test_child(guarded_subprocess_env):
    env = guarded_subprocess_env({"PYTHONPATH": "/path/to/test/shim"})
    subprocess.run([sys.executable, "-c", "..."], env=env, check=True)
```

Existing `sitecustomize.py` startup probes are chained after installing the guard.
Do not use Python `-I`, `-E`, or `-S` for tests that might reach provider code:
those options bypass the inherited startup policy. Native non-Python programs,
deliberately sanitized subprocess environments, and malicious code are outside
this accidental-call guard; explicitly stub their provider boundary. This is a
test harness policy, not a network sandbox.

`allow_media_control` permits tests of mutating media-control commands; it does
not permit provider spend or audible playback. Real sounddevice playback,
including `RawOutputStream` and `OutputStream`, and external audio players are
blocked in the pytest process. Use fake devices and player functions. `ffmpeg`
conversion remains allowed because it does not play sound. A spawned Python
child inherits the provider network guard; stub its playback separately.

B183's historical live Custom STT/TTS verification is not rerun automatically.
Future B183-style live tests must use the dedicated provider marker and explicit
environment variable, with authorization for the live requests.
