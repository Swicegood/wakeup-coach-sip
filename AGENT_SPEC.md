# Agent Specification — Wake-Up Coach

## 1. Mission (Non-Negotiable)

Your mission is to build a **dockerized wake-up coach service** that integrates with Asterisk via **ARI** and uses **OpenAI’s audio-native realtime model** to call me in the morning and converse with me until I am awake.

The system should be reliable, simple, and oriented toward *working end-to-end behavior* rather than theoretical completeness.

---

## 2. Authority & Autonomy Contract

You are authorized to:

* Read, create, and modify any files in this repository
* Design the project structure and choose implementation details
* Run commands, build Docker images, start/stop containers, and inspect logs
* Make architectural decisions unless explicitly constrained below

You should proceed **autonomously**, without asking for confirmation, **except** when:

* A decision is destructive or irreversible
* There are multiple viable designs with serious, long-term tradeoffs

Default posture: *act, test, adjust*.

---

## 3. Non-Goals & Forbidden Actions (Very Important)

The following are explicitly **out of scope** and must not be implemented:

* ❌ Implementing SIP, RTP, or media codecs directly
* ❌ Acting as a SIP peer or endpoint
* ❌ Modifying FreePBX-generated dialplan files directly
* ❌ Adding a UI (web, mobile, CLI menus, etc.)
* ❌ Introducing separate STT or TTS services
* ❌ Premature optimization for scale, HA, or multi-user support

If tempted to do any of the above, stop and reconsider the mission.

---

## 4. Hard Constraints (These Are Rails)

These constraints are facts, not suggestions:

* Telephony is handled entirely by **Asterisk**
* Integration with calls must use **ARI only**
* Audio must be streamed directly to/from **OpenAI’s audio-native realtime model**
* The service must run in **Docker**
* Latency and conversational continuity matter more than audio fidelity
* This is a single-user, single-call system

---

## 5. System Responsibilities (Conceptual)

The system is responsible for:

* Detecting call lifecycle events (answer, hangup, errors)
* Streaming audio from the call to the LLM in realtime
* Streaming audio from the LLM back into the call
* Maintaining conversational state across the call
* Persisting until wakefulness is detected or the call ends

Wakefulness is determined by keyword spoken by me

---

## 6. Feedback & Debugging Protocol

When something does not work:

1. Inspect logs and runtime behavior first
2. Form a concrete hypothesis
3. Test the hypothesis with minimal change
4. Prefer incremental fixes over rewrites

If you must ask me a question:

* Ask **one specific, high-leverage question**
* Clearly state what you tried and what you observed

Silence, partial audio, or “nothing happens” are signals — treat them seriously.

---

## 7. Agent Stance (Behavioral Guidance)

Act like a **senior engineer working semi-autonomously**.

* Prefer working systems over elegant abstractions
* Preserve any behavior that works
* Avoid large refactors unless clearly justified
* Keep the system simple enough to reason about while half-awake

Progress > perfection.

---

## 8. Definition of Success

This project is successful when:

* A scheduled call is placed
* Audio flows both directions
* The conversation continues naturally
* I reliably wake up and engage
* The system fails gracefully if anything goes wrong
## Wake-Up Call Persistence + Doorbell-Gated Call Ending

### Goal
When the assistant stops getting responses (user falls asleep), the system should:
1) Prompt the user with: **"Quick check-in: can you hear me?"**
2) Wait **10 seconds** for a response
3) If no response, **hang up** and **call back**
4) Repeat the call-back cycle indefinitely **unless** the user ends the call via:
   - Saying the magic word **"Goodbye"** (or "end call") AND
   - The system has received a valid **doorbell  webhook** within the last **5 minutes** (configurable)

This ensures the user must physically get out of bed (doorbell ) before they can end wake-up calls.

---

### Behavior Summary (State Machine)

#### States
- `CALL_ACTIVE`
- `WAITING_FOR_USER_AFTER_SLEEP_PROMPT`
- `CALL_ENDED`

#### Key flags
- `doorbell_activated` (bool): whether doorbell  event was received recently
- `doorbell_activation_time` (datetime): when last activation occurred
- `doorbell_timeout_task` (asyncio.Task): scheduled reset task for doorbell_activated

#### End conditions
- The call-back loop stops ONLY when:
  - user says **"goodbye"** or **"end call"** (case-insensitive), AND
  - `doorbell_activated == True` at that moment

If user says "goodbye" but `doorbell_activated == False`:
- Do **NOT** end the call
- The assistant should respond with something like:
  - "To end this wake-up call, please touch the doorbell   first."

---

### Sleep Detection / No-Response Handling

#### Trigger
If the system detects a prolonged silence / no-user-response condition during the call (implementation-specific), run the following procedure:

#### Procedure: sleep-check loop
1) Speak: **"Quick check-in: can you hear me?"**
2) Start a timer for **10 seconds**
3) If any valid user audio / speech is detected within 10s:
   - Return to normal conversation handling
4) If no user response within 10s:
   - Hang up the call
   - Wait a short backoff (recommended 1–3 seconds)
   - Initiate a new call to the user
   - Repeat indefinitely

#### Notes
- “Valid response” can be either:
  - ASR recognized text, OR
  - Non-trivial audio energy above threshold for N frames (optional)
- Keep the logic robust: false positives are acceptable; false negatives are worse.

---

## Doorbell Webhook: `/doorbell-webhook`

### Purpose
Receives notifications when someone uses the UniFi Protect doorbell   and enables the call-ending feature for a limited time.

### Endpoint Details
- **Route:** `POST /doorbell-webhook`
- **Purpose:** Activates the "magic words" feature that allows users to end wake-up calls after they physically get out of bed and touch the doorbell.

### How It Works

#### 1) Receives the webhook
UniFi Protect sends a POST request when a  authentication event occurs.

Implementation parsing (FastAPI/Starlette style):
```python
body = await request.body()
data = json.loads(body) if body else {}
event_type = data.get('event_type', '').lower()
device_id = data.get('device_id', '')
