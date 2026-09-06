# Sales Copilot

A live teleprompter for cold calls. You keep the call on your phone, WhatsApp or Skype
and keep this app open next to it. It listens to the prospect, and about a second after
they stop talking the next sentence is on the glass in large type, written from your own
knowledge base and the prospect's own website.

Built for reps whose English is a second language. Zero monthly cost: Groq free tier plus
Jina Reader, which needs no key at all.

```
                                                    ┌──────────────────────┐
  prospect audio  ──┐                               │  Groq whisper        │
  (tab share or     │                               │  large v3 turbo      │
   virtual cable)   │   Int16 PCM 16 kHz            └──────────┬───────────┘
                    ├──► browser AudioWorklet ──► WebSocket ──►│ server VAD
  your microphone ──┘        (no MediaRecorder)     /ws/        │ segments an
                                                  teleprompter  │ utterance
                                                                ▼
   ┌───────────────────────┐    system prompt      ┌──────────────────────┐
   │ your knowledge base   ├──────────────────────►│  Groq chat model     │
   │ + Jina Reader scrape  │                       │  (streaming SSE)     │
   └───────────────────────┘                       └──────────┬───────────┘
                                                              │ tokens
   ┌──────────────────────────────────────────────────────────▼───────────┐
   │  TELEPROMPTER   text-4xl, sentence split, boresight mark, ask tick    │
   └───────────────────────────────────────────────────────────────────────┘
```

---

## Quick start

You need Node 20+, Python 3.12 and a free Groq key from https://console.groq.com/keys
(no card).

```bash
# 1. put your key where the backend can see it
cp .env.local.example backend/.env   # then put your key in GROQ_API_KEY

# 2. frontend env (no secrets in here)
cp frontend/.env.local.example frontend/.env.local

# 3. run both
./start.sh
```

Open http://localhost:3000

`start.sh` creates the venv, installs both dependency sets on first run, and starts
uvicorn on 8000 and Next.js on 3000. Ctrl+C stops both.

To run them separately:

```bash
cd backend  && ./run.sh                    # http://127.0.0.1:8000  (docs at /docs)
cd frontend && npm run dev                 # http://localhost:3000
```

---

## Using it on a real call

### 1. Set up the call

On the setup page, write what you sell. Be specific and use real numbers, the copilot is
only allowed to say what is written there. Then describe the client, a one line call goal,
and the language you will speak.

There are two ways to describe the client, and you can use either or both:

- **Client website.** A bare domain is fine. The backend fetches it through
  `https://r.jina.ai/<url>`, cleans it, and folds it into the notes.
- **About the client.** A free text box for the very common case where the client has no
  website at all, which is exactly who you are calling if you sell websites. Their trade,
  their city, their size, how they get customers today, the owner's name, anything you
  already know. It carries the same weight as a scraped page.

Everything is fused into one system prompt held in server memory. Nothing is written to
disk. If the website fetch fails you get an amber notice and the call still works from
what you sell plus whatever you wrote about the client.

### 2. Route the audio

The app captures two independent streams:

| lane | what it is | what it does |
|---|---|---|
| **PROSPECT** (cyan) | the person on the call | triggers a suggestion every time they stop talking |
| **YOU** (violet) | your own microphone | transcribed for context only, never triggers a suggestion |

Three ways to feed the PROSPECT lane, easiest first:

1. **Share a tab or screen.** Click SOURCES, pick "Share a tab or screen", and in
   Chrome's picker choose the tab (or Entire Screen) and tick **Share tab audio**.
   Works when the call is in a browser tab (WhatsApp Web, Google Meet, Teams).
2. **macOS virtual cable.** Install [BlackHole](https://existential.audio/blackhole/)
   (free), create a Multi Output Device in Audio MIDI Setup containing your speakers and
   BlackHole, set your call app's output to it, then pick BlackHole as the PROSPECT input.
3. **Windows virtual cable.** Install [VB-Cable](https://vb-audio.com/Cable/) (free), set
   the call app's output to CABLE Input, then pick CABLE Output as the PROSPECT input.

No hardware at all? The transcript rail has an INJECT box. Type what the prospect said and
the copilot answers it exactly as if it had heard it. The whole app is demoable that way.

### 3. Read the line

When the prospect stops, the suggestion lands on the glass and you read it out loud.

- Sentences are split into separate blocks so you breathe in the right places.
- A short accent tick in the left gutter marks the sentence that ends in a question, so
  you can see the ask coming.
- The word count sits in the header, so you know how long the read is before you start.
- Keys **1** to **8** fire the objection buttons instantly, without waiting for audio.

---

## Practice mode

You do not want your first ten cold calls to be the ones where you learn. Practice mode
puts a robot client on the other end. It talks first, it talks back out loud through your
browser, it pushes back with real objections, and it can hang up on you. The teleprompter
runs exactly as it does on a real call, so you are training the real skill: glance, read
out loud, keep going.

Pick "Practice call" at the top of the setup page, choose a level, and fill in the same
boxes you would for a real call. You practise against the client you are actually about
to phone.

| Level | What they are like |
|---|---|
| **Warm** | Friendly. They ask real questions and give you time to talk. |
| **Normal** | Busy and short. They push back two or three times. |
| **Brutal** | They want to hang up. You get one line to keep them. |

The client speaks through your browser's own voice, so it costs nothing and never runs out.
Your microphone is off while they talk, and it turns back on the moment they stop. Press
the space bar or the **Cut in** button to interrupt them, which is a real skill worth
practising. No microphone at all? Type your line in the log instead, the robot still answers.

### The scorecard

When you press **End and score me** you get a score out of 100 and, more useful, exactly
where every point came from:

| Row | Out of |
|---|---|
| Call result, did they agree | 30 |
| Times they said no, and whether you had an answer | 20 |
| Talking time, aim for about half | 15 |
| Questions asked | 10 |
| Reply speed, how fast you started talking | 10 |
| Prompter use, how many of its lines you actually used | 10 |
| Filler words | 5 |

Every number in there is counted in Python, never guessed by a model. Only the judgement
calls (what went well, what to fix, the best and worst moment) come from the model, and it
is told to quote your own words back and never invent a quote.

The part most people care about is **How much the prompter helped you**: how many of its
lines you used out of how many it gave you, plus your best moment and your worst moment
side by side, showing what the client said, what the prompter told you to say, and what
you actually said. That is the fastest way to find out whether you should be trusting the
screen more or less.

A practice call makes two model calls per turn instead of one, so it burns the Groq free
tier about twice as fast as a real call. If you see "You have hit the free Groq limit",
wait a minute.


### Line, or points

There is a switch in the teleprompter head: **Line** or **Points**.

**Line** gives you the whole sentence to read out loud. That is what a nervous rep
wants, and it is the default.

**Points** gives you two to four short triggers instead, and you say it in your own
words. It sounds like you rather than like somebody reading a script.

```
Line     I hear you. We start at 3,500 dollars. It brings new orders
         every day. Shall we look at the plan?

Points   3500 USD, 2 to 4 weeks
         Aster cut calls 61 percent
         ask for 15 minutes
```

The switch is remembered, so you set it once. Flipping it applies to the next line,
not the one already on the glass.

Two things happen on the server when you pick points, and both matter:

- The rules change, so the model writes points natively rather than a sentence
  being chopped up afterwards. Numbers come back as digits here, because you are
  glancing at them rather than reading them out.
- The copilot's own past lines stop being fed back to it. That was measured, not
  guessed: with its own full sentences in the history the model imitates them and
  keeps answering in sentences however firmly the rules say otherwise. It is also
  simply more correct, since in points mode you never said the copilot's line, you
  said your own words around it.


---

## Placing the call

Until now you dialled with your own phone and the app just listened. It still can, and that
is still the default and the only free way that needs no setup at all. There are now four
ways, and the app tells you the cost of each before you press anything.

| Option | Cost | What happens |
|---|---|---|
| **I will dial myself** | Free | You call from your phone or WhatsApp. The app only listens. |
| **WhatsApp, from my phone** | Free | The app opens WhatsApp with the number ready. You press call. |
| **WhatsApp, from the app** | Meta business rates | The app places the WhatsApp call. Needs a Meta business number. |
| **Phone call, from the app** | About 1 to 3 US cents a minute, plus the number | We ring your phone first, then join the client. The app hears both sides. |

The first two work right now with nothing to set up. The other two need keys, and the
picker greys them out and names the exact variables that are missing until you add them.

### Twilio setup

You need a Twilio account, a phone number you rent from them, and a public web address,
because Twilio has to reach your machine from the internet. On a laptop that means a tunnel:

```bash
./start-tunnel.sh          # free, no account, and it writes the address into .env for you
```

That uses Cloudflare, which needs no sign up, unlike ngrok. The address changes every time
you run it, which is why the script edits `backend/.env` itself. Leave it running while you
make calls.

Then check the whole setup with:

```bash
backend/.venv/bin/python backend/check_calling.py
```

It talks to the real Twilio API and tells you exactly what is still missing, including the
traps: a trial account that can only call verified numbers, a FROM number you do not own,
and a number with no voice capability.

Then in `backend/.env`:

```
TWILIO_ACCOUNT_SID=ACxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxx
TWILIO_FROM_NUMBER=+15551234567
PUBLIC_BASE_URL=https://something.trycloudflare.com
CALL_WEBHOOK_SECRET=any-long-random-string
```

When you press "Ring my phone", Twilio calls YOUR phone first, and only joins the client
after you pick up, so the client never hears dead air. Twilio then forks the live audio to
the app, which hears both sides of the call: the client on one lane, you on the other.

`CALL_WEBHOOK_SECRET` signs the webhook links so a stranger cannot drive them. Leave it
empty and a new one is made at every start, which is safe but breaks a call that is already
ringing when you restart the server.

A Twilio trial account can only call numbers you have verified in their console. That is
their rule, not ours, and the app passes their error through in plain words.

### WhatsApp setup

**Use the link option.** It needs nothing at all. The app opens `wa.me` with the number
filled in, you press call in WhatsApp, and you share that tab's audio so the app can hear
the client. That is the whole setup, and it is the only WhatsApp path that can cold call.

**Calling from inside the app cannot cold call, and that is Meta's rule, not a limitation
here.** Before reading further, know that this option is a dead end for prospecting. From
Meta's own documentation:

1. **You must ask for permission first.** A business may not simply dial a WhatsApp user.
   You send a `call_permission_request` message, and the person has to accept it. A
   production number may ask **one time per person per day, twice per week**. Cold calling
   a list is therefore impossible by design.
2. **Your number needs a 2,000 a day messaging limit.** New numbers start far below that
   and have to earn their way up through real messaging volume.
3. **The call is WebRTC.** `POST /{PHONE_NUMBER_ID}/calls` wants an SDP offer, so this needs
   a full media stack, not an HTTP request. The code here builds the correct request and
   accepts an `sdp_offer`, but it does not carry a WebRTC stack, so a real connect would be
   refused by Meta.

Business initiated calling is also switched off entirely in the United States, Canada,
Egypt, Vietnam and Nigeria. Pakistan is not on that list.

`WHATSAPP_TOKEN` and `WHATSAPP_PHONE_ID` switch the option on if you have all of the above.
**It was never run against the real API from here, because there are no Meta credentials on
this machine.** Everything else in this README was run and verified.

The picker in the app says all of this in one line before you press anything, so nobody
spends a week on Meta Business setup to find out it cannot do the job.

### What was actually verified

A real Twilio call needs a paid account, so instead the phone audio path was tested by
building exactly what Twilio sends: real speech encoded to 8 kHz G.711 mu-law, wrapped in
Twilio's own Media Streams frames, pushed at the media socket. Whisper read it back word
for word, the audio landed on the correct lane, and the copilot answered it. The transcode
is cross checked against the reference implementation on all 256 mu-law codes.

So the pipeline is proven. Only the credentials are missing.


---

## Latency

Measured on this machine against the Groq free tier, not estimated.

| stage | typical | notes |
|---|---|---|
| VAD endpoint | 620 ms | `VAD_SILENCE_MS`, this is a deliberate wait so you do not cut the prospect off |
| Groq whisper large v3 turbo | 300 to 430 ms | a 4 second utterance |
| chat model first token | 190 ms cold prompt, 640 ms with a 12k character site scrape | prefill dominates, so a shorter scrape is a faster copilot |
| **end to end, prospect stops to first word on the glass** | **about 1.1 to 1.3 s** | |

Tune `VAD_SILENCE_MS` down to 450 for a snappier copilot that sometimes interrupts, or up
to 800 if it keeps cutting people off mid sentence. The SOURCES popover has a live
sensitivity slider for noisy rooms.

### About the chat model

The original target for this project was `llama-3.3-70b-versatile`. **Groq has retired it**
and it now returns a 404. Every chat model Groq currently serves is a reasoning model, and
each family leaks its thinking differently:

- `openai/gpt-oss-*` streams thinking in a separate `delta.reasoning` field. Harmless to
  display but it delays the first real token by 400 to 600 ms.
- `qwen/qwen3.*` streams `<think> ... </think>` inside `delta.content`, which would put the
  model's private notes straight on the teleprompter.

The teleprompter output is deliberately kept at about a grade 4 reading level, two to four
short sentences, under 35 words, numbers written the way they are spoken, and no idioms or
sales jargon. That is enforced in the OUTPUT RULES block of
`backend/app/prompts.py`, change it there if you want a different voice.

`reasoning_params()` in `backend/app/services/groq_client.py` sends the right suppression
flags per family, and `StreamSanitizer` strips any `<think>` block that still gets through
(across chunk boundaries) and folds em dashes, smart quotes and non breaking hyphens into
plain ASCII a non native speaker can read at a glance.

Measured first token, same prompt:

| model | first token | verdict |
|---|---|---|
| `qwen/qwen3.6-27b` | **~190 ms** | default. Shortest, plainest, most speakable output |
| `openai/gpt-oss-120b` | ~490 ms | good output, noticeably slower |
| `openai/gpt-oss-20b` | ~500 ms | no faster than the 120b |
| `groq/compound-mini` | ~1030 ms | agentic, too slow for a live call |

Swap it any time with `LLM_MODEL` in `backend/.env`. The setup page footer reads the live
model ids from `/api/health`, so it can never go stale.

---

## Why raw PCM and not MediaRecorder

Groq has no streaming websocket STT, only a REST transcription endpoint. The obvious
approach, `MediaRecorder` with a short `timeslice`, does not work: only the first WebM
chunk carries a header, so every later chunk is individually undecodable.

Instead the browser runs an `AudioWorkletProcessor` that resamples to 16 kHz mono and posts
transferred Int16 buffers. Those go over the WebSocket with a one byte stream tag, and the
server runs an energy VAD with an adaptive noise floor, a 320 ms pre roll ring buffer so
word beginnings are not clipped, and a hangover before it closes an utterance and wraps it
in a WAV header for Groq. Deterministic, no codec surprises, and the level meter comes free.

---

## Project layout

```
backend/
  app/config.py                  settings, every knob is an env var
  app/prompts.py                 the system prompt template and the 8 objection prompts
  app/models.py                  pydantic models, snake_case in Python, camelCase on the wire
  app/services/audio.py          VAD segmenter, WAV encoder, no numpy, self testing
  app/services/groq_client.py    STT + streaming chat, reasoning suppression, sanitiser
  app/services/jina.py           Reader fetch, markdown cleaner, SSRF guard
  app/services/session_store.py  in memory sessions, transcript window, whisper biasing
  app/services/context_builder.py fuses knowledge base + scrape into the system prompt
  app/api/routes_context.py      REST
  app/api/routes_ws.py           the websocket, VAD, barge in, latency accounting
  app/practice.py                the robot client personas and the coach prompt
  app/services/practice_engine.py the persona and coach model calls, with fallbacks
  app/services/scoring.py        the scorecard arithmetic, pure and self testing
  app/api/routes_practice.py     practice REST
  app/telephony.py               the four calling options, readiness, number checking
  app/services/telephony_audio.py mu-law to 16 kHz PCM, self testing
  app/services/twilio_client.py  places the call, builds the TwiML
  app/services/whatsapp_client.py the link, and the Cloud API scaffold
  app/api/routes_call.py         calling REST
  app/api/routes_twilio.py       Twilio webhooks and the media socket
frontend/
  app/page.tsx                   setup
  app/call/page.tsx              the cockpit
  app/api/*/route.ts             server side proxies to FastAPI, so no CORS in the browser
  components/                    Teleprompter, ObjectionBar, WaveVisualizer, TranscriptRail, ...
  lib/audio/capture.ts           dual stream capture, worklet wiring, device handling
  lib/practice/speech.ts         the browser voice, with the Chrome speech bugs worked around
  lib/ws/client.ts               reconnect ladder, backpressure guard, heartbeat
  lib/hooks/useTeleprompter.ts   the one hook the dashboard hangs off
  public/worklets/               the PCM recorder worklet
```

---

## API

`POST /api/prepare-context`
body `{ knowledgeBase, clientUrl?, clientContext?, callGoal?, language? }`
-> `{ sessionId, systemPrompt, clientTitle, scrapeChars, scrapeOk, scrapeError, ... }`
`clientContext` is the free text description used when the client has no website.
`GET  /api/health`          -> `{ status, groqConfigured, sessions, version, models }`
`POST /api/practice/start`  same body as prepare-context plus `difficulty`, returns the opening line
`GET  /api/practice/difficulties`
`GET  /api/call/providers`  -> the four ways to call, with cost and what is missing
`POST /api/call/start`      -> places the call, or hands back a WhatsApp link
`POST /api/call/{id}/hangup`
`WS   /ws/twilio`           -> Twilio Media Streams, token guarded
`GET  /api/practice/{id}/debrief` -> the scorecard
`GET  /api/quick-actions`   -> the 8 frozen objection buttons
`GET  /api/session/{id}`    -> `{ exists, turns, createdAt }`
`WS   /ws/teleprompter?session_id=<uuid>`

WebSocket, client to server: binary frames are `[stream byte][Int16 LE PCM @16 kHz]` where
the stream byte is `0` for the prospect and `1` for you. Text frames are JSON with a `type`
of `ping`, `control`, `quick_action`, `manual_text` or `config`.

Server to client: `ready`, `pong`, `vad`, `transcript`, `suggestion_start`,
`suggestion_delta`, `suggestion_done`, `status`, `error`, and in practice mode
`client_turn`, `practice_state` and `practice_over`.

Interactive docs at http://127.0.0.1:8000/docs

---

## Troubleshooting

**"No audio track" when sharing a tab.** Chrome refuses audio only display capture. In the
picker choose a Tab or Entire Screen and tick "Share tab audio". Sharing a Window never
carries audio.

**Device list shows blank labels.** Browsers hide device names until you have granted mic
permission once. Start your own microphone first, then reopen SOURCES.

**Nothing transcribes.** Check the PROSPECT lane is actually moving in the wave visualizer.
A flat lane means the audio is not reaching the browser, which is a routing problem, not an
app problem. If the lane moves but nothing appears, drop the sensitivity slider.

**Suggestions feel late.** Lower `VAD_SILENCE_MS`, and shorten the site scrape with
`MAX_SCRAPE_CHARS`. Prefill on a large system prompt is the single biggest cost.

**"Backend unreachable" on the setup page.** The FastAPI process is not running. Start it
with `cd backend && ./run.sh`.

**Rate limited.** The Groq free tier has per minute limits, and keyless Jina Reader allows
about 20 requests a minute. A free Jina key raises that. Neither costs money.

---

## Security notes

- `GROQ_API_KEY` lives only in `backend/.env` and never reaches the browser. The frontend
  talks to Groq exclusively through the FastAPI process.
- The Jina fetcher refuses loopback, private, link local and reserved addresses, resolving
  the hostname first, so the endpoint cannot be used to probe an intranet or a cloud
  metadata service.
- Sessions live in server memory only and expire after `SESSION_TTL_SECONDS` (6 hours by
  default). Transcripts are never written to disk.
- `.env`, `.env.local` and `backend/.env` are gitignored.

You are recording a live conversation. Whether you need the other party's consent depends
on where you both are. Check before you use this on a real call.
