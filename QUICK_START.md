# Setup

Six steps, about twenty minutes. Steps 1–3 get the app on your phone. Steps 4–6 make it fill itself in every morning.

Anything involving a password or an API key is yours to enter — I've left those as blanks rather than putting credentials in the code.

---

## 1. Create the repository

Go to **github.com/new** and create:

- Owner: `bhargavaraju-cmd`
- Name: `daily-financial-intelligence`
- **Public** (GitHub Pages is free on public repos; on a private repo it needs a paid plan)
- Don't add a README — this project already has one

Then upload the contents of this folder. Either drag them into the browser upload page, or:

```bash
cd daily-financial-intelligence
git init && git branch -M main
git remote add origin https://github.com/bhargavaraju-cmd/daily-financial-intelligence.git
git add . && git commit -m "initial" && git push -u origin main
```

## 2. Turn on GitHub Pages

**Settings → Pages → Source: Deploy from a branch → `main` / `/ (root)` → Save**

Give it a minute, then your app is at:

```
https://bhargavaraju-cmd.github.io/daily-financial-intelligence/
```

It will show sample data until step 6.

## 3. Put it on your phone

**Android / Chrome** — open the URL, menu (⋮) → *Add to Home screen*.

**iPhone / Safari** — open the URL, Share → *Add to Home Screen*. It must be Safari; Chrome on iOS can't install web apps.

You'll get a **Daily Intel** icon that opens with no browser chrome, and the last brief stays readable offline.

## 4. Add your API key

The brief is only as good as the analysis layer. Without a key it falls back to keyword scoring, which ranks stories but can't write "why this matters to you."

Get a key at **console.anthropic.com** → API Keys, then in your repo:

**Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | `sk-ant-...` |

Cost is roughly a few US cents a day — one call, once a morning.

## 5. Set up email

Gmail rejects your normal password from scripts. You need an **App Password**, which is a 16-character code that only works for mail and can be revoked on its own.

1. Turn on 2-Step Verification at **myaccount.google.com/security** (required before app passwords appear)
2. Go to **myaccount.google.com/apppasswords**
3. Create one named `daily intel` and copy the 16 characters

Add four more secrets:

| Name | Value |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | `bhargavraju111@gmail.com` |
| `SMTP_PASS` | the 16-character app password, no spaces |
| `DIGEST_TO` | `bhargavraju111@gmail.com` |

Enter these yourself — I haven't put any credential in the code, and you shouldn't paste that app password into a chat window either.

## 6. Run it once

**Actions → Daily brief → Run workflow**

If Actions asks you to enable workflows on a fresh repo, say yes. Watch the log: it prints how many sources answered, how many distinct events survived deduplication, and whether the email went out.

Then reopen the app. Real content, and a copy in your inbox.

From here it runs itself every morning.

---

## When something looks wrong

**App still shows sample data.** Pages serves from a CDN and lags a minute or two behind the commit. Check that the run in step 6 actually committed — the log ends with either `brief:` and a date, or `nothing changed`. Pull to refresh, or tap *Check for a newer brief* at the bottom.

**Brief arrives at 9:20 rather than 9:00.** Normal. Scheduled workflows queue behind everything else on GitHub's shared runners; delays of 5–30 minutes are routine and occasionally longer. The cron is set to 03:10 UTC to absorb most of that. If you want a guaranteed time you'd need a paid scheduler, which is not worth it for a morning read.

**"n unavailable" in the footer.** Expected, and not a failure. Government sites go down, change URL structure, and block cloud IP ranges. Each source is tried independently and a dead one is skipped. Worry when the number climbs past about a third.

**No email.** The workflow log says which check failed. Nearly always it's the app password: regular Gmail passwords are rejected, and the 16 characters must be entered without spaces.

**Nothing in the brief.** Some days there genuinely isn't much, and the app says so rather than padding. If it happens two days running, open the Actions log — the source count will tell you whether it's a quiet day or a broken fetch.

**Everything is priority 8.** That's the keyword fallback, meaning the model didn't run. Check `ANTHROPIC_API_KEY`, and check the model name in `DFI_MODEL` is still current.
