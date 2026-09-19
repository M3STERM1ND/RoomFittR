# E9 — browser recording spike

Throwaway page for `implementation-plan.md` §3.10 E9 / §8 Phase 0. It is deliberately
**not** part of `apps/web`: it has no build step, no dependencies and nothing here
ships. When Phase 6 builds the real capture UI, this directory can be deleted — the
conclusions live in [`findings.md`](./findings.md).

## What it measures

| Section | Question it answers |
| --- | --- |
| 1 Environment | Is this a secure context with `getUserMedia` and `MediaRecorder` at all? |
| 2 Codec support | Which containers/codecs does `MediaRecorder.isTypeSupported` claim? |
| 3 Camera | What resolution/fps does the device *actually* deliver vs. what we asked for (§3.2 needs ≥720p, ≥24 fps)? |
| 4 Recording | Does a 3-minute capture survive backgrounding and screen lock? What bitrate, and does it fit the 750 MB cap? Does the file play back? |
| 5 Upload | Can the browser slice a 100 MB+ blob into 8 MB parts and resume after a network drop? |
| 6 Motion | Is the optional `DeviceMotionEvent` sidecar (§3.2) available, and does iOS grant permission? |

Each section writes into one result object; **Export JSON** dumps it for pasting into
`findings.md`.

## Running it

`getUserMedia` requires a secure context. `localhost` counts; a phone on your LAN does
not. So:

**On a desktop browser**

```sh
npx --yes serve spikes/e9-browser-recording
# open http://localhost:3000
```

**On a phone** — needs real HTTPS. Either:

```sh
# a) tunnel (easiest)
npx --yes serve spikes/e9-browser-recording &
npx --yes localtunnel --port 3000

# b) local HTTPS with a trusted cert
npx --yes local-ssl-proxy --source 8443 --target 3000
```

Then open the URL on the device and grant camera access.

## Running the 3-minute test properly

The 30-second option only checks that recording starts. The finding that matters comes
from the 3-minute run, and only if you interrupt it:

1. Start the 3-minute recording.
2. At ~0:30 switch to another app for 10 seconds and come back.
3. At ~1:15 lock the screen for 10 seconds and unlock.
4. At ~2:00 pull down the notification shade.
5. Let it finish on its own. Do not press Stop.

Then check: did it reach 3:00, are there stalls, and does the playback element play the
file the same browser just produced? Every interruption is timestamped in the export.

## Devices to cover

§3.10 E9 names iOS Safari, Android Chrome, and desktop Chrome/Safari/Firefox. iOS Safari
is the one that decides the answer — everything else is expected to work.
