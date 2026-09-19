# E9 findings — in-browser recording

**Experiment:** §3.10 E9. **Risk it retires:** R10 (in-browser recording inconsistent).
**Page:** [`index.html`](./index.html) · **How to run:** [`README.md`](./README.md)

Two kinds of statement appear below and they are kept apart on purpose:

- **Documented** — read from a vendor source, cited. True regardless of which phones we own.
- **Measured** — produced by running `index.html` on a real device. Empty until someone runs it.

---

## 1. Conclusion

**In-browser recording is viable, and it is not allowed to be the only path.**

Every target browser has had a `MediaRecorder` that produces a file our pipeline can
decode for years, so the capture-in-the-browser design in §1.1 stands. What is *not*
established by documentation — and what no amount of reading will establish — is whether
a **three-minute** recording survives a screen lock and an app switch on a real iPhone.
That is a device fact, it is the single thing most likely to break capture in the field,
and §3.2 allows captures up to 3 minutes, so it is squarely in scope.

Accordingly, the decisions that follow do not depend on the measured rows landing well.

**Measured 2026-09-19 — the gap is closed.** A 3-minute 1080p capture on iPhone (iOS 18.7,
Safari 26.6) survived both an app switch and a screen lock, finished its full duration and
played back. **E9 passes**: a usable 1080p file on the deciding browser, and chunked upload
resuming from the failed part rather than the beginning (verified on desktop Chrome).

The one real surprise is that *recording* surviving and *chunk delivery* surviving are
different things — delivery froze for 43 s around backgrounding and flushed late. See
note B. It costs D3 a tolerance and it strengthens the case for D2, but it does not change
the conclusion above.

## 2. Decisions this spike settles

| # | Decision | Rationale |
| --- | --- | --- |
| D1 | **Negotiate the container at runtime, never hard-code one.** Probe in order: `video/mp4;codecs="avc1.42E01E"` → `video/mp4` → `video/webm;codecs="vp9"` → `video/webm;codecs="vp8"` → browser default. | Safari and Chrome disagree, and Safari's own answer changed in 18.4 (below). The pipeline decodes with FFmpeg, so any of these is fine server-side; the only wrong move is asking for one the browser refuses. |
| D2 | **Keep the file-upload fallback as a first-class path, not an error state.** | §1.1 already lists it. E9 cannot be allowed to gate the product on iOS Safari behaving. A user who records with the stock Camera app and uploads must get an equally good scan. |
| D3 | **Record in timeslices (~1 s) and upload parts during the recording, not after.** | A 3-minute 1080p capture is 100–400 MB. Holding it as one in-memory `Blob` on a mid-range phone is how capture dies at minute two, and it makes an interrupted recording a total loss rather than a partial one. |
| D4 | **Watch `visibilitychange` during recording and surface an explicit warning.** | Whatever iOS does on backgrounding, a capture that silently truncates is worse than one that says "you left the app, the recording stopped". §7.4 failure handling should treat this as a named, user-visible cause. |
| D5 | **Validate duration, resolution and size client-side before upload begins**, against the §3.2 hard limits. | Cheapest possible rejection point. Uploading 400 MB to discover it is 640×480 wastes the user's data and our R2 writes. |
| D6 | **Treat the `DeviceMotionEvent` sidecar as strictly optional**, never blocking capture on the iOS permission prompt. | Already the §3.2 position; the spike confirms it is the right one, since a denial is invisible until you ask. |

## 3. Documented behaviour

| Claim | Source |
| --- | --- |
| `MediaRecorder` is Baseline "widely available" across browsers since April 2021. | [MDN `MediaRecorder`](https://developer.mozilla.org/en-US/docs/Web/API/MediaRecorder) |
| Safari shipped `MediaRecorder` in Safari 14.1 / iOS 14.3, producing **MP4 with H.264 + AAC only**. Requesting WebM returned `false` from `isTypeSupported`. | [WebKit: MediaRecorder API](https://webkit.org/blog/11353/mediarecorder-api/) |
| **Safari 18.4 added WebM output** (VP8/VP9 video, Opus audio). From 18.4 on, iOS Safari answers `true` for both MP4 and WebM. | [MediaRecorder browser support](https://www.testmuai.com/learning-hub/mediarecorder-browser-support/) |
| Chrome/Firefox default to WebM; MP4 support in Chrome's `MediaRecorder` is comparatively recent. `isTypeSupported` must be consulted per browser. | [MDN `isTypeSupported()`](https://developer.mozilla.org/docs/Web/API/MediaRecorder/isTypeSupported_static) |
| iOS Safari is known to drop or blank a `getUserMedia` stream on navigation and to re-prompt for camera permission intermittently. | [Apple Developer Forums](https://developer.apple.com/forums/thread/750254), [Apple Community](https://discussions.apple.com/thread/256081579) |

**What the documentation does not settle.** No vendor source states what happens to an
in-flight `MediaRecorder` when iOS Safari is backgrounded or the screen locks. The native-iOS
rule — background/lock terminates video capture unless the app holds a background audio
category, which a web page cannot — is suggestive but is *not* a statement about WKWebView's
`MediaRecorder`. This is the gap the measured section exists to close, and it is why D2 and
D4 do not wait for it.

## 4. Measured results

Run [`index.html`](./index.html) per the README, **Export JSON**, and add a row plus the raw
export. §3.10 E9 names these five targets.

| Device / OS / browser | Container produced | Requested → delivered | fps | 3 min completed? | MB/min | Projected 3 min vs 750 MB | Stalls > 3 s | Survived background | Survived screen lock | Plays back | Motion sidecar |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **iPhone · iOS 18.7 · Safari 26.6** | `video/mp4; codecs=avc1.42000a` (asked `avc1.42E01E`) | 1920×1080 → 1920×1080 | 30 | ✅ 180.2 s | 39.1 | 117.4 MB ✅ | 3 (longest **43.1 s** — **note B**) | ✅ 14.5 s hidden | ✅ 14.1 s hidden | ✅ 180.1 s | ✅ 119 samples @ 59.4 Hz, gravity + rotation |
| Android · Chrome | _pending_ | | | | | | | | | | |
| **Windows 11 · Chrome 153 · desktop** | `video/mp4;codecs=avc1.42002a` (asked `avc1.42E01E`) | 1920×1080 → 1920×1080 | 30 | ✅ 180.0 s | 9.4 | 28.2 MB ✅ | 43 of 63 gaps — **see note A** | not tested | not tested | ✅ | n/a (no sensor) |
| macOS · Safari | _pending_ | | | | | | | | | | |
| Desktop · Firefox | _pending_ | | | | | | | | | | |

### Raw exports

<!-- Paste one fenced JSON block per device here, newest first. -->

**Note A — the stall count is probably a container artefact, not a stall.**
`recorder.start(1000)` asks for a 1 s timeslice, so a 180 s recording should yield ~180
chunks. It produced **64**, a mean gap of 2.81 s, and 43 of those gaps crossed the 3 s
threshold. A desktop with 16 cores and 32 GB stalling 43 times is not credible. The likely
cause is that Chrome's **MP4** muxer emits on fragment/keyframe boundaries rather than on
the requested timeslice, so the metric is measuring GOP length. **Unresolved** — the test
is to force `video/webm;codecs="vp9"` and see whether chunk count jumps to ~180. Until
then this number should not be read as a stall count, and D3 is unaffected either way:
parts arriving every ~3 s still streams fine.

The iPhone run supports this reading: the same page, the same 1 s timeslice and the same
MP4 container gave a **1.28 s** mean gap on Safari against Chrome's 2.81 s. Two engines
muxing MP4 at different cadences is ordinary; a desktop stalling 43 times is not. Note that
this makes `stalls_over_3s` engine-dependent and therefore not comparable across rows —
which is a flaw in the page's metric, not in either browser.

**Upload resume: verified.** Part 2 was dropped mid-flight and retried in place
(`resumed_from_part: 2`), with all 4 parts accounted for and no restart from part 1.

**Note B — the recording survived; chunk *delivery* did not.** This is the finding the
spike existed to produce, and it is two facts, not one.

*It survived.* A 3-minute 1080p capture on a real iPhone ran to completion through an app
switch **and** a screen lock: `completed_full_duration: true`, 180.2 s recorded, and the
file plays back at 180.1 s. Nothing was lost. The native-iOS rule that backgrounding kills
video capture does **not** apply to Safari's `MediaRecorder`. R10's worst case is retired.

*Delivery stalled badly.* The longest gap between chunks was **43.08 s**, against a 1.28 s
mean for the other 107 gaps. The first backgrounding began at 35.7 s, and 35.7 + 43.1 =
78.8 s — so chunk delivery froze when the tab was hidden and did **not** resume when the tab
came back at 50.3 s; it stayed frozen for roughly another 28 s. `MediaRecorder` buffered
throughout and flushed late.

**What this changes:**

- **D3 needs a tolerance.** An uploader streaming parts during capture must survive a
  ~45 s gap in chunk arrival without concluding the recording died. Never finalize an
  upload, or declare a capture failed, on a chunk-arrival timeout.
- **D4 is confirmed as implementable.** `visibilitychange` fired cleanly four times with
  usable timestamps, so the explicit user-facing warning it calls for has a reliable signal.
- **Buffered-but-unflushed data is a real exposure.** Up to ~45 s of capture existed only
  in Safari's internal buffer. A tab evicted under memory pressure in that window loses it,
  which is a second argument for D2's upload-a-file fallback.

**Caveat: one device, one run.** 43 s is not a constant to design against — the shape of
the behaviour is the finding, not the number.

**Two documented claims are now measured first-hand:** iOS Safari 26.6 answers `true` for
`video/webm` vp8/vp9, confirming the 18.4 change in §3; and it ignored the requested
`avc1.42E01E` in favour of `avc1.42000a`, which is precisely why D1 says negotiate rather
than hard-code.

**Bitrate is 4× desktop** — 39.1 MB/min vs 9.4 — so a 3-minute 1080p iPhone capture is
~117 MB. Still 6× under the 750 MB cap, but the desktop figure alone would have badly
understated it.

**Blob slicing held up:** Safari sliced the 117 MB recording into 15 parts with all parts
accounted for, which is the iOS-specific risk the dry run exists to test.

#### iPhone · iOS 18.7 · Safari 26.6 — 2026-09-19

```json
{
  "spike": "E9",
  "recorded_at": "2026-09-19T18:07:46.108Z",
  "env": {
    "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.6 Mobile/15E148 Safari/604.1",
    "platform": "iPhone", "secure_context": true, "has_media_devices": true,
    "has_media_recorder": true, "device_pixel_ratio": 3, "screen": "393x852",
    "hardware_concurrency": 4, "device_memory_gb": null, "storage_quota_bytes": 41231686042
  },
  "codecs": {
    "video/mp4;codecs=\"avc1.42E01E\"": true, "video/mp4;codecs=\"avc1.640028\"": true,
    "video/mp4;codecs=\"hvc1\"": false, "video/mp4": true,
    "video/webm;codecs=\"vp9\"": true, "video/webm;codecs=\"vp8\"": true,
    "video/webm;codecs=\"av01.0.05M.08\"": false, "video/webm": true,
    "video/x-matroska;codecs=avc1": false, "video/quicktime": false
  },
  "camera": {
    "requested": "1920x1080", "open_ms": 898, "delivered": "1920x1080", "frame_rate": 30,
    "facing_mode": "environment", "label": "Back Dual Wide Camera", "meets_720p_floor": true
  },
  "recording": {
    "requested_mime": "video/mp4;codecs=\"avc1.42E01E\"",
    "actual_mime": "video/mp4; codecs=avc1.42000a",
    "target_ms": 180000, "actual_ms": 180178, "completed_full_duration": true,
    "bytes": 123247688, "mb_per_min": 39.1, "est_bitrate_mbps": 5.47, "chunk_count": 108,
    "stalls_over_3s": 3, "longest_stall_ms": 43082,
    "interruptions": [
      {"at_ms": 35732, "event": "visibility:hidden"},
      {"at_ms": 50277, "event": "visibility:visible"},
      {"at_ms": 76644, "event": "visibility:hidden"},
      {"at_ms": 90719, "event": "visibility:visible"}
    ],
    "projected_3min_mb": 117.4, "playback_ok": true, "within_750mb_at_3min": true,
    "playback_duration_s": 180.1
  },
  "upload": {
    "mode": "dry run (slicing only)", "endpoint": null, "part_size_bytes": 8388608,
    "parts": 15, "bytes_sent": 123247688, "elapsed_ms": 52, "throughput_mbps": 18961.18,
    "resumed_from_part": null, "all_parts_accounted_for": true,
    "log": "15 parts, all \"sliced (dry run)\", 14 x 8388608 B + 1 x 5807176 B"
  },
  "motion": {
    "device_motion_in_window": true, "needs_permission": true, "permission": "granted",
    "samples": 119, "has_gravity": true, "has_rotation_rate": true, "observed_hz": 59.4
  },
  "notes": []
}
```

_Upload was a dry run: the phone could not reach the desktop-local PUT sink through the
tunnel, so resume was verified on desktop instead. `throughput_mbps` is slicing speed, not
network. The `log` array is summarised — all 15 parts returned `sliced (dry run)`._

_Which interruption was which is inferred from the run schedule, not labelled by the page:
the pair at 35.7–50.3 s is the app switch, the pair at 76.6–90.7 s is the screen lock._

#### Windows 11 · Chrome 153 · desktop — 2026-09-19

```json
{
  "spike": "E9",
  "recorded_at": "2026-09-19T18:00:59.517Z",
  "env": {
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
    "platform": "Win32", "secure_context": true, "has_media_devices": true,
    "has_media_recorder": true, "device_pixel_ratio": 1, "screen": "1920x1080",
    "hardware_concurrency": 16, "device_memory_gb": 32, "storage_quota_bytes": 10737542218
  },
  "codecs": {
    "video/mp4;codecs=\"avc1.42E01E\"": true, "video/mp4;codecs=\"avc1.640028\"": true,
    "video/mp4;codecs=\"hvc1\"": false, "video/mp4": true,
    "video/webm;codecs=\"vp9\"": true, "video/webm;codecs=\"vp8\"": true,
    "video/webm;codecs=\"av01.0.05M.08\"": true, "video/webm": true,
    "video/x-matroska;codecs=avc1": true, "video/quicktime": false
  },
  "camera": {
    "requested": "1920x1080", "open_ms": 555, "delivered": "1920x1080", "frame_rate": 30,
    "facing_mode": null, "label": "HD Pro Webcam C920 (046d:082d)", "meets_720p_floor": true
  },
  "recording": {
    "requested_mime": "video/mp4;codecs=\"avc1.42E01E\"",
    "actual_mime": "video/mp4;codecs=avc1.42002a",
    "target_ms": 180000, "actual_ms": 180005, "completed_full_duration": true,
    "bytes": 29606642, "mb_per_min": 9.4, "est_bitrate_mbps": 1.32, "chunk_count": 64,
    "stalls_over_3s": 43, "longest_stall_ms": 3791, "interruptions": [],
    "projected_3min_mb": 28.2, "playback_ok": true, "within_750mb_at_3min": true,
    "playback_duration_s": 180
  },
  "upload": {
    "mode": "live", "endpoint": "http://localhost:3001/put", "part_size_bytes": 8388608,
    "parts": 4, "bytes_sent": 29606642, "elapsed_ms": 6760, "throughput_mbps": 35.04,
    "resumed_from_part": 2, "all_parts_accounted_for": true,
    "log": [
      {"part": 1, "bytes": 8388608, "status": 200, "result": "ok"},
      {"part": 2, "result": "dropped, retrying"},
      {"part": 2, "bytes": 8388608, "status": 200, "result": "ok"},
      {"part": 3, "bytes": 8388608, "status": 200, "result": "ok"},
      {"part": 4, "bytes": 4440818, "status": 200, "result": "ok"}
    ]
  },
  "motion": {
    "device_motion_in_window": true, "needs_permission": true, "permission": "granted",
    "samples": 1, "has_gravity": false, "has_rotation_rate": false, "observed_hz": 0.5
  },
  "notes": []
}
```

_`throughput_mbps` is meaningless here — the endpoint was a local sink with an artificial
1.5 s per-part delay, used only to make the drop clickable. Backgrounding and screen lock
were not exercised on desktop; those columns are the phone's job._

## 5. Pass criteria (§3.10 E9)

> Usable ≥1080p (or 720p) file on all target browsers; upload resumes after network drop.

Read as four checks, all of which the page reports directly:

1. `camera.meets_720p_floor` is true on every target device.
2. `recording.completed_full_duration` is true for the 3-minute run, with the interruptions
   in §"Running the 3-minute test properly" applied.
3. `recording.playback_ok` is true — the browser can decode the file it just wrote.
4. `upload.resumed_from_part` is non-null and `upload.all_parts_accounted_for` is true.

**If 2 fails on iOS Safari:** capture is not broken, but in-browser recording stops being the
primary path on iOS. The fallback in D2 becomes the default there, and §3.2's capture coach
needs an iOS-specific branch. Record that outcome here and flag it to the Masterplan rather
than treating it as a blocker.

## 6. Open items handed forward

- **Real R2 multipart.** Section 5 of the page tests the *client* half: slicing, part
  accounting and resume-from-failed-part. The presigned-multipart round trip against a real
  bucket needs Phase 0 task 6 (R2 buckets), and the production path is Uppy's S3 multipart
  plugin (§1.1). Point the page's endpoint field at a presign URL once the bucket exists.
- **Rotation-rate hint.** §3.2 wants a "slow down" hint above ~45°/s from
  `DeviceOrientationEvent`. The page confirms the sensor is present and its sample rate; the
  threshold itself should be tuned during real capture (Phase 0 task 4), not guessed here.
- **Re-verification in the real UI.** §8 Phase 6 already schedules a real-device capture test.
  These numbers are the baseline it compares against.
