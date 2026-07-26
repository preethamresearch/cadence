# The 300 milliseconds that decide whether your AI sounds human

Two people having a conversation leave about **200 milliseconds** between
turns. That gap is remarkably consistent — across languages, across cultures,
across phone lines. It is roughly a fifth of a second, and we are exquisitely
sensitive to it.

Push it to half a second and the other person sounds hesitant. Push it to a
full second and they sound distracted, or like they did not hear you. You will
start to repeat yourself.

I spent last week measuring that gap on an AI voice agent. The number came back
at **1,107 milliseconds**.

---

## Why "it feels laggy" is such a useless bug report

I have written that bug report. You probably have too. It is genuinely all you
can say, because the thing that went wrong is a *feeling* — and feelings do not
show up in dashboards.

Here is what makes it hard. When someone says a voice assistant feels slow,
there are at least four completely different things they might mean:

- It took a long time to **start** answering.
- It answered quickly but **rambled**.
- It kept **stopping mid-sentence** and starting again.
- It **talked over** them.

Those are four separate faults with four separate fixes, and they feel almost
identical to the person on the phone. Worse, in the systems we normally use to
watch software, they look identical too — the same numbers, none of them wrong,
none of them useful.

So I built something to tell them apart.

---

## What we were measuring, and what we were missing

Almost every tool for watching AI systems assumes a shape: you ask a question,
you get an answer, and the tool measures the time in between. That is a
perfectly good model for a chatbot.

Voice does not work like that. Both people are on the line the whole time. You
can interrupt. It can interrupt you. There is no clean moment where your turn
ends and its turn begins — that boundary is something you both *negotiate*,
constantly, without thinking about it.

Which means the most important measurement — how long you sat in silence
waiting — is not a thing the existing tools even have a name for. It falls in
the gap *between* the things they measure.

So I drew it.

![The silence between speakers, drawn as an amber gap](./blog_assets/05-console-silence.jpg)

Blue is the person talking. Violet is the agent. The amber band in the middle
is the silence, with the milliseconds counting up while it lasts.

I did not expect this to be the useful part. It was meant to be a debugging
view. But watching that number climb while you sit there waiting communicates
something a percentile chart cannot: you *feel* the delay rather than reading
about it. Two seconds of silence is unbearable on screen in a way that "2000ms"
simply is not.

---

## The interruption is the interesting signal

The measurement I did not anticipate finding useful was **where** people
interrupt.

Not *how often* — that number turns out to be nearly meaningless on its own.
Two agents can have identical interruption rates and completely opposite
problems:

- If people cut in **almost immediately** — within a few hundred milliseconds —
  they are usually not interrupting at all. The agent is mistaking background
  noise for speech and stopping itself. A door closing. A cough. Someone else
  in the room.
- If people cut in **several seconds** into the answer, they are genuinely
  interrupting, and it means the agent is talking too much. They got what they
  needed in the first sentence and are trying to move on.

Same count. Opposite causes. Opposite fixes. You only see the difference if you
record *when* inside the reply the interruption landed, and almost nothing
records that.

---

## What "good" should actually mean

Numbers are only useful once you decide which ones are acceptable. So I wrote
down six, with a reason for each rather than a round number that felt nice:

| What | Target | Why |
|---|---|---|
| Silence before replying | under 350 ms | Past this it reads as hesitation |
| Interruptions per call | under 0.8 | Above this, people are fighting to be heard |
| Talking over the user | under 150 ms | Beyond this it feels rude, not eager |
| Calls resolved without a human | above 72% | The number the deployment is funded on |
| Turns where the user repeated themselves | under 9% | The closest thing to "did it work" |
| Escalations to a human | under 11% | The commercial ceiling |

![The six objectives, with targets drawn on each panel](./blog_assets/02-slo-dashboard.jpg)

The fifth one is my favourite, and it took the longest to arrive at. **How
often did the person have to say it again?** Not the latency, not the token
count — whether the conversation actually worked. When someone says *"no, I
meant the other one"* or *"can you repeat that"*, something failed a turn
earlier. It is the closest thing to measuring understanding that I could find
without asking anybody to fill in a survey.

---

## The part where I was wrong

I built all of this against a simulator, because you cannot design dashboards
against three hand-held test calls. I tuned that simulator to what I believed
good performance looked like: replies starting in about 250 milliseconds.

Then I pointed the whole thing at a real Gemini Live agent.

**1,107 milliseconds.** Three to four times slower than the world I had spent a
day designing for.

![Real sessions plotted against the simulated baseline](./blog_assets/03-diagnostics-v16-v17.jpg)

The orange line is reality. The two flat lines below it are what I had assumed.

I could have quietly adjusted the simulator so the numbers agreed. I left it,
because that gap *is* the finding — and because an instrument that only ever
confirms what you already believed is not an instrument, it is decoration.

It also caught two genuine bugs in my own measuring code that thirty-five
automated tests had missed. Which is its own small lesson: the tests checked
that the code did what I told it to. Only real data checked whether what I told
it was right.

---

## If you are building one of these

Three things I would tell myself a week ago.

**Measure the silence, not the duration.** How long the whole exchange took is
almost irrelevant. How long the person waited before hearing *anything* is
nearly everything. They are different numbers and only one of them predicts
whether people enjoy talking to your agent.

**Record where interruptions happen, not just that they did.** A count tells
you something is wrong. The distribution tells you what.

**Point it at reality earlier than feels comfortable.** I lost a day to
assumptions that a single real call would have corrected. The simulator was
useful — it just was not true.

---

None of this is really about AI. It is about the fact that conversation has
rules we all follow without noticing, and we notice instantly when a machine
breaks them. The 200 millisecond gap. Yielding when someone cuts in. Not
repeating what was already understood.

We have got very good at making these systems say the right words. Getting the
timing right is a separate problem, and right now most of us are not even
measuring it.

---

*The tooling is open source, along with a three-minute demo:
[github.com/preethamresearch/cadence](https://github.com/preethamresearch/cadence)
· [watch it](https://youtu.be/CZ2TeXH-yFY)*

*Built for the Agents of SigNoz hackathon. Written by me; I used an AI
assistant for the code and for editing this post, which is declared in the
submission.*
