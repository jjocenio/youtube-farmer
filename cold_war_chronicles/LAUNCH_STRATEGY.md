# Chronicles of Eras — Launch Strategy
**Channel:** @chroniclesoferas
**Week starting:** May 12, 2026

---

## Situation

- 6 long-form documentaries published (0 views — fresh re-upload)
- 6 Shorts ready to produce and publish (manifests + SEO metadata done)
- Channel is new to the algorithm — no audience signal established yet

**Primary goal this week:** get all 6 Shorts live and start building the funnel from Shorts → long-form.

---

## The Rule That Governs Everything

> Do not publish a second Short for any video until the first Short shows signs of converting viewers to long-form.

Adding more Shorts to the same video before you have data is wasted production. Publish the long-form first, then one Short. Let the data tell you which angles deserve a second Short.

---

## Week 1 — Publish All 6 Shorts
*May 12–22 — every 2 days*

| Day | Date | Short | Title | File |
|-----|------|-------|-------|------|
| Mon | May 12 | Crypto AG | *They Were Paying the CIA* | `crypto_ag_.../short_1/` |
| Wed | May 14 | Stanislav Petrov | *He Had 5 Minutes* | `stanislav_petrov_.../short_1/` |
| Fri | May 16 | Able Archer 83 | *NATO Almost Started WW3* | `able_archer_.../short_1/` |
| Sun | May 18 | Camp Century | *The Nuclear City Under the Ice* | `the_secret_nuclear_.../short_1/` |
| Tue | May 20 | Operation Gold | *The KGB Knew the Whole Time* | `operation_gold_.../short_1/` |
| Thu | May 22 | IRD / UK Propaganda | *The Department That Never Existed* | `uk_cold_war_.../short_1/` |

**Why this order:** Crypto AG leads because it has the broadest awareness (Washington Post 2020 coverage = existing search demand) and the strongest cognitive hook. Petrov and Able Archer follow as the most emotionally charged stories. Camp Century and Operation Gold are strong but slightly more niche. IRD closes the batch — compelling story but the narrowest keyword pool.

### For each upload:
1. Use the title from `short_1/youtube.md` → Recommended variant
2. Paste the **Full Description** block directly — hashtags are already at the bottom
3. Paste the **Tags** as a comma-separated list into the Tags field (no `#`)
4. Generate thumbnail using the Fal.ai prompt in `youtube.md`, add text overlay
5. Set category: **Education** (not Entertainment — affects RPM)
6. Do **not** add end screens or cards — Shorts don't render them

---

## Week 2 — Monitor and Produce
*May 23–29*

**No new uploads this week.** Let the Shorts run.

### What to check (YouTube Studio → Analytics → each Short):

| Metric | Green | Yellow | Red |
|--------|-------|--------|-----|
| Views rate (first 48h) | Growing | Flat | Declining |
| % Viewed | > 70% | 50–70% | < 50% |
| Swipe-away point | After 15s | 5–15s | < 5s |
| Likes / Views | > 3% | 1–3% | < 1% |
| Click-through to channel | Any | — | Zero |

**If a Short is below 50% viewed:** the hook or pacing is the problem. Note which scene viewers are dropping — the timestamp will show in the retention graph.

**If a Short is above 70% viewed but getting zero channel clicks:** the funnel card (the "full documentary" scene) isn't working. Consider retitling or changing the thumbnail.

### Also this week:
- Begin production on the next long-form video
- Choose the topic, run the `/youtube ideate` flow if needed
- Aim to have a script ready by end of week

---

## Week 3+ — The Ongoing Cadence

```
New long-form published
        ↓
Immediately publish 1 Short from that video
        ↓
Wait 2–3 weeks, check if Short converts to long-form watch time
        ↓
If yes → produce Short 2 for that video (different angle)
If no  → move on, produce Short for next long-form
```

**Target cadence:** 1 long-form every 2–3 weeks + 1 Short per long-form on publish day.

Do not chase a Shorts-only cadence. This channel's value is in the documentaries. Shorts are the top of the funnel, not the product.

---

## When to Add a Second Short

Check these three things before producing a Short 2 for any video:

1. **Short 1 has > 70% completion rate** — the format and hook worked
2. **Short 1 sent at least some viewers to the long-form** — check "traffic source: related video" in the long-form's analytics
3. **You have a genuinely different angle** — not a variation of the same hook, but a different reveal from the same documentary

If all three are true, produce Short 2 using the same manifest format in `short_2/manifest.json` and `short_2/youtube.md`.

---

## What Not to Do

- **Don't publish a Short and a long-form on the same day** — split them by at least 48 hours so each gets independent algorithmic treatment
- **Don't add background music from licensed libraries** — all 6 Shorts are set to no-music for 100% revenue pool allocation; if you add music, use only Suno-generated tracks you own
- **Don't upload more than 1 Short per day** — the algorithm needs time to test each one separately
- **Don't delete a Short if it underperforms in week 1** — Shorts can pick up weeks later when the algorithm finds the right audience; deletion resets everything

---

## File Reference

Each video follows this structure:

```
video_directory/
├── manifest.json          ← long-form production timeline
├── youtube.md             ← long-form SEO metadata
└── short_1/
    ├── manifest.json      ← Short production timeline (scenes, narration, SFX)
    └── youtube.md         ← Short SEO metadata (titles, description, tags, thumbnail)
```

When you produce Short 2, create `short_2/` alongside `short_1/` — same structure, no renaming needed.

---

## 30-Day Checkpoint

At the end of May, ask these questions:

- Which Short had the highest completion rate?
- Which Short sent the most viewers to long-form?
- Are those the same Short, or different ones?
- What do the top-performing Shorts have in common (hook style, topic, visual)?

Use those answers to decide which angles to double down on for Short 2s, and which approach to use for future Shorts on new long-form videos.
